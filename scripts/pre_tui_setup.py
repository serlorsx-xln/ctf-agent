#!/usr/bin/env python3
"""Launcher entry: pre-TUI setup prompt (see backend.launch_setup)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.launch_setup import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
