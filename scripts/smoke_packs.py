#!/usr/bin/env python3
"""Smoke-test additive pack loading into L0 (ensure_pack + probe commands)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Prefer an existing DOCKER_HOST; else platform defaults.
if "DOCKER_HOST" not in os.environ and sys.platform != "win32":
    for candidate in (
        Path.home() / ".colima/default/docker.sock",
        Path.home() / ".docker/run/docker.sock",
        Path("/var/run/docker.sock"),
    ):
        if candidate.exists():
            os.environ["DOCKER_HOST"] = f"unix://{candidate}"
            break

from backend.sandbox import DockerSandbox  # noqa: E402
from backend.tool_router import PACK_SPECS  # noqa: E402

# pack_id -> shell probe that must exit 0 after ensure
PROBES: dict[str, str] = {
    "web": "command -v nmap && command -v sqlmap && python3 -c 'import flask,jwt'",
    "forensics": (
        "command -v binwalk && command -v fls && command -v tshark "
        "&& python3 -c 'import volatility3,scapy'"
    ),
    "steg": (
        "command -v steghide && command -v stegseek && command -v zsteg "
        "&& command -v exiftool && command -v tesseract "
        "&& python3 -c 'from PIL import Image; import pytesseract'"
    ),
    "pwn": ("command -v q64 && command -v r2 && python3 -c 'import angr,capstone'"),
    "ghidra": (
        "test -x /opt/ghidra/support/analyzeHeadless "
        "&& command -v analyzeHeadless "
        "&& python3 -c '"
        "import os; "
        'assert os.environ.get("GHIDRA_INSTALL_DIR") == "/opt/ghidra", os.environ.get("GHIDRA_INSTALL_DIR"); '
        "import pyghidra; "
        "print(pyghidra.__file__)'"
    ),
    "crypto": "command -v sage && sage -c 'print(1+1)'",
    "crypto-tools": (
        "command -v flatter && command -v cado-nfs "
        "&& python3 -c 'import gmpy2; import fpylll' "
        "&& (command -v RsaCtfTool || ls /opt/RsaCtfTool/RsaCtfTool.py >/dev/null "
        "|| ls /opt/RsaCtfTool >/dev/null)"
    ),
    "containers": "command -v podman || command -v buildah",
    "ml": (
        "python3 -c '"
        "import tensorflow as tf; import torch; import tqdm; import imageio; import keras; "
        "print(tf.__version__, torch.__version__)'"
    ),
    "mobile": "command -v jadx && command -v apktool",
    "linux": (
        "command -v ffuf && command -v katana && command -v pspy && command -v linpeas "
        "&& command -v smbclient && command -v sshpass && command -v ldapsearch "
        "&& python3 -c 'import impacket,bloodhound' "
        "&& (command -v nxc || command -v netexec) "
        "&& (command -v evil-winrm || gem list -i evil-winrm)"
    ),
}


async def probe(sb: DockerSandbox, cmd: str) -> tuple[int, str]:
    r = await sb.exec(cmd, timeout_s=120)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.exit_code, out[:300]


async def main() -> int:
    # Prefer packs that have donors ready; still try all listed in argv or default set
    want = sys.argv[1:] or [
        "web",
        "forensics",
        "steg",
        "pwn",
        "ghidra",
        "containers",
        "crypto",
        "mobile",
        "ml",
        "crypto-tools",
        "linux",
    ]

    chal = tempfile.mkdtemp(prefix="ctf-pack-smoke-")
    (Path(chal) / "challenge.txt").write_text("pack smoke\n", encoding="utf-8")

    sb = DockerSandbox(image="ctf-sandbox-core", challenge_dir=chal, memory_limit="16g")
    results: list[tuple[str, str, str]] = []
    try:
        print("starting L0…", flush=True)
        await sb.start()
        print("L0 up", flush=True)

        for pack in want:
            if pack not in PACK_SPECS:
                results.append((pack, "SKIP", "unknown pack"))
                continue
            print(f"\n=== ensure {pack} ===", flush=True)
            try:
                msg = await sb.ensure_pack(pack)
                print(f"  ensure: {msg}", flush=True)
            except Exception as e:
                results.append((pack, "FAIL", f"ensure exception: {e}"))
                print(f"  FAIL ensure: {e}", flush=True)
                continue

            if "Failed" in msg or "not installed" in msg or "not available" in msg:
                results.append((pack, "FAIL", msg))
                continue

            cmd = PROBES.get(pack)
            if not cmd:
                results.append((pack, "OK", "ensured (no probe)"))
                continue

            # Longer for sage / torch / angr
            timeout = 600 if pack in ("crypto", "ml", "pwn", "crypto-tools", "ghidra") else 180
            print(f"  probe: {cmd[:80]}…", flush=True)
            # use exec with higher timeout via inner
            r = await sb.exec(cmd, timeout_s=timeout)
            out = ((r.stdout or "") + (r.stderr or "")).strip()[:400]
            if r.exit_code == 0:
                results.append((pack, "OK", out or "probe ok"))
                print("  OK", flush=True)
            else:
                # containers: soft-ok if apt couldn't install nested runtime
                if pack == "containers" and r.exit_code != 0:
                    results.append((pack, "SOFT", f"exit {r.exit_code}: {out}"))
                    print(f"  SOFT (nested containers optional): {out}", flush=True)
                else:
                    results.append((pack, "FAIL", f"exit {r.exit_code}: {out}"))
                    print(f"  FAIL probe: {out}", flush=True)
    finally:
        try:
            await sb.stop()
        except Exception as e:
            print(f"stop: {e}", flush=True)

    print("\n======== SUMMARY ========")
    for pack, status, detail in results:
        print(f"{status:4}  {pack:14}  {detail[:120]}")
    hard_fail = [p for p, s, _ in results if s == "FAIL"]
    return 1 if hard_fail else 0


if __name__ == "__main__":
    # DockerSandbox.exec?
    raise SystemExit(asyncio.run(main()))
