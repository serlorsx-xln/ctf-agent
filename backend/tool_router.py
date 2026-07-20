"""Transparent L1 tool-pack router.

UX: always start L0; attach packs into the same container (additive).
Large pack trees are bind-mounted read-only from the host pack cache so
many sandboxes share one copy. Small / late packs may still be copied.
User never picks --image for category. Agent never sees pack IDs in prompts.

Packs mirror the upstream fat-sandbox inventory, split by category so the
default L0 image stays light. Detection is file/extension based,
command-not-found, plus high-confidence remote AD / Assumed Breach text.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shlex
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Soft floor for container Memory when these packs are (pre)loaded.
# Operator CONTAINER_MEMORY_LIMIT still wins when it is already higher.
PACK_MEMORY_FLOOR: dict[str, str] = {
    "crypto": "12g",
    "ml": "8g",
    "crypto-tools": "8g",
    "ghidra": "8g",
    "pwn": "6g",
    "mobile": "6g",
}

# Bootstrap / ensure_pack exec timeout (seconds) per pack.
PACK_BOOTSTRAP_TIMEOUT_S: dict[str, int] = {
    "crypto": 1200,
    "crypto-tools": 900,
    "ghidra": 900,
    "ml": 900,
    "forensics": 600,
    "mobile": 600,
    "pwn": 600,
    "linux": 1200,
    "web": 600,
}

# Pack source images (artifact donors — not the running sandbox).
# Apt/pip-only packs reuse L0 (`ctf-sandbox-core`) as a dummy donor.
PACK_IMAGES: dict[str, str] = {
    "mobile": "ctf-sandbox-mobile",
    "pwn": "ctf-sandbox-pwn",
    "ghidra": "ctf-sandbox-ghidra",
    "crypto": "ctf-sandbox-crypto",
    "crypto-tools": "ctf-sandbox-crypto-tools",
    "steg": "ctf-sandbox-steg",
    "linux": "ctf-sandbox-linux",
    "forensics": "ctf-sandbox-core",
    "web": "ctf-sandbox-core",
    "ml": "ctf-sandbox-core",
    "containers": "ctf-sandbox-core",
}


@dataclass(frozen=True)
class PackSpec:
    """How to materialize a pack into the live L0 container."""

    image: str
    # Absolute paths inside the pack image to extract into the host cache.
    paths: tuple[str, ...] = ()
    # Paths bind-mounted RO from cache at container start (None = all paths).
    # Use a subset when bootstrap must rewrite files under a parent (e.g. sage wrappers).
    bind_paths: tuple[str, ...] | None = None
    # apt packages to ensure inside L0 (best-effort).
    apt: tuple[str, ...] = ()
    # pip packages (uses --break-system-packages when needed).
    pip: tuple[str, ...] = ()
    # gem packages
    gems: tuple[str, ...] = ()
    # Symlinks: dest -> source (both absolute, created after copy).
    symlinks: tuple[tuple[str, str], ...] = ()
    # Extra PATH entries (written into /etc/profile.d + /etc/environment snippet).
    path_dirs: tuple[str, ...] = ()

    def paths_to_bind(self) -> tuple[str, ...]:
        return self.bind_paths if self.bind_paths is not None else self.paths


PACK_SPECS: dict[str, PackSpec] = {
    "mobile": PackSpec(
        image="ctf-sandbox-mobile",
        paths=(
            "/opt/jadx",
            "/opt/apktool",
            "/opt/blutter",
            "/usr/local/bin/apktool",
            "/usr/local/bin/blutter",
        ),
        apt=(
            "openjdk-17-jre-headless",
            "android-tools-adb",
            "aapt",
            "libicu-dev",
            "libcapstone-dev",
            "cmake",
            "ninja-build",
            "pkg-config",
        ),
        pip=("androguard", "frida-tools", "reflutter"),
        symlinks=(
            ("/usr/local/bin/jadx", "/opt/jadx/bin/jadx"),
            ("/usr/local/bin/jadx-gui", "/opt/jadx/bin/jadx-gui"),
        ),
        path_dirs=("/opt/jadx/bin",),
    ),
    "pwn": PackSpec(
        image="ctf-sandbox-pwn",
        # Only GEF from the donor. qemu wrappers are written in bootstrap so
        # they stay writable and always inject -L (even with an old donor cache).
        paths=(
            "/root/.gdbinit-gef.py",
            "/root/.gdbinit",
        ),
        apt=(
            "qemu-user-static",
            "binfmt-support",
            "libc6-amd64-cross",
            "libc6-i386-cross",
            "patchelf",
            "ruby",
            "ruby-dev",
            "gdb",
            "radare2",
        ),
        pip=(
            "pwntools",
            "ROPgadget",
            "capstone",
            "unicorn",
            "keystone-engine",
            "pyelftools",
            "angr",
        ),
        gems=("one_gadget", "seccomp-tools"),
    ),
    "ghidra": PackSpec(
        # Full Ghidra tree + PyGhidra (Veria-parity decompiler). Heavy — bind RO.
        image="ctf-sandbox-ghidra",
        paths=("/opt/ghidra",),
        bind_paths=("/opt/ghidra",),
        apt=("openjdk-21-jdk-headless",),
        pip=("pyghidra",),
        symlinks=(("/usr/local/bin/analyzeHeadless", "/opt/ghidra/support/analyzeHeadless"),),
        path_dirs=("/opt/ghidra/support",),
    ),
    "crypto": PackSpec(
        # Donor: Dockerfile.crypto → conda-forge Sage at /opt/sagemath (Ubuntu 24.04).
        image="ctf-sandbox-crypto",
        paths=(
            "/opt/sagemath",
            "/usr/local/bin/sage",
            "/usr/local/bin/sage-python",
        ),
        bind_paths=("/opt/sagemath",),
        # Keep bootstrap light: core image already has pycryptodome + sympy.
        # Do NOT pip-install galois here — it pulls numba and OOMs (exit 137)
        # inside typical Desktop RAM. Agents can `pip3 install galois` if needed.
        pip=(),
        path_dirs=(),
    ),
    "crypto-tools": PackSpec(
        # RsaCtfTool / cado-nfs / flatter — separate from Sage to keep builds sane.
        image="ctf-sandbox-crypto-tools",
        paths=(
            "/opt/RsaCtfTool",
            "/opt/cado-nfs",
            "/opt/flatter",
        ),
        apt=(
            "libgmp-dev",
            "libmpfr-dev",
            "libmpc-dev",
            "libfplll-dev",
            "libeigen3-dev",
            "libblas-dev",
            "liblapack-dev",
            "libopenblas-dev",
        ),
        pip=("gmpy2", "cysignals", "fpylll"),
        symlinks=(
            ("/usr/local/bin/flatter", "/opt/flatter/bin/flatter"),
            ("/usr/local/bin/cado-nfs", "/opt/cado-nfs/bin/cado-nfs"),
        ),
        path_dirs=("/opt/flatter/bin", "/opt/cado-nfs/bin"),
    ),
    "steg": PackSpec(
        image="ctf-sandbox-steg",
        paths=("/opt/stegseek",),
        apt=(
            "steghide",
            "libimage-exiftool-perl",
            "pngcheck",
            "imagemagick",
            "ffmpeg",
            "sox",
            "tesseract-ocr",
            "tesseract-ocr-eng",
            "ruby",
            "ruby-dev",
        ),
        pip=("Pillow", "pytesseract", "scipy"),
        gems=("zsteg",),
        symlinks=(("/usr/local/bin/stegseek", "/opt/stegseek/bin/stegseek"),),
    ),
    "linux": PackSpec(
        # Jeopardy / Linux-box helpers when the customer has no Kali (L2 later).
        image="ctf-sandbox-linux",
        paths=("/opt/linux-tools",),
        apt=(
            "smbclient",
            "ftp",
            "sshpass",
            "openssh-client",
            "nmap",
            "faketime",
            "krb5-user",
            "ldap-utils",
            "ruby",
            "ruby-dev",
            # NetExec (nxc) builds aardwolf from source — needs Rust + headers.
            "rustc",
            "cargo",
            "python3-dev",
            "git",
            "libssl-dev",
            "libffi-dev",
        ),
        # bloodhound = bloodhound-python CLI. NetExec is NOT on PyPI as `netexec`
        # (install from git in bootstrap_script — batched pip would abort bloodhound).
        pip=("impacket", "certipy-ad", "bloodhound"),
        gems=("evil-winrm",),
        symlinks=(
            ("/usr/local/bin/ffuf", "/opt/linux-tools/bin/ffuf"),
            ("/usr/local/bin/pspy", "/opt/linux-tools/bin/pspy"),
            ("/usr/local/bin/linpeas", "/opt/linux-tools/bin/linpeas"),
        ),
        path_dirs=("/opt/linux-tools/bin",),
    ),
    "forensics": PackSpec(
        image="ctf-sandbox-core",
        paths=(),
        apt=(
            "binwalk",
            "sleuthkit",
            "foremost",
            "dcfldd",
            "testdisk",
            "xfsprogs",
            # tshark pulls wireshark-common (editcap / mergecap / capinfos).
            "tshark",
        ),
        pip=("volatility3", "scapy"),
    ),
    "web": PackSpec(
        image="ctf-sandbox-core",
        paths=(),
        # ffuf lives in the linux donor (/opt/linux-tools); first `ffuf` use
        # auto-ensures that pack. sqlmap covers SQLi without pulling AD stack.
        apt=("nmap", "sqlmap"),
        pip=("flask", "PyJWT"),
    ),
    "ml": PackSpec(
        image="ctf-sandbox-core",
        paths=(),
        # torch/keras installed in bootstrap_script (CPU index URL).
        pip=(),
    ),
    "containers": PackSpec(
        image="ctf-sandbox-core",
        paths=(),
        apt=(
            "podman",
            "buildah",
            "fuse-overlayfs",
            "slirp4netns",
            "uidmap",
        ),
    ),
}

# Binary / entrypoint → pack (for not-found auto-ensure).
TOOL_TO_PACK: dict[str, str] = {
    # mobile
    "jadx": "mobile",
    "jadx-gui": "mobile",
    "apktool": "mobile",
    "blutter": "mobile",
    "reflutter": "mobile",
    "frida": "mobile",
    "frida-ps": "mobile",
    "frida-trace": "mobile",
    "aapt": "mobile",
    "aapt2": "mobile",
    "adb": "mobile",
    # pwn
    "one_gadget": "pwn",
    "ropgadget": "pwn",
    "ROPgadget": "pwn",
    "checksec": "pwn",
    "seccomp-tools": "pwn",
    "patchelf": "pwn",
    "q64": "pwn",
    "q32": "pwn",
    "qemu-x86_64-static": "pwn",
    "qemu-i386-static": "pwn",
    "r2": "pwn",
    "radare2": "pwn",
    "rabin2": "pwn",
    "rax2": "pwn",
    # ghidra / RE
    "pyghidra": "ghidra",
    "analyzeHeadless": "ghidra",
    "ghidraRun": "ghidra",
    "ghidra": "ghidra",
    # crypto (sage)
    "sage": "crypto",
    "sagemath": "crypto",
    "galois": "crypto",
    # crypto-tools
    "RsaCtfTool": "crypto-tools",
    "rsactftool": "crypto-tools",
    "cado-nfs": "crypto-tools",
    "flatter": "crypto-tools",
    # steg / media
    "steghide": "steg",
    "stegseek": "steg",
    "zsteg": "steg",
    "exiftool": "steg",
    "pngcheck": "steg",
    "identify": "steg",
    "convert": "steg",
    "montage": "steg",
    "ffmpeg": "steg",
    "sox": "steg",
    "tesseract": "steg",
    # forensics
    "binwalk": "forensics",
    "mmls": "forensics",
    "fls": "forensics",
    "icat": "forensics",
    "tsk_recover": "forensics",
    "fsstat": "forensics",
    "foremost": "forensics",
    "testdisk": "forensics",
    "dcfldd": "forensics",
    "xfs_db": "forensics",
    "xfs_repair": "forensics",
    "vol": "forensics",
    "volatility": "forensics",
    "volatility3": "forensics",
    "tshark": "forensics",
    "editcap": "forensics",
    "mergecap": "forensics",
    "capinfos": "forensics",
    "scapy": "forensics",
    # web
    "nmap": "web",
    "sqlmap": "web",
    # linux box / light remote
    "linpeas": "linux",
    "linpeas.sh": "linux",
    "pspy": "linux",
    "ffuf": "linux",
    "smbclient": "linux",
    "sshpass": "linux",
    "ssh": "linux",
    "faketime": "linux",
    "kinit": "linux",
    "certipy": "linux",
    "evil-winrm": "linux",
    "ldapsearch": "linux",
    "ldapadd": "linux",
    "ldapwhoami": "linux",
    "nxc": "linux",
    "netexec": "linux",
    "bloodhound-python": "linux",
    "bloodhound": "linux",
    "impacket-smbclient": "linux",
    "impacket-psexec": "linux",
    "impacket-wmiexec": "linux",
    "psexec.py": "linux",
    "smbclient.py": "linux",
    "wmiexec.py": "linux",
    "secretsdump.py": "linux",
    "getTGT.py": "linux",
    "getST.py": "linux",
    # containers
    "podman": "containers",
    "buildah": "containers",
    "podman-compose": "containers",
}

# Prefer denser / common packs first when multiple match (prefetch order).
_PACK_PRIORITY = (
    "mobile",
    "pwn",
    "ghidra",
    "crypto",
    "crypto-tools",
    "steg",
    "linux",
    "forensics",
    "web",
    "ml",
    "containers",
)

_MOBILE_SUFFIXES = {".apk", ".aab", ".ipa", ".xapk"}
_MOBILE_NAMES = {"libapp.so", "libflutter.so", "classes.dex", "androidmanifest.xml"}
_CRYPTO_SUFFIXES = {".sage"}
_CRYPTO_NAMES: set[str] = set()
_FORENSICS_SUFFIXES = {
    ".raw",
    ".vmem",
    ".mem",
    ".dmp",
    ".e01",
    ".ewf",
    ".aff",
    ".aff4",
    ".pcap",
    ".pcapng",
    ".cap",
}
_FORENSICS_NAMES = {"memory.dmp", "memdump.raw", "core.dump"}
# Light web markers in distfiles (prefetch sqlmap/nmap — not the AD linux stack).
# Intentionally omit .js (Node/misc challenges) to avoid noisy web prefetch.
_WEB_SUFFIXES = {".php", ".html", ".htm", ".asp", ".aspx", ".jsp"}
_ML_SUFFIXES = {".pt", ".pth", ".onnx", ".h5", ".keras", ".safetensors"}
_CONTAINER_NAMES = {
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}

_NOT_FOUND_RE = re.compile(
    r"(?:bash:\s+)?(?:line \d+:\s+)?([^\s:]+):\s+(?:command not found|not found)",
    re.IGNORECASE,
)
_IMPORT_FAIL_RE = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

# Default L0 image (packs attach on demand).
DEFAULT_L0_CANDIDATES = ("ctf-sandbox-core",)

# Python import name → pack (auto-ensure on ModuleNotFoundError).
_IMPORT_TO_PACK: dict[str, str] = {
    "angr": "pwn",
    "capstone": "pwn",
    "unicorn": "pwn",
    "pyghidra": "ghidra",
    "galois": "crypto",
    "gmpy2": "crypto-tools",
    "fpylll": "crypto-tools",
    "Crypto": "crypto",
    "PIL": "steg",
    "Pillow": "steg",
    "pytesseract": "steg",
    "scipy": "steg",
    "volatility3": "forensics",
    "scapy": "forensics",
    "flask": "web",
    "jwt": "web",
    "torch": "ml",
    "keras": "ml",
    "impacket": "linux",
    "bloodhound": "linux",
}


def pack_cache_root() -> Path:
    override = os.environ.get("CTF_PACK_CACHE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "ctf-agent" / "packs"


def pack_cache_dir(pack_id: str) -> Path:
    arch = platform.machine().replace("aarch64", "arm64")
    return pack_cache_root() / pack_id / arch


def pack_binds_enabled() -> bool:
    """Host-cache bind mounts (default on). Set CTF_PACK_BIND=0 to force docker cp."""
    return os.environ.get("CTF_PACK_BIND", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def parse_memory_bytes(limit: str) -> int:
    """Parse Docker-style memory strings (``16g``, ``512m``, raw bytes)."""
    s = (limit or "").strip().lower()
    if not s:
        return 0
    try:
        if s.endswith("g"):
            return int(float(s[:-1]) * 1024 * 1024 * 1024)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1024 * 1024)
        if s.endswith("k"):
            return int(float(s[:-1]) * 1024)
        return int(s)
    except ValueError, IndexError:
        return 0


def format_memory_limit(nbytes: int) -> str:
    if nbytes <= 0:
        return "0"
    gib = nbytes / (1024 * 1024 * 1024)
    if abs(gib - round(gib)) < 1e-6:
        return f"{int(round(gib))}g"
    mib = nbytes / (1024 * 1024)
    if abs(mib - round(mib)) < 1e-6:
        return f"{int(round(mib))}m"
    return str(int(nbytes))


def recommended_memory_limit(base: str, packs: Iterable[str]) -> str:
    """Raise ``base`` to the max pack floor among ``packs`` (never lower)."""
    best = parse_memory_bytes(base) or parse_memory_bytes("4g")
    for pack_id in packs:
        floor = PACK_MEMORY_FLOOR.get(pack_id)
        if not floor:
            continue
        best = max(best, parse_memory_bytes(floor))
    return format_memory_limit(best)


def pack_bootstrap_timeout_s(pack_id: str) -> int:
    return PACK_BOOTSTRAP_TIMEOUT_S.get(pack_id, 600)


def pack_cache_max_bytes() -> int:
    """Max host pack-cache size. ``CTF_PACK_CACHE_MAX_GB`` (default 25; 0=off)."""
    raw = os.environ.get("CTF_PACK_CACHE_MAX_GB", "25").strip()
    try:
        gib = float(raw)
    except ValueError:
        gib = 25.0
    if gib <= 0:
        return 0
    return int(gib * 1024 * 1024 * 1024)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file() and not p.is_symlink():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def touch_pack_cache(pack_id: str) -> None:
    """Mark a pack cache entry as recently used (LRU eviction)."""
    cache = pack_cache_dir(pack_id)
    if not cache.is_dir():
        return
    accessed = cache / ".accessed"
    try:
        accessed.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    except OSError:
        pass
    for name in (".ready", ".prepared", ".accessed"):
        p = cache / name
        if p.exists():
            try:
                os.utime(p, None)
            except OSError:
                pass


def pack_cache_entries() -> list[tuple[float, int, str, Path]]:
    """Return ``(mtime, size, pack_id, arch_path)`` for each cached pack/arch."""
    root = pack_cache_root()
    if not root.is_dir():
        return []
    out: list[tuple[float, int, str, Path]] = []
    for pack_dir in sorted(root.iterdir()):
        if not pack_dir.is_dir():
            continue
        pack_id = pack_dir.name
        for arch_dir in pack_dir.iterdir():
            if not arch_dir.is_dir():
                continue
            accessed = arch_dir / ".accessed"
            ready = arch_dir / ".ready"
            stamp = accessed if accessed.is_file() else ready
            try:
                mtime = stamp.stat().st_mtime if stamp.is_file() else arch_dir.stat().st_mtime
            except OSError:
                mtime = 0.0
            out.append((mtime, _dir_size_bytes(arch_dir), pack_id, arch_dir))
    return out


def evict_pack_cache(*, protect: Iterable[str] | None = None) -> list[str]:
    """Delete oldest pack caches until under ``CTF_PACK_CACHE_MAX_GB``.

    ``protect`` pack ids are never removed (e.g. the pack just materialized).
    Returns human-readable labels of removed entries (``pack/arch``).
    """
    max_b = pack_cache_max_bytes()
    if max_b <= 0:
        return []
    protected = set(protect or ())
    entries = pack_cache_entries()
    total = sum(size for _, size, _, _ in entries)
    if total <= max_b:
        return []

    entries.sort(key=lambda e: e[0])  # oldest first
    removed: list[str] = []
    for _mtime, size, pack_id, path in entries:
        if total <= max_b:
            break
        if pack_id in protected:
            continue
        label = f"{pack_id}/{path.name}"
        try:
            shutil.rmtree(path, ignore_errors=True)
            parent = path.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError as e:
            logger.warning("Pack cache evict failed for %s: %s", label, e)
            continue
        removed.append(label)
        total -= size
        logger.info("Evicted pack cache %s (%d bytes freed, total~%d)", label, size, total)
    return removed


def pack_cache_item(cache: Path, container_path: str) -> Path:
    """Host path inside a pack cache for an absolute container path."""
    return cache / container_path.lstrip("/")


def pack_marker_path(pack_id: str) -> str:
    """Opaque in-container marker (no pack id in the path)."""
    digest = hashlib.sha256(pack_id.encode("utf-8")).hexdigest()[:16]
    return f"/var/lib/ctf/.ready-{digest}"


def _iter_challenge_files(challenge_dir: Path) -> list[Path]:
    roots = [challenge_dir / "distfiles", challenge_dir]
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(p)
    return out


def _looks_like_elf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _challenge_text(challenge_dir: Path) -> str:
    for name in ("challenge.txt", "README.md", "metadata.yml"):
        path = challenge_dir / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ""


def _wants_linux_remote_pack(text: str) -> bool:
    """High-confidence remote AD / Assumed Breach labs (no distfiles needed)."""
    if not text:
        return False
    low = text.lower()
    compact = low.replace(" ", "").replace("-", "")
    if "assumedbreach" in compact:
        return True
    ad_keys = (
        "active directory",
        "domain controller",
        "kerberos",
        "winrm",
        "ldap",
        "smb ",
        " smb",
        "ntlm",
        "bloodhound",
    )
    hits = sum(1 for k in ad_keys if k in low)
    if hits >= 2:
        return True
    # HTB-style: lab IP + user/root flags + credentials line
    has_ip = bool(re.search(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text))
    has_flags = "user flag" in low and "root flag" in low
    has_creds = bool(re.search(r"\b\w+\s*/\s*\S+", text)) or "credentials" in low
    return has_ip and has_flags and has_creds


def detect_packs(challenge_dir: str | Path) -> list[str]:
    """Heuristic pack detection from challenge files (prefetch).

    Primarily file/extension based. Also prefetch ``linux`` for high-confidence
    remote AD / Assumed Breach challenge text (empty distfiles is common).
    """
    root = Path(challenge_dir)
    packs: set[str] = set()

    for path in _iter_challenge_files(root):
        name = path.name.lower()
        suffix = path.suffix.lower()

        if suffix in _MOBILE_SUFFIXES or name in _MOBILE_NAMES:
            packs.add("mobile")
        if name.endswith(".apk") or "libapp.so" in name:
            packs.add("mobile")

        if suffix in _CRYPTO_SUFFIXES or name in _CRYPTO_NAMES:
            packs.add("crypto")

        if suffix in _FORENSICS_SUFFIXES or name in _FORENSICS_NAMES:
            packs.add("forensics")

        if suffix in _WEB_SUFFIXES:
            packs.add("web")

        if suffix in _ML_SUFFIXES:
            packs.add("ml")

        if name in _CONTAINER_NAMES:
            packs.add("containers")

        if (
            (_looks_like_elf(path) and name not in _MOBILE_NAMES and suffix not in {".so"})
            or suffix == ".elf"
            or (path.suffix == "" and _looks_like_elf(path))
        ):
            packs.add("pwn")
            packs.add("ghidra")

    if _wants_linux_remote_pack(_challenge_text(root)):
        packs.add("linux")

    return [p for p in _PACK_PRIORITY if p in packs]


def resolve_sandbox_image(
    challenge_dir: str | Path,
    *,
    default_image: str = "ctf-sandbox-core",
    locked_image: str | None = None,
) -> tuple[str, list[str]]:
    """Always keep L0 as the runtime image; packs are additive later."""
    packs = detect_packs(challenge_dir)
    if locked_image:
        return locked_image, packs
    logger.info("Tool router: L0 image=%s packs=%s", default_image, packs)
    return default_image, packs


def apply_router_to_settings(settings, challenge_dir: str | Path):
    """Record detected packs; do not swap sandbox_image to a pack image."""
    locked = bool(getattr(settings, "sandbox_image_locked", False))
    packs = detect_packs(challenge_dir)
    settings.detected_packs = packs
    if locked:
        return settings.sandbox_image, packs

    image = getattr(settings, "sandbox_image", None) or "ctf-sandbox-core"
    donor_only = {
        "ctf-sandbox-mobile",
        "ctf-sandbox-pwn",
        "ctf-sandbox-ghidra",
        "ctf-sandbox-crypto",
        "ctf-sandbox-crypto-tools",
        "ctf-sandbox-steg",
        "ctf-sandbox-linux",
    }
    if image in donor_only:
        logger.warning(
            "sandbox_image=%s looks like a pack donor; preferring L0 core",
            image,
        )
        image = "ctf-sandbox-core"
        settings.sandbox_image = image
    logger.info("Tool router: staying on L0=%s; will ensure packs=%s", image, packs)
    return image, packs


def tools_doc_path(name: str) -> Path | None:
    root = Path(__file__).resolve().parents[1] / "sandbox"
    path = root / name
    return path if path.is_file() else None


def tools_doc_for_image(image: str) -> Path | None:
    """Host path to the base tools inventory for an L0 image."""
    if "core" in (image or ""):
        return tools_doc_path("sandbox-tools-core.txt")
    if "mobile" in (image or ""):
        return tools_doc_path("sandbox-tools-mobile.txt")
    if "pwn" in (image or ""):
        return tools_doc_path("sandbox-tools-pwn.txt")
    return tools_doc_path("sandbox-tools-core.txt")


def merged_tools_doc(base_image: str, ensured_packs: set[str] | list[str]) -> str:
    """Concatenate L0 + loaded pack inventories for /tools.txt.

    Agent-facing text must not mention pack IDs or router internals.
    """
    parts: list[str] = []
    base = tools_doc_for_image(base_image)
    if base:
        parts.append(base.read_text(encoding="utf-8").rstrip())
    else:
        parts.append("CTF SANDBOX\n===========\nBase tools available.\n")

    for pack_id in _PACK_PRIORITY:
        if pack_id not in ensured_packs:
            continue
        pack_doc = tools_doc_path(f"sandbox-tools-{pack_id}.txt")
        if pack_doc:
            parts.append("")
            parts.append(pack_doc.read_text(encoding="utf-8").rstrip())

    return "\n".join(parts) + "\n"


def bootstrap_script(pack_id: str) -> str:
    """Shell script run inside L0 after files are copied."""
    spec = PACK_SPECS[pack_id]
    lines = [
        "set -e",
        "export DEBIAN_FRONTEND=noninteractive",
    ]
    if spec.apt:
        pkgs = " ".join(shlex.quote(p) for p in spec.apt)
        lines += [
            "apt-get update -qq || true",
            f"apt-get install -y --no-install-recommends {pkgs} || true",
        ]
    if spec.pip:
        pkgs = " ".join(shlex.quote(p) for p in spec.pip)
        lines += [
            "PIP3=$(command -v /usr/bin/pip3 || command -v pip3)",
            f"$PIP3 install --no-cache-dir --break-system-packages {pkgs} "
            f"|| $PIP3 install --no-cache-dir {pkgs} || true",
        ]
    if pack_id == "crypto":
        # Sage tree is often RO bind-mounted; only touch writable wrapper paths.
        # Host finalize already seeds pycryptodome into the Sage env when possible.
        lines += [
            "set +e",
            "if [ -w /opt/sagemath ] && [ -x /opt/sagemath/bin/python3 ]; then",
            "  /opt/sagemath/bin/python3 -m pip install --no-cache-dir "
            "pycryptodome 2>/dev/null || true",
            "fi",
            "printf '%s\\n' '#!/bin/bash' 'exec /opt/sagemath/bin/sage \"$@\"' "
            "> /usr/local/bin/sage",
            "printf '%s\\n' '#!/bin/bash' "
            "'exec /opt/sagemath/bin/python3 \"$@\"' > /usr/local/bin/sage-python",
            "chmod +x /usr/local/bin/sage /usr/local/bin/sage-python",
            "set -e",
            # Require sage binary from the bind/copy — fail loud if missing.
            "test -x /opt/sagemath/bin/sage",
        ]
    if pack_id == "crypto-tools":
        lines += [
            "if [ -d /opt/RsaCtfTool ]; then",
            "  PIP3=$(command -v /usr/bin/pip3 || command -v pip3)",
            "  $PIP3 install --no-cache-dir --break-system-packages /opt/RsaCtfTool "
            "|| $PIP3 install --no-cache-dir /opt/RsaCtfTool || true",
            "fi",
            "mkdir -p /usr/local/bin",
            "if [ -x /opt/cado-nfs/bin/cado-nfs ]; then",
            "  cp -f /opt/cado-nfs/bin/cado-nfs /usr/local/bin/cado-nfs || true",
            "elif [ -f /opt/cado-nfs/cado-nfs.py ]; then",
            "  printf '%s\\n' '#!/bin/bash' "
            "'BUILD_PARENT=/opt/cado-nfs/build' "
            "'H=$(hostname)' "
            "'if [ ! -d \"$BUILD_PARENT/$H\" ]; then' "
            "'  src=$(ls -d \"$BUILD_PARENT\"/* 2>/dev/null | head -1)' "
            '\'  [ -n "$src" ] && ln -sf "$src" "$BUILD_PARENT/$H"\' '
            "'fi' "
            "'exec python3 /opt/cado-nfs/cado-nfs.py \"$@\"' "
            "> /usr/local/bin/cado-nfs",
            "  chmod +x /usr/local/bin/cado-nfs",
            "fi",
            "if [ -x /opt/flatter/bin/flatter ]; then",
            "  ln -sfn /opt/flatter/bin/flatter /usr/local/bin/flatter || true",
            "fi",
            "ldconfig 2>/dev/null || true",
        ]
    if pack_id == "ghidra":
        lines += [
            # Non-login bash -c does not source profile.d — seed env for every python3.
            "SITE=$(python3 -c 'import site; print(site.getsitepackages()[0])')",
            "printf '%s\\n' "
            "'import os' "
            '\'os.environ.setdefault("GHIDRA_INSTALL_DIR", "/opt/ghidra")\' '
            '> "$SITE/ctf_ghidra_env.py"',
            "printf '%s\\n' 'import ctf_ghidra_env' > \"$SITE/ctf_ghidra_env.pth\"",
            "printf '%s\\n' 'export GHIDRA_INSTALL_DIR=/opt/ghidra' "
            "> /etc/profile.d/ctf-ghidra.sh || true",
            "grep -q GHIDRA_INSTALL_DIR /etc/environment 2>/dev/null || "
            "echo GHIDRA_INSTALL_DIR=/opt/ghidra >> /etc/environment || true",
            "if [ -x /opt/ghidra/support/analyzeHeadless ]; then",
            "  ln -sfn /opt/ghidra/support/analyzeHeadless /usr/local/bin/analyzeHeadless || true",
            "fi",
        ]
    if pack_id == "pwn":
        lines += [
            "mkdir -p /usr/local/bin",
            'case "$(uname -m)" in aarch64|arm64)',
            "  mkdir -p /lib64",
            "  if [ ! -e /lib64/ld-linux-x86-64.so.2 ]; then",
            "    ln -sfn /usr/x86_64-linux-gnu/lib/ld-linux-x86-64.so.2 "
            "/lib64/ld-linux-x86-64.so.2 || true",
            "  fi",
            "  ;;",
            "esac",
            "printf '%s\\n' '#!/bin/bash' "
            "'PREFIX=\"${QEMU_LD_PREFIX:-/usr/x86_64-linux-gnu}\"' "
            '\'for a in "$@"; do case "$a" in -L) '
            'exec /usr/bin/qemu-x86_64-static "$@";; esac; done\' '
            '\'if [ -d "$PREFIX/lib" ]; then '
            'exec /usr/bin/qemu-x86_64-static -L "$PREFIX" "$@"; fi\' '
            "'exec /usr/bin/qemu-x86_64-static \"$@\"' "
            "> /usr/local/bin/qemu-x86_64-static",
            "printf '%s\\n' '#!/bin/bash' "
            "'PREFIX=\"${QEMU_LD_PREFIX:-/usr/i686-linux-gnu}\"' "
            '\'for a in "$@"; do case "$a" in -L) '
            'exec /usr/bin/qemu-i386-static "$@";; esac; done\' '
            '\'if [ -d "$PREFIX/lib" ]; then '
            'exec /usr/bin/qemu-i386-static -L "$PREFIX" "$@"; fi\' '
            "'exec /usr/bin/qemu-i386-static \"$@\"' "
            "> /usr/local/bin/qemu-i386-static",
            "printf '%s\\n' '#!/bin/bash' 'exec qemu-x86_64-static \"$@\"' > /usr/local/bin/q64",
            "printf '%s\\n' '#!/bin/bash' 'exec qemu-i386-static \"$@\"' > /usr/local/bin/q32",
            "chmod +x /usr/local/bin/qemu-x86_64-static "
            "/usr/local/bin/qemu-i386-static "
            "/usr/local/bin/q64 /usr/local/bin/q32",
        ]
    if pack_id == "ml":
        lines += [
            "PIP3=$(command -v /usr/bin/pip3 || command -v pip3)",
            "$PIP3 install --no-cache-dir --break-system-packages "
            "--ignore-installed sympy "
            "torch --index-url https://download.pytorch.org/whl/cpu "
            "|| $PIP3 install --no-cache-dir torch "
            "--index-url https://download.pytorch.org/whl/cpu || true",
            "$PIP3 install --no-cache-dir --break-system-packages keras "
            "|| $PIP3 install --no-cache-dir keras || true",
            # Keras 3 defaults to TensorFlow; we ship torch only.
            "printf '%s\\n' 'export KERAS_BACKEND=torch' "
            "> /etc/profile.d/ctf-keras-backend.sh || true",
            "grep -q KERAS_BACKEND /etc/environment 2>/dev/null || "
            "echo KERAS_BACKEND=torch >> /etc/environment || true",
        ]
    if pack_id == "linux":
        lines += [
            # Ensure linpeas launcher exists even if an older cache only has .sh
            "if [ -f /opt/linux-tools/bin/linpeas.sh ] "
            "&& [ ! -x /opt/linux-tools/bin/linpeas ]; then",
            "  printf '%s\\n' '#!/bin/bash' "
            "'exec bash /opt/linux-tools/bin/linpeas.sh \"$@\"' "
            "> /opt/linux-tools/bin/linpeas",
            "  chmod +x /opt/linux-tools/bin/linpeas",
            "fi",
            # NetExec: install from git (PyPI has no `netexec` dist). Needs rustc/cargo
            # from apt above. Best-effort — bloodhound/impacket still usable if this fails.
            "if ! command -v nxc >/dev/null 2>&1; then",
            "  PIP3=$(command -v /usr/bin/pip3 || command -v pip3)",
            "  $PIP3 install --no-cache-dir --break-system-packages "
            "'git+https://github.com/Pennyw0rth/NetExec.git' "
            "|| $PIP3 install --no-cache-dir "
            "'git+https://github.com/Pennyw0rth/NetExec.git' || true",
            "fi",
            # /etc/hosts is often a Docker bind-mount — sed -i fails.
            "cat > /usr/local/bin/ctf-hosts-add <<'EOF'",
            "#!/bin/bash",
            "set -euo pipefail",
            "if [ $# -lt 2 ]; then",
            '  echo "usage: ctf-hosts-add <ip> <hostname> [hostname...]" >&2',
            "  exit 2",
            "fi",
            "ip=$1; shift",
            'line="$ip $*"',
            "tmp=$(mktemp)",
            'cp /etc/hosts "$tmp"',
            'for h in "$@"; do',
            '  grep -v -E "[[:space:]]${h}([[:space:]]|$)" "$tmp" > "${tmp}.n" || true',
            '  mv "${tmp}.n" "$tmp"',
            "done",
            'printf \'%s\\n\' "$line" >> "$tmp"',
            'cat "$tmp" > /etc/hosts',
            'rm -f "$tmp"',
            "EOF",
            "chmod +x /usr/local/bin/ctf-hosts-add",
        ]
    if pack_id == "containers":
        lines += [
            # Nested containers are best-effort (same as upstream fat image).
            "command -v podman >/dev/null 2>&1 || true",
        ]
    if spec.gems:
        gems = " ".join(shlex.quote(g) for g in spec.gems)
        lines += [
            f"command -v gem >/dev/null 2>&1 && gem install {gems} --no-document || true",
        ]
    for dest, src in spec.symlinks:
        lines.append(f"ln -sfn {shlex.quote(src)} {shlex.quote(dest)} || true")
        lines.append(f"chmod +x {shlex.quote(src)} 2>/dev/null || true")
        lines.append(f"chmod +x {shlex.quote(dest)} 2>/dev/null || true")
    for d in spec.path_dirs:
        lines.append(
            f"grep -q {shlex.quote(d)} /etc/environment 2>/dev/null || "
            f'echo "PATH=\\"{d}:$PATH\\"" >> /etc/environment || true'
        )
        lines.append(
            f'printf "%s\\n" "export PATH={shlex.quote(d)}:\\$PATH" '
            f">> /etc/profile.d/ctf-extra-path.sh || true"
        )
    marker = pack_marker_path(pack_id)
    lines.append(f"mkdir -p {shlex.quote(str(Path(marker).parent))}")
    lines.append(f"touch {shlex.quote(marker)}")
    lines.append('echo "tools ready"')
    return "\n".join(lines) + "\n"


def infer_pack_from_command(command: str) -> str | None:
    """Guess pack from the command line the agent tried to run."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    for tok in tokens:
        base = Path(tok).name
        if base in TOOL_TO_PACK:
            return TOOL_TO_PACK[base]
        low = base.lower()
        if low in TOOL_TO_PACK:
            return TOOL_TO_PACK[low]
    low_cmd = command.lower()
    if "androguard" in low_cmd or "frida" in low_cmd:
        return "mobile"
    if "galois" in low_cmd or "sagemath" in low_cmd or "gf128" in low_cmd:
        return "crypto"
    if "rsactftool" in low_cmd or "cado" in low_cmd or "fpylll" in low_cmd:
        return "crypto-tools"
    if "pyghidra" in low_cmd or "analyzeheadless" in low_cmd or "ghidra" in low_cmd:
        return "ghidra"
    if (
        "volatility" in low_cmd
        or "binwalk" in low_cmd
        or "tshark" in low_cmd
        or "scapy" in low_cmd
        or "editcap" in low_cmd
    ):
        return "forensics"
    if "steghide" in low_cmd or "zsteg" in low_cmd or "tesseract" in low_cmd:
        return "steg"
    if "sqlmap" in low_cmd:
        return "web"
    if (
        "linpeas" in low_cmd
        or "pspy" in low_cmd
        or "ffuf" in low_cmd
        or "impacket" in low_cmd
        or "evil-winrm" in low_cmd
        or "smbclient" in low_cmd
        or "certipy" in low_cmd
        or "faketime" in low_cmd
        or "gettgt" in low_cmd
        or "ldapsearch" in low_cmd
        or "netexec" in low_cmd
        or "bloodhound" in low_cmd
    ):
        return "linux"
    if "import torch" in low_cmd or "import keras" in low_cmd:
        return "ml"
    if "podman" in low_cmd or "buildah" in low_cmd:
        return "containers"
    return None


def infer_pack_from_failure(command: str, stderr: str, stdout: str = "") -> str | None:
    """Guess pack from command-not-found / ModuleNotFoundError failures."""
    blob = f"{stderr}\n{stdout}"
    for m in _NOT_FOUND_RE.finditer(blob):
        name = Path(m.group(1)).name
        if name in TOOL_TO_PACK:
            return TOOL_TO_PACK[name]
        low = name.lower()
        if low in TOOL_TO_PACK:
            return TOOL_TO_PACK[low]
    for m in _IMPORT_FAIL_RE.finditer(blob):
        mod = m.group(1).split(".")[0]
        if mod in _IMPORT_TO_PACK:
            return _IMPORT_TO_PACK[mod]
    pack = infer_pack_from_command(command)
    if pack and (
        "command not found" in blob.lower()
        or "not found" in blob.lower()
        or "no such file" in blob.lower()
        or "modulenotfounderror" in blob.lower()
    ):
        return pack
    return None


def parse_ensure_pack_command(command: str) -> str | None:
    """If command is `ctf-ensure-pack <id>`, return pack id; else None."""
    try:
        tokens = shlex.split(command.strip())
    except ValueError:
        tokens = command.strip().split()
    if len(tokens) >= 2 and tokens[0] in ("ctf-ensure-pack", "/usr/local/bin/ctf-ensure-pack"):
        return tokens[1].strip()
    if len(tokens) == 1 and tokens[0] in ("ctf-ensure-pack", "/usr/local/bin/ctf-ensure-pack"):
        return ""
    return None


def donor_build_hint(pack_id: str) -> str:
    """Operator-facing build hint when a donor image is missing."""
    hints = {
        "crypto": ("docker build -f sandbox/Dockerfile.crypto -t ctf-sandbox-crypto ."),
        "crypto-tools": (
            "docker build -f sandbox/Dockerfile.crypto-tools -t ctf-sandbox-crypto-tools ."
        ),
        "steg": "docker build -f sandbox/Dockerfile.steg -t ctf-sandbox-steg .",
        "linux": "docker build -f sandbox/Dockerfile.linux -t ctf-sandbox-linux .",
        "mobile": ("docker build -f sandbox/Dockerfile.mobile -t ctf-sandbox-mobile ."),
        "pwn": "docker build -f sandbox/Dockerfile.pwn -t ctf-sandbox-pwn .",
        "ghidra": ("docker build -f sandbox/Dockerfile.ghidra -t ctf-sandbox-ghidra ."),
    }
    cmd = hints.get(pack_id)
    return f" Build the donor: {cmd}" if cmd else ""
