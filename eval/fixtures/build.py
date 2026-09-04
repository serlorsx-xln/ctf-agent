#!/usr/bin/env python3
"""Generate owned Jeopardy fixtures (text + Linux ELFs via ctf-sandbox-core)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.jeopardy_fixtures import build_all, write_text_fixtures  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--text-only",
        action="store_true",
        help="Skip docker gcc (rev/pwn binaries missing until a full build)",
    )
    args = p.parse_args()
    if args.text_only:
        write_text_fixtures()
        print("wrote text fixtures (no ELFs)")
        return
    build_all(require_elfs=True)
    print("wrote fixtures including Linux ELFs")


if __name__ == "__main__":
    main()
