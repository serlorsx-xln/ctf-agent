"""Shared first-print gate for Cursor quota outcomes across solvers."""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary

_printed: WeakKeyDictionary[asyncio.Event, bool] = WeakKeyDictionary()


def claim_quota_outcome_print(ev: asyncio.Event) -> bool:
    """Return True once per cancel-event so global ERROR lines are not duplicated."""
    if _printed.get(ev):
        return False
    _printed[ev] = True
    return True
