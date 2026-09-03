"""Soft-solver Send-now (steer) claim helpers — Claude / Codex / Gemini.

Soft solvers poll the operator inbox during a long turn, claim **steer** rows
for their display key (``broadcast=False``), and interrupt their provider turn
when possible (Claude ``interrupt``, Codex ``turn/interrupt``, Gemini cancel
in-flight generate). Cursor uses the same inbox via ``cursor_solver``.
"""

from __future__ import annotations

from typing import Any


def soft_claimer_key(solver: Any) -> str:
    from backend.models import agent_display_key

    rid = str(getattr(solver, "runner_id", None) or getattr(solver, "model_spec", "") or "").strip()
    spec = str(getattr(solver, "model_spec", None) or rid).strip()
    if rid and spec:
        return agent_display_key(rid, spec)
    name = str(getattr(solver, "agent_name", "") or "")
    return name.split("/", 1)[-1] if name else rid


async def claim_soft_steer_notes(solver: Any) -> list[str]:
    """Drain steer notes for this soft solver (no bus broadcast)."""
    import os

    from backend.operator_inbox import drain_operator_notes_to_bus

    # Dying siblings must not vacuum unscoped notes parked for Hold.
    # Hold winner keeps ``_confirmed`` after cancel — still claim for follow-ups.
    cancelled = getattr(getattr(solver, "cancel_event", None), "is_set", lambda: False)()
    if cancelled and not getattr(solver, "_confirmed", False):
        return []

    bus = getattr(solver, "message_bus", None)
    claimer = soft_claimer_key(solver)
    require_target = os.environ.get("ARTEMIS_CURSOR_OWNS_INBOX") == "1"
    return await drain_operator_notes_to_bus(
        bus,
        delivery="steer",
        claimer=claimer,
        require_target=require_target,
        broadcast=False,
    )


def operator_interrupt_prompt(notes: list[str], continue_body: str) -> str:
    """Build the follow-up user message after a soft interrupt."""
    body = "\n\n".join(f"Operator note: {n}" for n in notes if str(n or "").strip())
    if not body:
        return continue_body
    return (
        "The operator interrupted with the following (Send now). "
        "Acknowledge briefly and continue solving with this guidance.\n\n"
        f"{body}\n\n---\n{continue_body}"
    )


def restore_pending_soft_notes(solver: Any, notes: list[str] | None = None) -> None:
    """Re-queue undelivered Send-now notes (CORRECT / cancel / error exits).

    Notes are rewritten as ``queue`` so Hold/idle drain can pick them up.
    Emit a matching ``you (queue…)`` crumb so the TUI sticky Queue bar stays
    honest (the original steer crumb alone would look like the note vanished).
    """
    from backend.agents.live_log import emit_line
    from backend.operator_inbox import append_operator_note

    pending = notes
    if pending is None:
        pending = list(getattr(solver, "_pending_soft_notes", None) or [])
        try:
            solver._pending_soft_notes.clear()
        except Exception:
            pass
        try:
            solver._force_followup.clear()
        except Exception:
            pass
    cleaned = [n for n in pending if str(n or "").strip()]
    if not cleaned:
        return
    claimer = soft_claimer_key(solver)
    # Sibling cancel after CORRECT: park notes unscoped so Hold's winner can claim.
    # Keep scoped restore when this solver itself confirmed (winner path).
    cancelled = bool(
        getattr(getattr(solver, "cancel_event", None), "is_set", lambda: False)()
    )
    if cancelled and not getattr(solver, "_confirmed", False):
        claimer = None
    for text in cleaned:
        try:
            append_operator_note(text, delivery="queue", target=claimer)
            scope = f"→{claimer}" if claimer else ""
            emit_line(f"[artemis] you (queue{scope}): {text[:2000]}")
        except Exception:
            pass
