"""Turn-continue / bump copy that does not sabotage working paths."""

from __future__ import annotations

from backend.flags import normalize_flags_required

INFRA_RECOVERY_BLURB = (
    "NOTE: The previous Cursor agent session was interrupted by a transport/"
    "bridge timeout or internal error — not because your approach failed. "
    "Sandbox files under /challenge/workspace are intact. Resume from existing "
    "scripts and results; do not restart reverse-engineering from scratch. "
    "For long jobs, pass a large timeout_seconds (300–900+) and print progress."
)


PIVOT_PRESSURE = (
    "A full turn on your previous approach produced no accepted flag, so treat it "
    "as unproven: do not resume it with only new parameters or flags. State in one "
    "line what you have ruled out, then attack a different surface or technique. "
    "Existing scripts and results under /challenge/workspace stay valid — reuse the "
    "findings, not the dead technique."
)


def build_continue_prompt(
    *,
    accepted_flags: list[str] | tuple[str, ...] = (),
    flags_required: int = 1,
    bump_insights: str | None = None,
    infra_recovery: bool = False,
    session_sync: bool = False,
) -> str:
    """Prompt for the next solver turn after GAVE_UP / bump / resume.

    Three regimes, keyed on the evidence actually available:

    * Partial accepts — a path demonstrably works, so keep the agent on it.
    * Infra recovery — the turn died on a transport/bridge error, not on the
      approach, so resume rather than pivot.
    * Zero accepts, real failure — nothing is proven, so push off the technique
      that just burned a whole turn. Never invent technique playbooks.
    """
    accepted = [f for f in accepted_flags if f]
    if session_sync:
        try:
            from backend.shell.sandbox_session import load_session_state

            for f in load_session_state().get("accepted_flags") or []:
                fs = str(f).strip()
                if fs and fs not in accepted:
                    accepted.append(fs)
        except Exception:
            pass
    required = normalize_flags_required(flags_required)
    n = len(accepted)
    insights = (bump_insights or "").strip()
    prefix = f"{INFRA_RECOVERY_BLURB}\n\n" if infra_recovery else ""

    if accepted and n < required:
        progress = (
            f"Progress: {n}/{required} distinct flag(s) already accepted: "
            + " | ".join(accepted)
            + "."
        )
        if insights:
            return (
                f"{prefix}{progress}\n\n"
                f"Insights from other agents:\n\n{insights}\n\n"
                "Continue for the remaining flag(s). Refine the path that worked "
                "(same foothold / credentials / access). Change technique only if "
                "insights show the current path is wrong."
            )
        return (
            f"{prefix}{progress}\n\n"
            "Continue for the remaining flag(s). Build on what already worked — "
            "do not abandon a working path for a random new approach."
        )

    # Interrupted by infrastructure, not by a wrong approach — resuming is correct.
    if infra_recovery:
        if insights:
            return (
                f"{prefix}"
                f"Insights from other agents:\n\n{insights}\n\n"
                "Resume where you left off. Prefer adjusting parameters, targets, or "
                "tool flags before abandoning a promising technique."
            )
        return (
            f"{prefix}"
            "Resume where you left off. Prefer adjusting parameters, targets, or tool "
            "flags before abandoning a promising technique."
        )

    if insights:
        return (
            "Your previous attempt did not finish the challenge. "
            f"Insights from other agents:\n\n{insights}\n\n"
            f"{PIVOT_PRESSURE} Use the insights to choose the next surface. "
            "Do not blindly repeat identical failing commands."
        )

    return f"Continue solving. {PIVOT_PRESSURE}"
