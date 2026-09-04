"""QEMU guest libraries expected in pwn/mobile donor images.

C++ x86_64/i386 challenge bins on aarch64 hosts fail at runtime without
libstdc++ / libgcc_s in the qemu ``-L`` sysroot (libc6-*-cross alone is not enough).
"""

from __future__ import annotations

GUEST_LIB_PATHS: tuple[str, ...] = (
    "/usr/x86_64-linux-gnu/lib/libstdc++.so.6",
    "/usr/x86_64-linux-gnu/lib/libgcc_s.so.1",
    "/usr/i686-linux-gnu/lib/libstdc++.so.6",
    "/usr/i686-linux-gnu/lib/libgcc_s.so.1",
)

DONOR_GUEST_LIB_IMAGES: dict[str, str] = {
    "pwn": "ctf-sandbox-pwn",
    "mobile": "ctf-sandbox-mobile",
}

_GUEST_LIB_PROBE = (
    "import os\n"
    f"need = {list(GUEST_LIB_PATHS)!r}\n"
    "print('\\n'.join(p for p in need if not os.path.exists(p)))\n"
)


def image_missing_guest_libs(image: str) -> list[str]:
    """Return missing guest-lib paths in ``image``, or ``['image not installed']``."""
    from backend.sandbox.setup_ready import _docker_image_exists, _docker_run

    tag = (image or "").strip()
    if not tag:
        return ["image not installed"]
    if not _docker_image_exists(tag):
        return ["image not installed"]
    proc = _docker_run(
        [
            "run",
            "--rm",
            "--entrypoint",
            "python3",
            tag,
            "-c",
            _GUEST_LIB_PROBE,
        ],
        timeout_s=60,
    )
    if proc is None:
        return ["probe failed"]
    if proc.returncode not in (0, None) and not (proc.stdout or b"").strip():
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return [err[:160] or f"exit {proc.returncode}"]
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def probe_donor_guest_libs() -> dict[str, list[str]]:
    """Pack id → missing guest libs (empty list means that donor is complete)."""
    out: dict[str, list[str]] = {}
    for pack_id, image in DONOR_GUEST_LIB_IMAGES.items():
        missing = image_missing_guest_libs(image)
        if missing:
            out[pack_id] = missing
    return out


def flatten_guest_lib_gaps(gaps: dict[str, list[str]]) -> list[str]:
    """Stable ``pack:path`` lines for launch / CLI summaries."""
    lines: list[str] = []
    for pack_id in DONOR_GUEST_LIB_IMAGES:
        for item in gaps.get(pack_id, ()):
            lines.append(f"{pack_id}:{item}")
    return lines
