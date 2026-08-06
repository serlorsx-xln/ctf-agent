"""Attribute swarm log records to the runner that caused them.

Sandbox / pack / boot code logs through plain module loggers that know nothing
about which solver started them. In a multi-agent swarm those records interleave
on one stderr stream, so the TUI sees several identical untagged boot lines and
cannot tell whose container is starting. Each solver task binds its runner here
and the filter stamps records that are not already tagged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_agent: ContextVar[str] = ContextVar("artemis_log_agent", default="")


@contextmanager
def log_agent(tag: str) -> Iterator[None]:
    """Bind ``tag`` (``challenge/runner``) for the current task's log records."""
    token = _current_agent.set(tag or "")
    try:
        yield
    finally:
        _current_agent.reset(token)


def current_log_agent() -> str:
    return _current_agent.get()


class AgentTagFilter(logging.Filter):
    """Prefix untagged records with the owning runner.

    Attach to the *handler*, not a logger: logger-level filters only see records
    created by that logger, never the propagated ones we care about.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tag = _current_agent.get()
        if not tag:
            return True
        msg = str(record.msg)
        # Callers that already tag themselves ("[challenge/runner] …") win.
        if msg.startswith("["):
            return True
        record.msg = f"[{tag}] {msg}"
        return True
