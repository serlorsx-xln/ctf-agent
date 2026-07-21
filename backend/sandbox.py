"""Docker sandbox for CTF challenge solving — native async via aiodocker."""

from __future__ import annotations

import asyncio
import fcntl
import io
import logging
import os
import shlex
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiodocker

logger = logging.getLogger(__name__)

CONTAINER_LABEL = "ctf-agent"
OWNER_PID_LABEL = "ctf-agent.owner-pid"

# Concurrency control
_start_semaphore: asyncio.Semaphore | None = None
_active_count: int = 0
_count_lock = asyncio.Lock()

_WARN_THRESHOLDS = {100, 200, 500}

# Serialize host pack-cache extract/finalize per pack_id (same process).
_pack_cache_locks: dict[str, asyncio.Lock] = {}
_pack_cache_locks_mu = asyncio.Lock()


async def _pack_cache_lock(pack_id: str) -> asyncio.Lock:
    async with _pack_cache_locks_mu:
        lock = _pack_cache_locks.get(pack_id)
        if lock is None:
            lock = asyncio.Lock()
            _pack_cache_locks[pack_id] = lock
        return lock


def _pack_cache_lock_path(pack_id: str) -> Path:
    """Cross-process lock file: one extract per pack_id globally."""
    from backend.tool_router import pack_cache_root

    d = pack_cache_root() / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d / ".extract.lock"


def _acquire_pack_flock(pack_id: str) -> int:
    """Block until this process owns exclusive extract rights for ``pack_id``."""
    path = _pack_cache_lock_path(pack_id)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    logger.info("Pack %s: waiting for cross-process extract lock (%s)", pack_id, path)
    fcntl.flock(fd, fcntl.LOCK_EX)
    logger.info("Pack %s: acquired cross-process extract lock", pack_id)
    return fd


def _release_pack_flock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _pack_cache_is_ready(pack_id: str) -> bool:
    from backend.tool_router import PACK_SPECS, pack_cache_dir

    spec = PACK_SPECS.get(pack_id)
    if not spec:
        return False
    cache = pack_cache_dir(pack_id)
    marker = cache / ".ready"
    return marker.is_file() and all((cache / p.lstrip("/")).exists() for p in spec.paths)


def _docker_client() -> aiodocker.Docker:
    """Connect like the Docker CLI: DOCKER_HOST wins over ~/.docker currentContext.

    aiodocker prefers currentContext (e.g. desktop-linux) over DOCKER_HOST, which
    breaks Colima setups where the CLI sees images but the agent does not.
    """
    host = os.environ.get("DOCKER_HOST")
    if host:
        return aiodocker.Docker(url=host)
    return aiodocker.Docker()


def configure_semaphore(max_concurrent: int = 50) -> None:
    """Set the max concurrent container starts. Call once at startup."""
    global _start_semaphore
    _start_semaphore = asyncio.Semaphore(max_concurrent)


async def _track_start() -> None:
    global _active_count
    async with _count_lock:
        _active_count += 1
        if _active_count in _WARN_THRESHOLDS:
            logger.warning("Active containers: %d", _active_count)


async def _track_stop() -> None:
    global _active_count
    async with _count_lock:
        _active_count = max(0, _active_count - 1)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def cleanup_orphan_containers() -> None:
    """Remove leftover ctf-agent containers without killing concurrent solvers.

    Containers labeled with a live owner PID are left alone so two ``ctf-solve``
    processes can run at once. Unlabeled running containers are also kept
    (legacy / in-flight). Only true orphans (dead owner, or exited unlabeled)
    are force-deleted.
    """
    try:
        docker = _docker_client()
        try:
            containers = await docker.containers.list(
                all=True,
                filters={"label": [CONTAINER_LABEL]},
            )
            removed = 0
            skipped = 0
            for c in containers:
                try:
                    info = await c.show()
                    labels = (info.get("Config") or {}).get("Labels") or {}
                    owner = (labels.get(OWNER_PID_LABEL) or "").strip()
                    status = ((info.get("State") or {}).get("Status") or "").lower()
                    if owner.isdigit() and _pid_alive(int(owner)):
                        skipped += 1
                        continue
                    if not owner and status in {"running", "created", "restarting"}:
                        # No owner label but still live — likely a concurrent run
                        # started before owner labeling; do not steal it.
                        skipped += 1
                        continue
                    await c.delete(force=True)
                    removed += 1
                except Exception:
                    pass
            if removed:
                logger.info(
                    "Cleaned up %d orphan container(s) (kept %d live)",
                    removed,
                    skipped,
                )
        finally:
            await docker.close()
    except Exception as e:
        logger.warning("Orphan cleanup failed: %s", e)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


def parse_challenge_network_hints(text: str) -> tuple[list[str], list[int]]:
    """Extract lab hosts (IPs + lab-ish FQDNs) and TCP ports from challenge text.

    Generic patterns only — not challenge-specific service lists.
    Hostnames are included so Mac+VPN calibration can probe when no RFC1918 IP
    is pasted (common Assumed Breach writeups with only ``dc.lab.htb``).
    """
    import re

    hosts: list[str] = []
    seen_h: set[str] = set()

    def _add_host(h: str) -> None:
        h = h.strip().rstrip(".").lower()
        if not h or h in seen_h or h.startswith("127."):
            return
        if h in {"localhost", "example.com", "example.org"}:
            return
        # Docker Desktop / Colima gateway — not a remote VPN lab.
        if h == "host.docker.internal" or h.endswith(".docker.internal"):
            return
        seen_h.add(h)
        hosts.append(h)

    for m in re.finditer(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b",
        text,
    ):
        _add_host(m.group(0))

    # Lab-ish DNS names (htb/thm/local/…) — not a general TLD grab.
    for m in re.finditer(
        r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
        r"(?:htb|thm|local|lab|internal|lan|corp|vuln|offline)\b",
        text,
        flags=re.I,
    ):
        _add_host(m.group(0))

    # nc host port / connect host port
    for m in re.finditer(
        r"\bnc\s+([A-Za-z0-9._-]+)\s+(\d{2,5})\b",
        text,
        flags=re.I,
    ):
        _add_host(m.group(1))

    ports: list[int] = []
    seen_p: set[int] = set()

    def _add_port(raw: str) -> None:
        try:
            p = int(raw)
        except ValueError:
            return
        if 1 <= p <= 65535 and p not in seen_p:
            seen_p.add(p)
            ports.append(p)

    # host:port / :port after an IP or hostname
    for m in re.finditer(
        r"(?:(?:10|172|192)\.[\d.]+|localhost|127\.0\.0\.1|[A-Za-z0-9._-]+\.(?:htb|thm|local|lab))"
        r":(\d{1,5})\b",
        text,
        flags=re.I,
    ):
        _add_port(m.group(1))

    for m in re.finditer(
        r"\bnc\s+[A-Za-z0-9._-]+\s+(\d{2,5})\b",
        text,
        flags=re.I,
    ):
        _add_port(m.group(1))

    # port 31337 / ports: 80, 443, 31337 / tcp/31337 / TCP 31337
    for m in re.finditer(
        r"\b(?:ports?|tcp|udp)\s*[#:=/\-]?\s*(\d{1,5}(?:\s*,\s*\d{1,5})*)\b",
        text,
        flags=re.I,
    ):
        for part in re.split(r"\s*,\s*", m.group(1)):
            _add_port(part)

    return hosts[:8], ports[:32]


# Common services + a few often-closed sentinels (REFUSED ⇒ host is routed).
_DEFAULT_PROBE_PORTS: tuple[int, ...] = (
    22,
    80,
    443,
    445,
    3389,
    5985,
    8080,
    8443,
    8000,
    3000,
    1,
    65535,
)


def _lab_probe_script(hosts: list[str], ports: list[int], ok_token: str) -> str:
    """TCP probe: open OR connection-refused both mean the lab route works."""
    hosts_py = ",".join(repr(h) for h in hosts)
    ports_py = ",".join(str(p) for p in ports)
    fail_token = ok_token.replace("OK", "FAIL")
    return f"""
import errno, socket
hosts=[{hosts_py}]
ports=[{ports_py}]
for h in hosts:
    for p in ports:
        try:
            s=socket.create_connection((h,p), timeout=4)
            s.close()
            print({ok_token!r}, h, p, 'open')
            raise SystemExit(0)
        except ConnectionRefusedError:
            print({ok_token!r}, h, p, 'refused')
            raise SystemExit(0)
        except OSError as e:
            if getattr(e, 'errno', None) in (errno.ECONNREFUSED, 111, 61):
                print({ok_token!r}, h, p, 'refused')
                raise SystemExit(0)
        except Exception:
            pass
print({fail_token!r})
"""


def harden_nmap_command(command: str) -> str:
    """Make agent nmap reliable through Docker/VPN/SOCKS.

    Force ``-Pn`` and TCP connect ``-sT`` (SYN scans often lie in containers).
    Rewrite explicit ``-sS`` to ``-sT``. Does **not** rewrite port ranges —
    custom / full-port scans stay intact on every routing path.
    """
    import re

    # Only when nmap is invoked as a command (not `echo nmap`).
    if not (re.match(r"nmap\b", command.lstrip()) or re.search(r"[\n;|&]\s*nmap\b", command)):
        return command

    # SYN needs raw sockets; rewrite to connect scan. Leave -sU / -sV / etc.
    command = re.sub(r"(?<!\S)-sS\b", "-sT", command)

    # Real scan types only — not -sV/-sC (version/scripts).
    has_scan = re.search(r"(?<!\S)-s(?:T|S|A|W|M|U|Y|Z|O|N|F|X)\b", command)
    has_pn = re.search(r"(?<!\S)-Pn\b", command)
    list_or_ping = re.search(r"(?<!\S)-s[nL]\b", command)

    insert: list[str] = []
    if not has_pn and not list_or_ping:
        insert.append("-Pn")
    if not has_scan and not list_or_ping:
        insert.append("-sT")
    if not insert:
        return command

    flags = " ".join(insert)

    def _repl_head(m: re.Match[str]) -> str:
        return f"{m.group(1)}nmap {flags}"

    if re.match(r"nmap\b", command.lstrip()):
        leading = command[: len(command) - len(command.lstrip())]
        return leading + re.sub(r"nmap\b", f"nmap {flags}", command.lstrip(), count=1)
    return re.sub(r"([\n;|&]\s*)nmap\b", _repl_head, command, count=1)


def harden_hosts_edit_command(command: str) -> str:
    """Rewrite in-place ``sed -i … /etc/hosts`` to a temp-file rewrite.

    Docker bind-mounts ``/etc/hosts``; ``sed -i`` fails with
    ``Device or resource busy``.
    """
    import re

    if "/etc/hosts" not in command or "sed" not in command:
        return command
    if "ctf-hosts-add" in command:
        return command

    # Match a sed -i (optional suffix) … /etc/hosts invocation
    pattern = re.compile(
        r"(?P<head>^|[\n;|&]\s*)sed\s+-i\S*\s+(?P<body>.+?)\s+/etc/hosts\b",
    )

    def _rewrite(m: re.Match[str]) -> str:
        head = m.group("head")
        body = m.group("body").strip()
        # body is typically "'s/foo/bar/'" or similar sed script + optional args
        return (
            f'{head}tmp=$(mktemp) && sed {body} /etc/hosts > "$tmp" '
            f'&& cat "$tmp" > /etc/hosts && rm -f "$tmp"'
        )

    return pattern.sub(_rewrite, command, count=1)


async def _docker_cli(*args: str, timeout_s: float = 600) -> tuple[int, str, str]:
    """Run the host `docker` CLI (respects DOCKER_HOST)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"docker {' '.join(args)} timed out after {timeout_s}s"
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


@dataclass
class DockerSandbox:
    """Isolated Docker container for a single solver agent."""

    image: str
    challenge_dir: str
    memory_limit: str = "16g"
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
        """True when Docker reports the container no longer exists."""
        text = str(exc).lower()
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
            "NanoCpus": int(2 * 1e9),
        }

    async def start(self) -> None:
        sem = _start_semaphore or asyncio.Semaphore(50)
        async with sem:
            self._docker = _docker_client()

            self.workspace_dir = tempfile.mkdtemp(prefix="ctf-workspace-")

            challenge_root = Path(self.challenge_dir).resolve()
            from backend.challenge import distfiles_host_path
            from backend.tool_router import detect_packs, pack_binds_enabled

            dist_host = distfiles_host_path(challenge_root)
            binds: list[str] = [f"{self.workspace_dir}:/challenge/workspace:rw"]
            if dist_host is not None:
                binds.append(f"{dist_host}:/challenge/distfiles:ro")
            else:
                # Web/link-only: empty distfiles so the path still exists in-container.
                empty_dist = tempfile.mkdtemp(prefix="ctf-dist-empty-")
                self._temp_dirs.append(empty_dist)
                binds.append(f"{empty_dist}:/challenge/distfiles:ro")
            for name in (
                "challenge.txt",
                "challenge.md",
                "description.txt",
                "description.md",
                "README.md",
            ):
                p = challenge_root / name
                if p.is_file():
                    binds.append(f"{p}:/challenge/{p.name}:ro")

            # Prefetch packs before create so large trees can share RO host binds.
            self._bind_mounted_packs = set()
            prefetch = detect_packs(self.challenge_dir)
            from backend.tool_router import recommended_memory_limit

            bumped = recommended_memory_limit(self.memory_limit, prefetch)
            if bumped != self.memory_limit:
                logger.info(
                    "Raising sandbox memory %s → %s for packs %s",
                    self.memory_limit,
                    bumped,
                    sorted(prefetch),
                )
                self.memory_limit = bumped
            if pack_binds_enabled():
                for pack in prefetch:
                    try:
                        cache = await self._materialize_pack_cache(pack)
                        pack_binds = self._pack_bind_strings(cache, pack)
                        if pack_binds:
                            binds.extend(pack_binds)
                            self._bind_mounted_packs.add(pack)
                            logger.info(
                                "Pack %s: %d RO bind(s) from host cache %s",
                                pack,
                                len(pack_binds),
                                cache,
                            )
                    except Exception as e:
                        logger.warning(
                            "Pack %s bind prep failed (will docker cp on ensure): %s",
                            pack,
                            e,
                        )

            self._binds = binds

            self.image = await self._resolve_l0_image(self.image)

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
                logger.info(
                    "Host VPN routing: DIRECT (no remote lab hosts; skipping SOCKS)"
                )
            self._host_proxy_wrap = False
            try:
                await self._create_and_start(self.image)
                if self._host_proxy_port:
                    await self._install_host_proxy_client(self._host_proxy_port)
                    await self._calibrate_host_proxy_routing()
                await self.refresh_tools_doc()

                # Bootstrap prefetched packs (bind-mounted trees skip the copy).
                try:
                    for pack in prefetch:
                        logger.info(
                            "Prefetch bootstrap pack=%s (apt/pip may take 1–2 min "
                            "on a fresh container)…",
                            pack,
                        )
                        msg = await self.ensure_pack(pack)
                        logger.info("Prefetch pack %s: %s", pack, msg)
                except Exception as e:
                    logger.warning("Pack prefetch failed: %s", e)
            except Exception:
                if self._host_proxy_port is not None:
                    await release_host_proxy()
                    self._host_proxy_port = None
                    self._host_proxy_wrap = False
                raise

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
            },
            "HostConfig": self._host_config(),
        }
        self._container = await self._docker.containers.create(config)
        await self._container.start()
        self.image = image
        await _track_start()
        info = await self._container.show()
        short_id = info["Id"][:12]
        logger.info("Sandbox started: %s (image=%s)", short_id, image)

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
        from backend.host_proxy import host_proxy_mode, release_host_proxy

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
            if self._host_proxy_port is not None:
                await release_host_proxy()
                self._host_proxy_port = None
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

    async def ensure_pack(self, pack_id: str) -> str:
        """Attach an L1 tool pack into this L0 container (additive, idempotent).

        Prefetched packs are usually RO bind-mounted from the host cache; late
        discovery falls back to docker cp. Return strings are agent-visible —
        keep them generic (no pack/image IDs).
        """
        from backend.tool_router import PACK_SPECS

        pack_id = (pack_id or "").strip()
        if not pack_id:
            return "Nothing to install."
        if pack_id not in PACK_SPECS:
            return "Those tools are not available in this environment."

        async with self._lock:
            return await self._ensure_pack_inner(pack_id)

    async def _ensure_pack_inner(self, pack_id: str) -> str:
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
            await self.refresh_tools_doc()
            return "Required tools are already available."

        spec = PACK_SPECS[pack_id]
        bound = pack_id in self._bind_mounted_packs
        try:
            await self._docker.images.inspect(spec.image)
        except Exception:
            # Crypto-style packs may use L0 as donor — fall back to the live image.
            try:
                await self._docker.images.inspect(self.image)
                logger.warning(
                    "Pack donor %s missing; using live image %s",
                    spec.image,
                    self.image,
                )
            except Exception:
                # Bind-mounted packs can still bootstrap without the donor image.
                if not bound:
                    logger.warning(
                        "Pack donor image missing: pack=%s image=%s",
                        pack_id,
                        spec.image,
                    )
                    from backend.tool_router import donor_build_hint

                    hint = donor_build_hint(pack_id)
                    return (
                        "Required tools are not installed in this environment yet."
                        f"{hint} "
                        "Ask an operator to finish sandbox setup, then retry the command."
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
        await self.refresh_tools_doc()
        return (
            "Additional tools are now available. "
            "Workspace was preserved. Re-run your command, then re-read /tools.txt."
        )

    async def _maybe_raise_memory(self, packs: list[str]) -> None:
        """Bump live container Memory if pack floors exceed the current limit."""
        from backend.tool_router import parse_memory_bytes, recommended_memory_limit

        new = recommended_memory_limit(self.memory_limit, packs)
        if parse_memory_bytes(new) <= parse_memory_bytes(self.memory_limit):
            return
        old = self.memory_limit
        self.memory_limit = new
        if not self._container:
            return
        cid = self._container.id
        rc, _, err = await _docker_cli(
            "update",
            f"--memory={new}",
            f"--memory-swap={new}",
            cid,
            timeout_s=60,
        )
        if rc != 0:
            logger.warning(
                "Could not raise container memory %s → %s: %s",
                old,
                new,
                (err or "").strip()[:300],
            )
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

    def _pack_bind_strings(self, cache: Path, pack_id: str) -> list[str]:
        """Build Docker bind specs for a pack's host-cache trees."""
        from backend.tool_router import PACK_SPECS, pack_cache_item

        spec = PACK_SPECS[pack_id]
        out: list[str] = []
        for src in spec.paths_to_bind():
            host = pack_cache_item(cache, src).resolve()
            if not host.exists():
                logger.warning("Pack %s bind skip missing %s", pack_id, host)
                continue
            out.append(f"{host}:{src}:ro")
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
        from backend.tool_router import evict_pack_cache, pack_cache_dir, touch_pack_cache

        cache = pack_cache_dir(pack_id)
        await self._finalize_pack_cache(pack_id, cache)
        touch_pack_cache(pack_id)
        evict_pack_cache(protect={pack_id})
        return cache

    async def _materialize_pack_cache_unlocked(self, pack_id: str) -> Path:
        from backend.tool_router import PACK_SPECS, pack_cache_dir

        spec = PACK_SPECS[pack_id]
        cache = pack_cache_dir(pack_id)
        marker = cache / ".ready"

        cache.mkdir(parents=True, exist_ok=True)
        # One global extract container name per pack — safe because the
        # cross-process flock guarantees a single extractor.
        name = f"ctf-pack-extract-{pack_id}"
        await _docker_cli("rm", "-f", name, timeout_s=60)
        rc, _, err = await _docker_cli("create", "--name", name, spec.image, "sleep", "infinity")
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
                rc, _, err = await _docker_cli(
                    "cp", f"{name}:{src}", str(dest), timeout_s=cp_timeout
                )
                if rc != 0:
                    raise RuntimeError(f"docker cp {src} from {spec.image} failed: {err.strip()}")
            marker.write_text("ok\n", encoding="utf-8")
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
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=Path(path).name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buf.seek(0)

        try:
            await asyncio.wait_for(
                self._container.put_archive(str(Path(path).parent), buf.getvalue()),
                timeout=30,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Timed out writing {path}") from e

    async def stop(self) -> None:
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
