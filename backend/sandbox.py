"""Docker sandbox for CTF challenge solving — native async via aiodocker."""

from __future__ import annotations

import asyncio
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


async def cleanup_orphan_containers() -> None:
    """Kill any leftover ctf-agent containers from a previous run."""
    try:
        docker = aiodocker.Docker()
        try:
            containers = await docker.containers.list(
                all=True,
                filters={"label": [CONTAINER_LABEL]},
            )
            for c in containers:
                try:
                    await c.delete(force=True)
                except Exception:
                    pass
            if containers:
                logger.info("Cleaned up %d orphan container(s)", len(containers))
        finally:
            await docker.close()
    except Exception as e:
        logger.warning("Orphan cleanup failed: %s", e)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


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
        if status == 404:
            return True
        return False

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
            self._docker = aiodocker.Docker()
        self.ensured_packs.clear()
        self.extra_path_dirs.clear()
        # Keep self._binds and self._bind_mounted_packs — recreate remounts them.
        await self._create_and_start(self.image)
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
            self._docker = aiodocker.Docker()

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
            await self._create_and_start(self.image)
            await self.refresh_tools_doc()

            # Bootstrap prefetched packs (bind-mounted trees skip the copy).
            try:
                for pack in prefetch:
                    msg = await self.ensure_pack(pack)
                    logger.info("Prefetch pack %s: %s", pack, msg)
            except Exception as e:
                logger.warning("Pack prefetch failed: %s", e)

    async def _resolve_l0_image(self, preferred: str) -> str:
        """Use preferred L0 if present; else core; else deprecated fat image."""
        from backend.tool_router import DEFAULT_L0_CANDIDATES, LEGACY_FAT_L0

        candidates: list[str] = []
        if preferred:
            candidates.append(preferred)
        for c in DEFAULT_L0_CANDIDATES:
            if c not in candidates:
                candidates.append(c)
        if LEGACY_FAT_L0 not in candidates:
            candidates.append(LEGACY_FAT_L0)

        assert self._docker is not None
        last_err: Exception | None = None
        for img in candidates:
            try:
                await self._docker.images.inspect(img)
                if img == LEGACY_FAT_L0 and preferred != LEGACY_FAT_L0:
                    logger.warning(
                        "Using deprecated fat image %s — prefer ctf-sandbox-core + packs",
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
            "Labels": {CONTAINER_LABEL: "true"},
            "HostConfig": self._host_config(),
        }
        self._container = await self._docker.containers.create(config)
        await self._container.start()
        self.image = image
        await _track_start()
        info = await self._container.show()
        short_id = info["Id"][:12]
        logger.info("Sandbox started: %s (image=%s)", short_id, image)

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
        """Populate host cache from the pack donor image (once per arch)."""
        lock = await _pack_cache_lock(pack_id)
        async with lock:
            return await self._materialize_pack_cache_unlocked(pack_id)

    async def _materialize_pack_cache_unlocked(self, pack_id: str) -> Path:
        from backend.tool_router import PACK_SPECS, pack_cache_dir

        spec = PACK_SPECS[pack_id]
        cache = pack_cache_dir(pack_id)
        marker = cache / ".ready"
        if marker.is_file() and all(
            (cache / p.lstrip("/")).exists() for p in spec.paths
        ):
            await self._finalize_pack_cache(pack_id, cache)
            from backend.tool_router import evict_pack_cache, touch_pack_cache

            touch_pack_cache(pack_id)
            evict_pack_cache(protect={pack_id})
            return cache

        cache.mkdir(parents=True, exist_ok=True)
        name = f"ctf-pack-extract-{pack_id}-{os.getpid()}"
        rc, _, err = await _docker_cli(
            "create", "--name", name, spec.image, "sleep", "infinity"
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
                rc, _, err = await _docker_cli(
                    "cp", f"{name}:{src}", str(dest), timeout_s=cp_timeout
                )
                if rc != 0:
                    raise RuntimeError(
                        f"docker cp {src} from {spec.image} failed: {err.strip()}"
                    )
            marker.write_text("ok\n", encoding="utf-8")
        finally:
            await _docker_cli("rm", "-f", name, timeout_s=60)

        await self._finalize_pack_cache(pack_id, cache)
        from backend.tool_router import evict_pack_cache, touch_pack_cache

        touch_pack_cache(pack_id)
        removed = evict_pack_cache(protect={pack_id})
        if removed:
            logger.info("Pack cache eviction removed: %s", ", ".join(removed))
        return cache

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
                "#!/bin/bash\nexec /opt/sagemath/bin/sage \"$@\"\n",
            ),
            (
                cache / "usr" / "local" / "bin" / "sage-python",
                "#!/bin/bash\nexec /opt/sagemath/bin/python3 \"$@\"\n",
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
        for pattern in patterns:
            if any(sage_root.glob(pattern)):
                return True
        return False

    async def _copy_cache_into_container(
        self, cache: Path, paths: tuple[str, ...]
    ) -> None:
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
                        f"docker cp into sandbox failed for {src}: "
                        f"{err.strip()} | {err2.strip()}"
                    )

    async def exec(self, command: str, timeout_s: int = 300) -> ExecResult:
        if not self.workspace_dir:
            raise RuntimeError("Sandbox not started")

        async with self._lock:
            await self._ensure_container_unlocked()
            try:
                return await self._exec_inner(command, timeout_s)
            except aiodocker.exceptions.DockerError as e:
                if self._is_container_gone_error(e):
                    logger.warning("exec hit gone container — recreating and retrying once")
                    await self._recreate_container_unlocked()
                    try:
                        return await self._exec_inner(command, timeout_s)
                    except Exception as e2:
                        return ExecResult(
                            exit_code=-1,
                            stdout="",
                            stderr=f"Container recreate failed: {e2}",
                        )
                return ExecResult(exit_code=-1, stdout="", stderr=f"Docker error: {e}")

    async def _exec_inner(self, command: str, timeout_s: int) -> ExecResult:
        # Wrap command with `timeout` so the container kills the process on expiry.
        # --signal=KILL ensures hard kill; --kill-after=5 is a safety net.
        # Prepend pack PATH dirs (non-login bash -c does not read profile.d).
        path_prefix = ""
        if self.extra_path_dirs:
            joined = ":".join(self.extra_path_dirs)
            path_prefix = f"export PATH={shlex.quote(joined)}:$PATH; "
        inner = path_prefix + command
        wrapped = (
            f"timeout --signal=KILL --kill-after=5 {timeout_s} "
            f"bash -c {shlex.quote(inner)}"
        )
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

    async def copy_from(self, container_path: str, host_path: str) -> None:
        """Copy a file from the container to the host."""
        data = await self.read_file_bytes(container_path)
        Path(host_path).parent.mkdir(parents=True, exist_ok=True)
        Path(host_path).write_bytes(data)

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

        if self.workspace_dir:
            import shutil
            try:
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            except Exception:
                pass
            self.workspace_dir = ""
        logger.info("Sandbox stopped")
