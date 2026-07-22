#!/usr/bin/env python3
"""Per-connection barb-metal session (replaces image /rf | qemu).

The stock wrapper pipes ``rf(flag, payload.bin)`` into qemu stdio, then keeps
relaying the TCP client. A bare ``cat flag payload | qemu`` closes stdin after
the payload → boot reaches barbOS> but later commands get EOF / no replies.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
QEMU = os.environ.get("BARB_METAL_QEMU", "qemu-system-i386")


def main() -> int:
    flag = (DIR / "flag").read_bytes()
    payload = (DIR / "payload.bin").read_bytes()
    qemu = [
        QEMU,
        "-display",
        "none",
        "-monitor",
        "none",
        "-no-reboot",
        "-nodefaults",
        "-snapshot",
        "-chardev",
        "stdio,id=char0,mux=off,signal=off",
        "-serial",
        "chardev:char0",
        "-m",
        "64M",
        "-kernel",
        str(DIR / "service"),
    ]
    proc = subprocess.Popen(
        qemu,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    assert proc.stdin is not None and proc.stdout is not None

    # Same as /rf: sendfile(flag) then sendfile(payload.bin), keep fd open.
    proc.stdin.write(flag)
    proc.stdin.write(payload)
    proc.stdin.flush()

    stdin_fd = sys.stdin.buffer.fileno()
    stdout_fd = sys.stdout.buffer.fileno()
    q_in = proc.stdin.fileno()
    q_out = proc.stdout.fileno()

    client_open = True
    while proc.poll() is None:
        rlist = [q_out]
        if client_open:
            rlist.append(stdin_fd)
        readable, _, _ = select.select(rlist, [], [], 0.5)
        if q_out in readable:
            data = os.read(q_out, 4096)
            if not data:
                break
            os.write(stdout_fd, data)
        if client_open and stdin_fd in readable:
            data = os.read(stdin_fd, 4096)
            if not data:
                # Client closed — leave qemu stdin open (no EOF to UART).
                client_open = False
                continue
            os.write(q_in, data)

    try:
        proc.kill()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
