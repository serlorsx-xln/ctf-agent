"""Docker sandbox container lifecycle — start/stop/exec/files/packs."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shlex
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiodocker

from backend.sandbox.container_paths import container_path_parts
from backend.sandbox.docker_client import (
    CONTAINER_LABEL,
    OWNER_PID_LABEL,
    SESSION_ID_LABEL,
    _docker_cli,
    _docker_client,
    _track_start,
    _track_stop,
    docker_cp_from_container,
    ensure_start_semaphore,
)

# _docker_cli still used by pack materialize / finalize paths.
from backend.sandbox.governor import (
    apply_live_memory,
    parse_memory_bytes,
    recommended_memory_limit,
    sandbox_nano_cpus,
)
from backend.sandbox.harden import (
    harden_hosts_edit_command,
    harden_nmap_command,
    parse_challenge_network_hints,
)
from backend.sandbox.packs import (
    _acquire_pack_flock,
    _pack_cache_is_ready,
    _pack_cache_lock,
    _release_pack_flock,
)
from backend.sandbox.proxy import _DEFAULT_PROBE_PORTS, _lab_probe_script

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class DockerSandbox:
    """Isolated Docker container for a single solver agent."""

    image: str
    challenge_dir: str
    memory_limit: str = "16g"
    # Optional Settings (or duck-typed) for pack preflight / eval_strict_packs.
    settings: Any = None
    # Artemis TUI / swarm session id (container label + orphan cleanup scope).
    session_id: str = "_default"
    # Wall time spent in pack prefetch (materialize + ensure) for eval artifacts.
    preflight_ms: float = 0.0
    workspace_dir: str = ""
    ensured_packs: set[str] = field(default_factory=set)
    extra_path_dirs: list[str] = field(default_factory=list)
    _container: Any = field(default=None, repr=False)
    _docker: Any = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _binds: list[str] = field(default_factory=list, repr=False)
    # Packs whose trees were RO bind-mounted from the host cache at start.
    _bind_mounted_packs: set[str] = field(default_factory=set, repr=False)
    # Host SOCKS port (Mac VPN passthrough); None when disabled / unused.
    _host_proxy_port: int | None = field(default=None, repr=False)
    # When True, agent bash is wrapped with proxychains → host SOCKS.
    # auto: DIRECT when the container reaches the lab; SOCKS only as fallback.
    _host_proxy_wrap: bool = field(default=False, repr=False)
    # Ports mentioned in challenge text (custom services); used for lab probes.
    _challenge_ports: list[int] = field(default_factory=list, repr=False)
    # Host temp dirs to delete on stop (e.g. empty distfiles bind).
    _temp_dirs: list[str] = field(default_factory=list, repr=False)

    @property
    def container_id(self) -> str:
        """The Docker container ID, available after start()."""
        if not self._container:
            raise RuntimeError("Sandbox not started")
        return self._container.id

    @staticmethod
    def _is_container_gone_error(exc: BaseException) -> bool:
        """True when Docker reports the container no longer exists.

        File 404s from ``get_archive`` also say "404" and "container"
        (``Could not find the file … in container <id>``). Those must not
        recreate the sandbox.
        """
        text = str(exc).lower()
        if (
            "could not find the file" in text
            or "no such file" in text
            or "no file found" in text
        ):
            return False
        if "no such container" in text:
            return True
        if "404" in text and "container" in text:
            return True
        status = getattr(exc, "status", None)
        return status == 404

    async def _ensure_container_unlocked(self) -> None:
        """Make sure we have a live container (caller must hold ``_lock``)."""
        if not self.workspace_dir or not self._binds:
            raise RuntimeError("Sandbox not started")
        if self._container is not None:
            try:
                await self._container.show()
                return
            except Exception as e:
                if not self._is_container_gone_error(e):
                    # Stale handle / daemon blip — still try recreate.
                    logger.warning("Sandbox inspect failed (%s) — recreating", e)
                else:
                    logger.warning(
                        "Sandbox container gone (%s) — recreating (workspace kept)",
                        e,
                    )
        else:
            logger.warning("Sandbox container missing — recreating (workspace kept)")
        await self._recreate_container_unlocked()

    async def _recreate_container_unlocked(self) -> None:
        """Create a fresh container with the same binds/workspace; re-apply packs."""
        packs = set(self.ensured_packs) | set(self._bind_mounted_packs)
        if self._container is not None:
            try:
                await self._container.delete(force=True)
            except Exception:
                pass
            self._container = None
            await _track_stop()
        if self._docker is None:
            self._docker = _docker_client()
        self.ensured_packs.clear()
        self.extra_path_dirs.clear()
        # Keep self._binds and self._bind_mounted_packs — recreate remounts them.
        await self._create_and_start(self.image)
        if self._host_proxy_port:
            await self._install_host_proxy_client(self._host_proxy_port)
            await self._calibrate_host_proxy_routing()
        await self.refresh_tools_doc()
        for pack_id in packs:
            try:
                msg = await self._ensure_pack_inner(pack_id)
                logger.info("Re-ensure pack %s after recreate: %s", pack_id, msg)
            except Exception as e:
                logger.warning("Re-ensure pack %s failed: %s", pack_id, e)

    def _parse_memory_limit(self) -> int:
        s = self.memory_limit.strip().lower()
        try:
            if s.endswith("g"):
                return int(s[:-1]) * 1024 * 1024 * 1024
            if s.endswith("m"):
                return int(s[:-1]) * 1024 * 1024
            return int(s)
        except (ValueError, IndexError):
            logger.warning("Invalid memory_limit %r, defaulting to 4GB", self.memory_limit)
            return 4 * 1024 * 1024 * 1024

    def _host_config(self) -> dict[str, Any]:
        return {
            "Binds": self._binds,
            "ExtraHosts": ["host.docker.internal:host-gateway"],
            "CapAdd": ["SYS_ADMIN", "SYS_PTRACE"],
            "SecurityOpt": ["seccomp=unconfined"],
            "Devices": [
                {
                    "PathOnHost": "/dev/loop-control",
                    "PathInContainer": "/dev/loop-control",
                    "CgroupPermissions": "rwm",
                }
            ],
            "Memory": self._parse_memory_limit(),
            "NanoCpus": sandbox_nano_cpus(),
            # Agents often crash qemu/pwn binaries; unbounded cores fill workspace
            # (100MB+ per dump) and derail the solve.
            "Ulimits": [{"Name": "core", "Soft": 0, "Hard": 0}],
        }

    async def start(self) -> None:
        sem = ensure_start_semaphore()
        async with sem:
            self._docker = _docker_client()

            from backend.platform_paths import docker_volume_path, ensure_docker_bind_dir

            self.workspace_dir = tempfile.mkdtemp(prefix="ctf-workspace-")
            ensure_docker_bind_dir(self.workspace_dir)

            challenge_root = Path(self.challenge_dir).resolve()
            from backend.challenge import distfiles_host_path
            from backend.pack_preflight import resolve_prefetch_packs
            from backend.tool_router import pack_binds_enabled

            dist_host = distfiles_host_path(challenge_root)
            binds: list[str] = [
                f"{docker_volume_path(self.workspace_dir)}:/challenge/workspace:rw"
            ]
            if dist_host is not None:
                binds.append(f"{docker_volume_path(dist_host)}:/challenge/distfiles:ro")
            else:
                # Web/link-only: empty distfiles so the path still exists in-container.
                empty_dist = tempfile.mkdtemp(prefix="ctf-dist-empty-")
                ensure_docker_bind_dir(empty_dist)
                self._temp_dirs.append(empty_dist)
                binds.append(f"{docker_volume_path(empty_dist)}:/challenge/distfiles:ro")
            for name in (
                "challenge.txt",
                "challenge.md",
                "description.txt",
                "description.md",
                "README.md",
            ):
                p = challenge_root / name
                if p.is_file():
                    binds.append(f"{docker_volume_path(p)}:/challenge/{p.name}:ro")

            # Prefetch packs before create so large trees can share RO host binds.
            self._bind_mounted_packs = set()
            prefetch = resolve_prefetch_packs(self.settings, self.challenge_dir)
            strict = bool(getattr(self.settings, "eval_strict_packs", False))
            preflight_t0 = time.monotonic()
            logger.info(
                "preflight_start packs=%s strict=%s",
                prefetch or [],
                strict,
            )

            bumped = recommended_memory_limit(self.memory_limit, prefetch)
            if bumped != self.memory_limit:
                logger.info(
                    "Raising sandbox memory %s → %s for packs %s",
                    self.memory_limit,
                    bumped,
                    sorted(prefetch),
                )
                self.memory_limit = bumped
            # Writable pack scratch (blutter Dart VM, etc.) must mount even when
            # CTF_PACK_BIND=0 forces docker-cp for RO donor trees — otherwise every
            # container recompiles the Dart VM from scratch.
            binds.extend(self._pack_state_bind_strings())
            if pack_binds_enabled() and prefetch:
                async def _materialize_one(pack: str) -> tuple[str, list[str]]:
                    pack_t0 = time.monotonic()
                    cache = await self._materialize_pack_cache(pack)
                    pack_binds = self._pack_bind_strings(cache, pack)
                    if pack_binds:
                        logger.info(
                            "Pack %s: %d RO bind(s) from host cache %s (%.0fms)",
                            pack,
                            len(pack_binds),
                            cache,
                            (time.monotonic() - pack_t0) * 1000,
                        )
                    return pack, pack_binds

                for item in await asyncio.gather(
                    *[_materialize_one(p) for p in prefetch],
                    return_exceptions=True,
                ):
                    if isinstance(item, BaseException):
                        logger.warning(
                            "Pack bind prep failed (will docker cp on ensure): %s",
                            item,
                        )
                        if strict:
                            raise item
                        continue
                    pack, pack_binds = item
                    if pack_binds:
                        binds.extend(pack_binds)
                        self._bind_mounted_packs.add(pack)

            self._binds = binds

            from backend.tool_router import PREFETCH_RUNTIME_IMAGES, resolve_runtime_l0_image

            runtime_pref = resolve_runtime_l0_image(prefetch, self.image)
            if runtime_pref in PREFETCH_RUNTIME_IMAGES.values():
                from backend.sandbox.donor_build import ensure_donor_image

                pack_id = next(
                    (p for p, img in PREFETCH_RUNTIME_IMAGES.items() if img == runtime_pref),
                    None,
                )
                if pack_id:
                    ok, build_msg = await ensure_donor_image(pack_id)
                    if not ok:
                        logger.warning(
                            "Runtime image %s unavailable (%s); falling back to core",
                            runtime_pref,
                            build_msg,
                        )
                        runtime_pref = "ctf-sandbox-core"
                    else:
                        logger.info(
                            "Using baked runtime %s for prefetch %s (%s)",
                            runtime_pref,
                            sorted(prefetch),
                            build_msg,
                        )
            self.image = await self._resolve_l0_image(runtime_pref)

            from backend.host_proxy import (
                acquire_host_proxy,
                host_proxy_mode,
                release_host_proxy,
            )

            # Skip SOCKS entirely for offline / localhost challenges — avoids
            # noisy proxy logs and useless calibration when the lab is on the host.
            probe_hosts, _ = parse_challenge_network_hints(self._challenge_text())
            force_proxy = host_proxy_mode() in ("1", "true", "yes", "on")
            if probe_hosts or force_proxy:
                self._host_proxy_port = await acquire_host_proxy()
            else:
                self._host_proxy_port = None
                logger.info("Host VPN routing: DIRECT (no remote lab hosts; skipping SOCKS)")
            self._host_proxy_wrap = False
            try:
                await self._create_and_start(self.image)
                await self._ensure_pip_break_system_wrapper()
                if self._host_proxy_port:
                    await self._install_host_proxy_client(self._host_proxy_port)
                    await self._calibrate_host_proxy_routing()
                await self.refresh_tools_doc()

                # Bootstrap prefetched packs in parallel (bind-mounted trees skip the copy).
                # Refresh /tools.txt once after all packs (not per-pack).
                if prefetch:
                    async def _prefetch_one(pack: str) -> None:
                        pack_t0 = time.monotonic()
                        if pack == "pwn" and "pwn" in (self.image or ""):
                            hint = "baked pwn runtime (usually seconds)"
                        elif pack == "ghidra":
                            hint = "ghidra apt/pip ~3–5 min on fresh core"
                        elif pack == "pwn":
                            hint = "pwn pip/angr ~5–8 min on fresh core"
                        else:
                            hint = "usually under 2 min"
                        logger.info("Prefetch bootstrap pack=%s (%s)…", pack, hint)
                        msg = await self.ensure_pack(pack, refresh_tools=False)
                        logger.info(
                            "Prefetch pack %s: %s (%.0fms)",
                            pack,
                            msg,
                            (time.monotonic() - pack_t0) * 1000,
                        )

                    results = await asyncio.gather(
                        *[_prefetch_one(p) for p in prefetch],
                        return_exceptions=True,
                    )
                    for item in results:
                        if isinstance(item, BaseException):
                            logger.warning("Pack prefetch failed: %s", item)
                            if strict:
                                raise item
                    await self.refresh_tools_doc()
            except Exception:
                if self._host_proxy_port is not None:
                    await release_host_proxy()
                    self._host_proxy_port = None
                    self._host_proxy_wrap = False
                raise
            finally:
                self.preflight_ms = (time.monotonic() - preflight_t0) * 1000
                logger.info(
                    "preflight_done packs=%s preflight_ms=%.0f",
                    prefetch or [],
                    self.preflight_ms,
                )

    async def _resolve_l0_image(self, preferred: str) -> str:
        """Use preferred L0 if present; else ``ctf-sandbox-core``."""
        from backend.tool_router import DEFAULT_L0_CANDIDATES

        candidates: list[str] = []
        if preferred:
            candidates.append(preferred)
        for c in DEFAULT_L0_CANDIDATES:
            if c not in candidates:
                candidates.append(c)

        assert self._docker is not None
        last_err: Exception | None = None
        for img in candidates:
            try:
                await self._docker.images.inspect(img)
                if img != preferred:
                    logger.warning(
                        "L0 image %r not found; falling back to %r",
                        preferred,
                        img,
                    )
                return img
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(
            f"No L0 sandbox image found (tried {candidates}). "
            "Build: docker build -f sandbox/Dockerfile.core -t ctf-sandbox-core ."
        ) from last_err

    async def _create_and_start(self, image: str) -> None:
        if not self._docker:
            raise RuntimeError("Docker client not connected")
        config = {
            "Image": image,
            "Cmd": ["sleep", "infinity"],
            "WorkingDir": "/challenge",
            "Tty": False,
            "Labels": {
                CONTAINER_LABEL: "true",
                OWNER_PID_LABEL: str(os.getpid()),
                SESSION_ID_LABEL: (
                    (self.session_id or "").strip()
                    or (os.environ.get("ARTEMIS_SESSION_ID") or "").strip()
                    or "_default"
                ),
            },
            "HostConfig": self._host_config(),
        }
        self._container = await self._docker.containers.create(config)
        await self._container.start()
        self.image = image
        await _track_start()
        cid = getattr(self._container, "id", None) or getattr(self._container, "_id", "")
        logger.info("Sandbox started: %s (image=%s)", str(cid)[:12] or "?", image)

    async def _install_host_proxy_client(self, port: int) -> None:
        """Install proxychains + config so agent bash *can* use the host VPN SOCKS.

        Whether commands are actually wrapped is decided by
        ``_calibrate_host_proxy_routing`` (DIRECT when the lab is reachable).
        """
        import re

        from backend.host_proxy import proxychains_conf

        # proxychains4 requires a numeric first-hop IP (not a hostname).
        # Prefer IPv4 — Docker Desktop often returns IPv6 first and hop probes fail.
        res = await self._exec_inner(
            "getent ahostsv4 host.docker.internal 2>/dev/null | awk '{print $1; exit}' "
            "|| getent hosts host.docker.internal | awk '/^[0-9]+\\./{print $1; exit}'",
            timeout_s=15,
            via_host_proxy=False,
        )
        proxy_host = (res.stdout or "").strip().splitlines()
        proxy_ip = proxy_host[0].strip() if proxy_host else ""
        if proxy_ip and ":" in proxy_ip and "." not in proxy_ip:
            logger.warning(
                "host.docker.internal resolved to IPv6 %s; retrying for IPv4",
                proxy_ip,
            )
            res4 = await self._exec_inner(
                "getent ahostsv4 host.docker.internal | awk '{print $1; exit}'",
                timeout_s=15,
                via_host_proxy=False,
            )
            v4 = (res4.stdout or "").strip().splitlines()
            if v4 and re.match(r"^\d+\.\d+\.\d+\.\d+$", v4[0].strip()):
                proxy_ip = v4[0].strip()
        if not proxy_ip or (":" in proxy_ip and "." not in proxy_ip):
            # Prefer container default gateway, then common Colima / Desktop addrs.
            gw = await self._exec_inner(
                "ip -4 route show default 2>/dev/null | awk '{print $3; exit}'",
                timeout_s=10,
                via_host_proxy=False,
            )
            gw_ip = (gw.stdout or "").strip().splitlines()
            candidates = [
                *(gw_ip[:1]),
                "192.168.65.1",  # Docker Desktop
                "192.168.5.2",  # Colima
            ]
            proxy_ip = next(
                (c.strip() for c in candidates if re.match(r"^\d+\.\d+\.\d+\.\d+$", c.strip())),
                "192.168.65.1",
            )
            logger.warning(
                "Could not resolve IPv4 for host.docker.internal; using %s for SOCKS",
                proxy_ip,
            )

        # Install package first, then overwrite config (avoid dpkg conffile fights).
        check = await self._exec_inner(
            "command -v proxychains4 >/dev/null || "
            "(apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y -qq proxychains4)",
            timeout_s=180,
            via_host_proxy=False,
        )
        if check.exit_code != 0:
            logger.warning(
                "proxychains4 install failed (host VPN proxy degraded): %s",
                (check.stderr or check.stdout)[:300],
            )
            self._host_proxy_wrap = False
            return

        conf = proxychains_conf(port, proxy_host=proxy_ip)
        await self._write_file_inner("/etc/proxychains4.conf", conf.encode("utf-8"))
        which = await self._exec_inner(
            "command -v proxychains4 && tail -3 /etc/proxychains4.conf",
            timeout_s=15,
            via_host_proxy=False,
        )
        # Reachability of the SOCKS hop itself (not whether we will wrap).
        hop = await self._exec_inner(
            f'python3 -c "import socket; s=socket.create_connection(('
            f"'{proxy_ip}',{port}),timeout=3); s.close(); print('HOP_OK')\"",
            timeout_s=15,
            via_host_proxy=False,
        )
        probe = await self._exec_inner(
            "proxychains4 -q -f /etc/proxychains4.conf "
            "curl -sI --connect-timeout 8 http://example.com/ >/dev/null",
            timeout_s=30,
            via_host_proxy=False,
        )
        logger.info(
            "Host SOCKS installed socks5://%s:%s (hop=%s proxy_probe_exit=%s) %s",
            proxy_ip,
            port,
            "ok" if "HOP_OK" in (hop.stdout or "") else "fail",
            probe.exit_code,
            (which.stdout or "")[:120],
        )

    def _challenge_text(self) -> str:
        from pathlib import Path

        root = Path(self.challenge_dir)
        for name in (
            "challenge.txt",
            "challenge.md",
            "description.txt",
            "description.md",
            "README.md",
        ):
            p = root / name
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        return ""

    def _challenge_probe_hosts(self) -> list[str]:
        """Lab/target IPs from challenge text (for direct-vs-SOCKS calibration)."""
        hosts, ports = parse_challenge_network_hints(self._challenge_text())
        self._challenge_ports = ports
        return hosts

    def _probe_ports(self) -> list[int]:
        """Challenge-mentioned ports first, then generic defaults (incl. sentinels)."""
        ordered: list[int] = []
        seen: set[int] = set()
        for p in list(self._challenge_ports) + list(_DEFAULT_PROBE_PORTS):
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        return ordered

    async def _probe_direct_lab_access(self, hosts: list[str]) -> bool:
        """True if the container can reach a lab host without SOCKS.

        Success = TCP open **or** connection refused (route exists; port may be
        closed / custom-only). Timeout/filter-only ⇒ not reachable this path.
        """
        if not hosts:
            return False
        script = _lab_probe_script(hosts, self._probe_ports(), "DIRECT_OK")
        result = await self._exec_inner(
            f"python3 -c {shlex.quote(script)}",
            timeout_s=40,
            via_host_proxy=False,
        )
        return "DIRECT_OK" in (result.stdout or "")

    async def _probe_socks_lab_access(self, hosts: list[str]) -> bool:
        """True if lab TCP works through host SOCKS (open or refused)."""
        if not hosts:
            return False
        script = _lab_probe_script(hosts, self._probe_ports(), "SOCKS_OK")
        result = await self._exec_inner(
            f"proxychains4 -q -f /etc/proxychains4.conf python3 -c {shlex.quote(script)}",
            timeout_s=50,
            via_host_proxy=False,
        )
        return "SOCKS_OK" in (result.stdout or "")

    async def _calibrate_host_proxy_routing(self) -> None:
        """Pick SOCKS vs direct for agent bash.

        Policy (auto):
        1. Prefer **DIRECT** when the container can TCP-reach a lab host — faster
           and avoids connect-scan timeouts through a high-latency SOCKS hop.
        2. Fall back to **SOCKS** when direct fails but SOCKS reaches the lab
           (typical Docker Desktop + host VPN gap).
        3. If both DIRECT and SOCKS lab probes fail → leave **unwrapped** (do not
           force proxychains onto a dead path).
        4. No lab hosts in challenge text → no wrap (offline / local challenges).
        5. ``CTF_HOST_PROXY=1`` forces SOCKS when the hop is up.

        Reachability treats connection-refused like success so custom-only open
        ports still calibrate correctly when a closed sentinel RSTs. Ports named
        in the challenge text are probed first. Hosts may be RFC1918 IPs or
        lab-ish FQDNs (``*.htb``, ``*.local``, …).
        """
        from backend.host_proxy import host_proxy_mode

        hosts = self._challenge_probe_hosts()
        forced = host_proxy_mode() in ("1", "true", "yes", "on")

        hop = await self._exec_inner(
            'python3 -c "'
            "import re,pathlib,socket;"
            "c=pathlib.Path('/etc/proxychains4.conf').read_text();"
            "m=re.search(r'socks5\\s+(\\S+)\\s+(\\d+)',c);"
            "assert m, 'no socks line';"
            "socket.create_connection((m.group(1),int(m.group(2))),timeout=3).close();"
            "print('HOP_OK')\"",
            timeout_s=15,
            via_host_proxy=False,
        )
        hop_ok = "HOP_OK" in (hop.stdout or "") and bool(self._host_proxy_port)

        async def _use_direct(reason: str) -> None:
            self._host_proxy_wrap = False
            # Keep the SOCKS lease/port so sibling sandboxes still have a live
            # hop. Release only in stop() — releasing here under-counts leases.
            logger.info("Host VPN routing: DIRECT (%s)", reason)

        async def _use_socks(reason: str, *, warning: bool = False) -> None:
            self._host_proxy_wrap = True
            log = logger.warning if warning else logger.info
            log("Host VPN routing: SOCKS (%s) — agent bash uses proxychains4", reason)

        if forced and hop_ok:
            await _use_socks("CTF_HOST_PROXY forced on")
            return

        direct = await self._probe_direct_lab_access(hosts) if hosts else False
        if direct:
            extra = f"; challenge ports {self._challenge_ports}" if self._challenge_ports else ""
            await _use_direct(f"container reaches {', '.join(hosts[:3])}{extra}")
            return

        if hop_ok and hosts:
            socks_lab = await self._probe_socks_lab_access(hosts)
            if socks_lab:
                await _use_socks(f"reaches {', '.join(hosts[:3])}; direct miss")
                return
            # Both paths failed (filtered DROP, wrong VPN, bad hop). Do NOT wrap
            # every bash through a dead SOCKS path — prefer DIRECT + warn.
            await _use_direct(
                "lab probe failed on DIRECT and SOCKS — leaving unwrapped; "
                f"check VPN/machine for {', '.join(hosts[:3])}"
            )
            return

        if hop_ok and not hosts:
            await _use_direct("no lab hosts in challenge text; SOCKS hop idle (offline/local)")
            return

        # No usable SOCKS hop (and direct already failed or no hosts).
        await _use_direct("SOCKS unavailable; lab probe " + ("failed" if hosts else "skipped"))

    def _harden_nmap_command(self, command: str) -> str:
        """Harden agent nmap for Docker/VPN/SOCKS without dropping port ranges."""
        return harden_nmap_command(command)

    def _harden_hosts_edit_command(self, command: str) -> str:
        return harden_hosts_edit_command(command)

    async def _ensure_pip_break_system_wrapper(self) -> None:
        """Install /usr/local/bin/pip3 wrapper so bare `pip install` works (PEP 668).

        New core images ship this in Dockerfile.core; inject at start so older
        ctf-sandbox-core tags still behave. Idempotent.
        """
        from pathlib import Path

        wrapper = (
            Path(__file__).resolve().parents[2] / "sandbox" / "scripts" / "pip3_ctf_wrapper.sh"
        )
        try:
            body = wrapper.read_bytes()
        except OSError as e:
            logger.warning("pip3 wrapper script missing on host: %s", e)
            return
        try:
            await self._write_file_inner("/usr/local/bin/pip3", body)
            await self._exec_inner(
                "chmod +x /usr/local/bin/pip3 && ln -sfn pip3 /usr/local/bin/pip",
                timeout_s=15,
            )
        except Exception as e:
            logger.warning("Could not install pip3 PEP 668 wrapper: %s", e)

    async def refresh_tools_doc(self) -> None:
        from backend.tool_router import merged_tools_doc

        # Use _write_file_inner — callers often already hold ``_lock``.
        body = merged_tools_doc(self.image, self.ensured_packs).encode("utf-8")
        try:
            await self._write_file_inner("/tools.txt", body)
            await self._write_file_inner("/challenge/TOOLS.txt", body)
        except Exception as e:
            logger.warning("Could not refresh /tools.txt: %s", e)

    async def _pack_marker_present(self, pack_id: str) -> bool:
        from backend.tool_router import pack_marker_path

        marker = pack_marker_path(pack_id)
        result = await self._exec_inner(
            f"test -f {shlex.quote(marker)} && echo yes",
            timeout_s=15,
        )
        return result.exit_code == 0 and "yes" in result.stdout

    async def ensure_pack(self, pack_id: str, *, refresh_tools: bool = True) -> str:
        """Attach an L1 tool pack into this L0 container (additive, idempotent).

        Prefetched packs are usually RO bind-mounted from the host cache; late
        discovery falls back to docker cp. Return strings are agent-visible —
        keep them generic (no pack/image IDs).

        When ``refresh_tools`` is False (batch preflight), skip rewriting
        ``/tools.txt`` — caller refreshes once after all packs.
        """
        from backend.tool_router import PACK_SPECS

        pack_id = (pack_id or "").strip()
        if not pack_id:
            return "Nothing to install."
        if pack_id not in PACK_SPECS:
            return "Those tools are not available in this environment."

        # Fast path + decide whether long host-side work is needed — under lock.
        needs_cache = False
        async with self._lock:
            if not self._docker or not self._container:
                raise RuntimeError("Sandbox not started")
            if pack_id in self.ensured_packs or await self._pack_marker_present(pack_id):
                self.ensured_packs.add(pack_id)
                self._remember_pack_paths(pack_id)
                await self._maybe_raise_memory([pack_id])
                if refresh_tools:
                    await self.refresh_tools_doc()
                return "Required tools are already available."
            bound = pack_id in self._bind_mounted_packs
            needs_cache = bool(PACK_SPECS[pack_id].paths) and not bound

        # Donor build + host-cache extract can take many minutes — do them
        # outside the sandbox lock so bash/read/write stay responsive.
        from backend.sandbox.donor_build import DONOR_BUILD_SPECS, ensure_donor_image

        if pack_id in DONOR_BUILD_SPECS:
            await ensure_donor_image(pack_id)
        if needs_cache:
            try:
                await self._materialize_pack_cache(pack_id)
            except Exception:
                logger.warning(
                    "Pack %s host-cache materialize failed (will retry under lock)",
                    pack_id,
                    exc_info=True,
                )

        async with self._lock:
            return await self._ensure_pack_inner(pack_id, refresh_tools=refresh_tools)

    async def _ensure_pack_inner(self, pack_id: str, *, refresh_tools: bool = True) -> str:
        from backend.tool_router import (
            PACK_SPECS,
            bootstrap_script,
        )

        if not self._docker or not self._container:
            raise RuntimeError("Sandbox not started")

        if pack_id in self.ensured_packs or await self._pack_marker_present(pack_id):
            self.ensured_packs.add(pack_id)
            self._remember_pack_paths(pack_id)
            await self._maybe_raise_memory([pack_id])
            if refresh_tools:
                await self.refresh_tools_doc()
            return "Required tools are already available."

        spec = PACK_SPECS[pack_id]
        bound = pack_id in self._bind_mounted_packs
        # Prefer CLI inspect so presence matches donor_build / materialize
        # (aiodocker can disagree with DOCKER_HOST / currentContext).
        donor_available = True
        rc, _, _ = await _docker_cli("image", "inspect", spec.image, timeout_s=30)
        if rc != 0:
            donor_available = False
            try:
                await self._docker.images.inspect(spec.image)
                donor_available = True
            except Exception:
                pass

        # Consistency: packs with ``requires_donor`` need the donor image (or a
        # prior bind mount). Never mark ready after apt/pip alone — that falsely
        # advertises Ghidra/jadx/stegseek. Apt/pip-only packs use core + paths=().
        if not donor_available and spec.requires_donor and not bound:
            # ensure_pack already tried auto-build outside the lock; re-check CLI
            # (aiodocker inspect can disagree with DOCKER_HOST / CLI context).
            from backend.sandbox.donor_build import ensure_donor_image
            from backend.tool_router import donor_build_hint

            ok, build_msg = await ensure_donor_image(pack_id)
            if ok:
                logger.info("Donor auto-build ok for %s: %s", pack_id, build_msg)
                donor_available = True
            else:
                hint = donor_build_hint(pack_id)
                return (
                    "Required tools are not installed in this environment yet. "
                    f"{build_msg}{hint} "
                    "Ask an operator to finish sandbox setup, then retry the command."
                )
        if not donor_available and not bound:
            # Apt/pip pack whose declared image tag is missing — bootstrap on L0
            # if the live sandbox image is present (normally both are core).
            try:
                await self._docker.images.inspect(self.image)
            except Exception:
                from backend.sandbox.donor_build import DONOR_BUILD_SPECS, ensure_donor_image
                from backend.tool_router import donor_build_hint

                if pack_id in DONOR_BUILD_SPECS:
                    ok, build_msg = await ensure_donor_image(pack_id)
                    if ok:
                        donor_available = True
                    else:
                        hint = donor_build_hint(pack_id)
                        return (
                            "Required tools are not installed in this environment yet. "
                            f"{build_msg}{hint} "
                            "Ask an operator to finish sandbox setup, then retry the command."
                        )
                else:
                    hint = donor_build_hint(pack_id)
                    return (
                        "Required tools are not installed in this environment yet."
                        f"{hint} "
                        "Ask an operator to finish sandbox setup, then retry the command."
                    )
            if not donor_available:
                logger.warning(
                    "Pack image %s missing; apt/pip bootstrap on live %s",
                    spec.image,
                    self.image,
                )

        logger.info(
            "Ensuring pack '%s' additively into %s (%s)",
            pack_id,
            self.image,
            "bind-mounted" if bound else f"from donor {spec.image}",
        )

        try:
            # Raise memory before bootstrap — heavy packs OOM during pip otherwise.
            await self._maybe_raise_memory([pack_id])
            if spec.paths and not bound:
                cache = await self._materialize_pack_cache(pack_id)
                await self._copy_cache_into_container(cache, spec.paths)
            elif bound:
                logger.info(
                    "Pack %s trees already bind-mounted; running bootstrap only",
                    pack_id,
                )
            boot = bootstrap_script(pack_id)
            from backend.tool_router import pack_bootstrap_timeout_s

            boot_timeout = pack_bootstrap_timeout_s(pack_id)
            boot_result = await self._exec_inner(boot, timeout_s=boot_timeout)
            if boot_result.exit_code != 0:
                logger.warning(
                    "Pack %s bootstrap exit=%s stderr=%s",
                    pack_id,
                    boot_result.exit_code,
                    (boot_result.stderr or boot_result.stdout)[:500],
                )
                return (
                    "Failed to load additional tools "
                    f"(pack bootstrap exit {boot_result.exit_code}). "
                    "Ask an operator to check the sandbox setup, then retry."
                )
            if not await self._pack_marker_present(pack_id):
                logger.warning("Pack %s bootstrap finished but ready marker missing", pack_id)
                return (
                    "Failed to load additional tools (pack marker missing). "
                    "Ask an operator to check the sandbox setup, then retry."
                )
        except Exception as e:
            logger.error("Failed to ensure pack %s: %s", pack_id, e)
            return (
                "Failed to load additional tools. "
                "Ask an operator to check the sandbox setup, then retry."
            )

        self.ensured_packs.add(pack_id)
        self._remember_pack_paths(pack_id)
        await self._maybe_raise_memory([pack_id])
        if refresh_tools:
            await self.refresh_tools_doc()
        return (
            "Additional tools are now available. "
            "Workspace was preserved. Re-run your command, then re-read /tools.txt."
        )

    async def _maybe_raise_memory(self, packs: list[str]) -> None:
        """Bump live container Memory if pack floors exceed the current limit."""
        new = recommended_memory_limit(self.memory_limit, packs)
        if parse_memory_bytes(new) <= parse_memory_bytes(self.memory_limit):
            return
        old = self.memory_limit
        self.memory_limit = new
        if not self._container:
            return
        ok = await apply_live_memory(self._container.id, new)
        if not ok:
            self.memory_limit = old
            return
        logger.info("Raised live sandbox memory %s → %s for packs %s", old, new, packs)

    def _remember_pack_paths(self, pack_id: str) -> None:
        from backend.tool_router import PACK_SPECS

        spec = PACK_SPECS.get(pack_id)
        if not spec:
            return
        for d in spec.path_dirs:
            if d not in self.extra_path_dirs:
                self.extra_path_dirs.append(d)

    def _pack_state_bind_strings(self) -> list[str]:
        """RW host binds for pack scratch that must outlive the container.

        Mounted for every pack that declares ``state_dirs`` (not only prefetched
        ones) so an on-demand ``ensure_pack`` still reuses an earlier build.
        Session-scoped unless the pack sets ``shared_state`` (blutter Dart VMs).
        """
        from backend.platform_paths import docker_volume_path, ensure_docker_bind_dir
        from backend.tool_router import PACK_SPECS, pack_state_dir

        out: list[str] = []
        sid = (self.session_id or "").strip() or None
        for pack_id, spec in PACK_SPECS.items():
            for container_path in spec.state_dirs:
                host = pack_state_dir(pack_id, container_path, session_id=sid)
                try:
                    host.mkdir(parents=True, exist_ok=True)
                    ensure_docker_bind_dir(host)
                except OSError as e:
                    logger.warning("Pack %s state dir %s unusable: %s", pack_id, host, e)
                    continue
                out.append(f"{docker_volume_path(host)}:{container_path}:rw")
        return out

    def _pack_bind_strings(self, cache: Path, pack_id: str) -> list[str]:
        """Build Docker bind specs for a pack's host-cache trees."""
        from backend.platform_paths import docker_volume_path
        from backend.tool_router import PACK_SPECS, pack_cache_item

        spec = PACK_SPECS[pack_id]
        out: list[str] = []
        for src in spec.paths_to_bind():
            host = pack_cache_item(cache, src).resolve()
            if not host.exists():
                logger.warning("Pack %s bind skip missing %s", pack_id, host)
                continue
            out.append(f"{docker_volume_path(host)}:{src}:ro")
        return out

    async def _materialize_pack_cache(self, pack_id: str) -> Path:
        """Populate host cache from the pack donor image (once per arch).

        Concurrent solvers (separate processes) share one extract: the first
        holds a cross-process flock and copies from the donor; waiters block
        on the lock, then reuse ``.ready`` — never a second parallel extract.
        """
        from backend.tool_router import pack_cache_dir

        cache = pack_cache_dir(pack_id)
        # Fully prepared cache: safe to share with no lock / no second extract.
        if _pack_cache_is_ready(pack_id) and (cache / ".prepared").is_file():
            from backend.tool_router import evict_pack_cache, touch_pack_cache

            touch_pack_cache(pack_id)
            evict_pack_cache(protect={pack_id})
            return cache

        lock = await _pack_cache_lock(pack_id)
        async with lock:
            fd = await asyncio.to_thread(_acquire_pack_flock, pack_id)
            try:
                # Another process may have finished extract+prepare while we waited.
                if _pack_cache_is_ready(pack_id):
                    logger.info(
                        "Pack %s: cache already ready (shared; skipped extract)",
                        pack_id,
                    )
                    return await self._finish_ready_pack_cache(pack_id)
                return await self._materialize_pack_cache_unlocked(pack_id)
            finally:
                await asyncio.to_thread(_release_pack_flock, fd)

    async def _finish_ready_pack_cache(self, pack_id: str) -> Path:
        from backend.sandbox.setup_bake import maybe_upgrade_ready_marker
        from backend.tool_router import evict_pack_cache, pack_cache_dir, touch_pack_cache

        cache = pack_cache_dir(pack_id)
        await self._finalize_pack_cache(pack_id, cache)
        maybe_upgrade_ready_marker(pack_id)
        touch_pack_cache(pack_id)
        evict_pack_cache(protect={pack_id})
        return cache

    async def _materialize_pack_cache_unlocked(self, pack_id: str) -> Path:
        from backend.tool_router import PACK_SPECS, pack_cache_dir

        spec = PACK_SPECS[pack_id]
        cache = pack_cache_dir(pack_id)
        marker = cache / ".ready"

        cache.mkdir(parents=True, exist_ok=True)
        # Ensure donor exists before docker create (prefetch materialize path
        # used to fail hard when the image was missing).
        from backend.sandbox.donor_build import DONOR_BUILD_SPECS, ensure_donor_image

        if pack_id in DONOR_BUILD_SPECS:
            ok, build_msg = await ensure_donor_image(pack_id)
            if not ok:
                raise RuntimeError(build_msg)

        # One global extract container name per pack — safe because the
        # cross-process flock guarantees a single extractor.
        name = f"ctf-pack-extract-{pack_id}"
        await _docker_cli("rm", "-f", name, timeout_s=60)
        # Label it like a sandbox so orphan cleanup reclaims it if we are killed
        # mid-extract (docker cp of a big tree can run for minutes).
        rc, _, err = await _docker_cli(
            "create",
            "--name",
            name,
            "--label",
            f"{CONTAINER_LABEL}=true",
            "--label",
            f"{OWNER_PID_LABEL}={os.getpid()}",
            "--label",
            f"{SESSION_ID_LABEL}="
            f"{(os.environ.get('ARTEMIS_SESSION_ID') or '').strip() or '_default'}",
            spec.image,
            "sleep",
            "infinity",
        )
        if rc != 0:
            raise RuntimeError(f"docker create {spec.image} failed: {err.strip()}")
        try:
            for src in spec.paths:
                dest = cache / src.lstrip("/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Remove stale file/dir so docker cp can recreate.
                if dest.exists() or dest.is_symlink():
                    import shutil

                    if dest.is_dir() and not dest.is_symlink():
                        shutil.rmtree(dest, ignore_errors=True)
                    else:
                        dest.unlink(missing_ok=True)
                # Sage / large pack trees can exceed 300s on first extract.
                cp_timeout = 1800 if pack_id in ("crypto", "crypto-tools") else 300
                try:
                    await docker_cp_from_container(
                        name, src, dest, timeout_s=cp_timeout
                    )
                except RuntimeError as exc:
                    raise RuntimeError(
                        f"docker cp {src} from {spec.image} failed: {exc}"
                    ) from exc
            marker.write_text("ok\n", encoding="utf-8")
            try:
                from backend.sandbox.setup_bake import write_pack_ready_marker

                write_pack_ready_marker(cache, pack_id)
            except Exception:
                logger.debug("write_pack_ready_marker failed", exc_info=True)
            logger.info("Pack %s: host cache materialized at %s", pack_id, cache)
        finally:
            await _docker_cli("rm", "-f", name, timeout_s=60)

        return await self._finish_ready_pack_cache(pack_id)

    async def _finalize_pack_cache(self, pack_id: str, cache: Path) -> None:
        """One-time host-cache prep (wrappers, Sage pip) before RO binds.

        Idempotent via ``.prepared``. Safe to call under the pack cache lock.
        """
        prepared = cache / ".prepared"
        if prepared.is_file():
            return

        ok = True
        if pack_id == "crypto":
            ok = await self._finalize_crypto_pack_cache(cache)
        if ok:
            prepared.write_text("ok\n", encoding="utf-8")

    async def _finalize_crypto_pack_cache(self, cache: Path) -> bool:
        """Fix sage wrappers on host + install pycryptodome into the Sage tree.

        Returns True when the cache is ready for RO binds (or there is nothing
        to prepare). Returns False so the next call can retry a failed pip.
        """
        sage_root = cache / "opt" / "sagemath"
        wrappers = (
            (
                cache / "usr" / "local" / "bin" / "sage",
                '#!/bin/bash\nexec /opt/sagemath/bin/sage "$@"\n',
            ),
            (
                cache / "usr" / "local" / "bin" / "sage-python",
                '#!/bin/bash\nexec /opt/sagemath/bin/python3 "$@"\n',
            ),
        )
        for path, body in wrappers:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        py = sage_root / "bin" / "python3"
        if not py.is_file() and not py.is_symlink():
            logger.warning("Crypto pack cache missing Sage python at %s", py)
            return True

        if self._sage_tree_has_pycryptodome(sage_root):
            return True

        # Linux Sage cannot run on macOS host — use a short-lived container with
        # the cache tree mounted RW so RO binds see pycryptodome already installed.
        from backend.tool_router import PACK_SPECS

        donor = PACK_SPECS["crypto"].image
        images = [donor]
        if getattr(self, "image", None):
            images.append(self.image)
        images.extend(["ctf-sandbox-core", "ubuntu:24.04"])
        seen: set[str] = set()
        last_err = ""
        for image in images:
            if not image or image in seen:
                continue
            seen.add(image)
            rc, out, err = await _docker_cli(
                "run",
                "--rm",
                "-v",
                f"{sage_root.resolve()}:/opt/sagemath",
                "--entrypoint",
                "/opt/sagemath/bin/python3",
                image,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "pycryptodome",
                timeout_s=600,
            )
            if rc == 0 or self._sage_tree_has_pycryptodome(sage_root):
                logger.info(
                    "Crypto pack cache: pycryptodome ready in Sage (via %s)",
                    image,
                )
                return True
            last_err = (err or out).strip()[:300]
            if "Unable to find image" in last_err or "pull access denied" in last_err:
                continue
            logger.warning(
                "Crypto pack cache: pip via %s failed (rc=%s): %s",
                image,
                rc,
                last_err,
            )
        logger.warning(
            "Crypto pack cache: could not install pycryptodome into Sage (%s)",
            last_err or "no usable image",
        )
        return False

    @staticmethod
    def _sage_tree_has_pycryptodome(sage_root: Path) -> bool:
        """Cheap check for Crypto under a extracted Sage tree (no docker)."""
        patterns = (
            "lib/python*/site-packages/Crypto/__init__.py",
            "local/lib/python*/site-packages/Crypto/__init__.py",
            "lib/python*/site-packages/Cryptodome/__init__.py",
        )
        return any(any(sage_root.glob(pattern)) for pattern in patterns)

    async def _copy_cache_into_container(self, cache: Path, paths: tuple[str, ...]) -> None:
        cid = self.container_id
        for src in paths:
            host_src = cache / src.lstrip("/")
            if not host_src.exists():
                raise FileNotFoundError(f"Pack cache missing {host_src}")
            # Ensure parent exists in container, then docker cp into parent.
            parent = str(Path(src).parent)
            await self._exec_inner(f"mkdir -p {shlex.quote(parent)}", timeout_s=30)
            cp_timeout = 1800 if "sagemath" in src or src.startswith("/opt/sagemath") else 300
            rc, _, err = await _docker_cli(
                "cp", str(host_src), f"{cid}:{src}", timeout_s=cp_timeout
            )
            if rc != 0:
                # Some docker versions want cp into the parent directory.
                rc2, _, err2 = await _docker_cli(
                    "cp",
                    str(host_src),
                    f"{cid}:{parent}/",
                    timeout_s=cp_timeout,
                )
                if rc2 != 0:
                    raise RuntimeError(
                        f"docker cp into sandbox failed for {src}: {err.strip()} | {err2.strip()}"
                    )

    async def exec(self, command: str, timeout_s: int = 300) -> ExecResult:
        if not self.workspace_dir:
            raise RuntimeError("Sandbox not started")

        async with self._lock:
            await self._ensure_container_unlocked()
            try:
                return await self._exec_inner(command, timeout_s, via_host_proxy=True)
            except aiodocker.exceptions.DockerError as e:
                if self._is_container_gone_error(e):
                    logger.warning("exec hit gone container — recreating and retrying once")
                    await self._recreate_container_unlocked()
                    try:
                        return await self._exec_inner(command, timeout_s, via_host_proxy=True)
                    except Exception as e2:
                        return ExecResult(
                            exit_code=-1,
                            stdout="",
                            stderr=f"Container recreate failed: {e2}",
                        )
                return ExecResult(exit_code=-1, stdout="", stderr=f"Docker error: {e}")

    async def _exec_inner(
        self,
        command: str,
        timeout_s: int,
        *,
        via_host_proxy: bool = False,
    ) -> ExecResult:
        import re

        # Prefer TERM so timed-out commands exit 124 (not 137 from SIGKILL).
        # KILL after 5s is only a safety net for stuck processes.
        # Prepend pack PATH dirs (non-login bash -c does not read profile.d).
        # Full-port connect scans need wall time; don't let a short agent
        # timeout silently amputate discovery (custom ports included).
        if (
            via_host_proxy
            and timeout_s < 900
            and re.search(r"(?<!\S)nmap\b", command)
            and re.search(
                r"(?<!\S)-p-(?!\S)"
                r"|(?<!\S)-p\s+-(?!\S)"
                r"|(?<!\S)-p\s*0*1\s*-\s*65535(?!\S)"
                r"|(?<!\S)-p0*1-65535(?!\S)",
                command,
            )
        ):
            logger.info(
                "nmap full-port sweep: raising timeout %ss → 900s",
                timeout_s,
            )
            timeout_s = 900
        path_prefix = ""
        if self.extra_path_dirs:
            joined = ":".join(self.extra_path_dirs)
            path_prefix = f"export PATH={shlex.quote(joined)}:$PATH; "
        command = self._harden_nmap_command(command)
        command = self._harden_hosts_edit_command(command)
        inner = path_prefix + command
        runner = f"bash -c {shlex.quote(inner)}"
        # Route agent traffic through host SOCKS only when calibration said so.
        # Internal pack/bootstrap calls keep via_host_proxy=False.
        if (
            via_host_proxy
            and self._host_proxy_wrap
            and self._host_proxy_port
            and "proxychains4" not in command
        ):
            # pipefail so `nmap | head` still surfaces nmap failures for pack ensure
            runner = (
                "proxychains4 -q -f /etc/proxychains4.conf "
                f"bash -o pipefail -c {shlex.quote(inner)}"
            )
        wrapped = f"timeout --signal=TERM --kill-after=5 {timeout_s} {runner}"
        exec_instance = await self._container.exec(
            cmd=["bash", "-c", wrapped],
            stdout=True,
            stderr=True,
            tty=False,
        )

        stream = exec_instance.start(detach=False)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _collect() -> None:
            while True:
                msg = await stream.read_out()
                if msg is None:
                    break
                if msg.stream == 1:
                    stdout_chunks.append(msg.data)
                else:
                    stderr_chunks.append(msg.data)

        try:
            # Give extra margin beyond the container-side timeout
            await asyncio.wait_for(_collect(), timeout=timeout_s + 30)
        except TimeoutError:
            try:
                await stream.close()
            except Exception:
                pass
            return ExecResult(
                exit_code=-1,
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr="Command timed out",
            )

        inspect = await exec_instance.inspect()
        exit_code = inspect.get("ExitCode", 0)

        return ExecResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> str | bytes:
        """Read a file from the container. Returns str for text, bytes for binary."""
        if not self.workspace_dir:
            raise RuntimeError("Sandbox not started")

        async with self._lock:
            await self._ensure_container_unlocked()
            try:
                return await self._read_file_inner(path)
            except Exception as e:
                if self._is_container_gone_error(e):
                    logger.warning("read_file hit gone container — recreating and retrying once")
                    await self._recreate_container_unlocked()
                    return await self._read_file_inner(path)
                raise

    async def _read_file_inner(self, path: str) -> str | bytes:
        assert self._container is not None
        try:
            tar = await asyncio.wait_for(
                self._container.get_archive(path),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out reading {path}") from e

        with tar:
            for member in tar:
                if member.isfile():
                    f = tar.extractfile(member)
                    if f:
                        data = f.read()
                        try:
                            return data.decode("utf-8")
                        except UnicodeDecodeError:
                            return data
        raise FileNotFoundError(f"No file found at {path}")

    async def read_file_bytes(self, path: str) -> bytes:
        """Read a file from the container as raw bytes."""
        result = await self.read_file(path)
        if isinstance(result, str):
            return result.encode("utf-8")
        return result

    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write a file into the container via tar archive."""
        if not self.workspace_dir:
            raise RuntimeError("Sandbox not started")

        if isinstance(content, str):
            content = content.encode("utf-8")

        async with self._lock:
            await self._ensure_container_unlocked()
            try:
                await self._write_file_inner(path, content)
            except Exception as e:
                if self._is_container_gone_error(e):
                    logger.warning("write_file hit gone container — recreating and retrying once")
                    await self._recreate_container_unlocked()
                    await self._write_file_inner(path, content)
                    return
                raise

    async def _write_file_inner(self, path: str, content: bytes) -> None:
        assert self._container is not None
        parent, name = container_path_parts(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buf.seek(0)

        try:
            await asyncio.wait_for(
                self._container.put_archive(parent, buf.getvalue()),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out writing {path}") from e

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        if self._container:
            try:
                await self._container.delete(force=True)
            except Exception:
                pass
            self._container = None
            await _track_stop()

        if self._docker:
            try:
                await self._docker.close()
            except Exception:
                pass
            self._docker = None

        if self._host_proxy_port is not None:
            from backend.host_proxy import release_host_proxy

            try:
                await release_host_proxy()
            except Exception:
                pass
            self._host_proxy_port = None
        self._host_proxy_wrap = False

        if self.workspace_dir:
            import shutil

            try:
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            except Exception:
                pass
            self.workspace_dir = ""
        if self._temp_dirs:
            import shutil

            for d in self._temp_dirs:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
            self._temp_dirs.clear()
        logger.info("Sandbox stopped")
