"""Reset session artifacts before a new swarm run (daemon or direct CLI)."""

from __future__ import annotations


def reset_for_new_swarm(session_id: str | None = None) -> None:
    """Cancel pending confirms, clear handshakes/flags progress, wipe usage file."""
    from backend.cost_tracker import clear_published_usage
    from backend.flags import cancel_flag_confirmation
    from backend.shell.sandbox_session import (
        clear_tui_handshakes,
        reset_accepted_flags,
        resolve_session_id,
    )

    sid = resolve_session_id(session_id)
    cancel_flag_confirmation()
    clear_tui_handshakes(sid)
    reset_accepted_flags(sid)
    clear_published_usage(sid)
