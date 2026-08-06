"""Single source of truth for the Artemis cache directory.

``ARTEMIS_CACHE`` env (default ``~/.cache/artemis``). Used by the daemon,
cost tracker, flags, sandbox session, and challenge materialization so they
all agree on the same path.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT = Path.home() / ".cache" / "artemis"


def cache_dir() -> Path:
    """Resolve the Artemis cache directory (creating it is the caller's job)."""
    return Path(os.environ.get("ARTEMIS_CACHE", str(_DEFAULT)))
