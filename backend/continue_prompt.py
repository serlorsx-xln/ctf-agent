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


def build_continue_prompt(
    *,
    accepted_flags: list[str] | tuple[str, ...] = (),
    flags_required: int = 1,
    bump_insights: str | None = None,
    infra_recovery: bool = False,
) -> str:
    """Prompt for the next solver turn after GAVE_UP / bump / resume.

    Partial accepts keep the agent on the working path. Full bumps without
    accepts may suggest a different approach. Never invent technique playbooks.
    """
    accepted = [f for f in accepted_flags if f]
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

    if insights:
        return (
            f"{prefix}"
            "Your previous attempt did not finish the challenge. "
            f"Insights from other agents:\n\n{insights}\n\n"
            "Use the insights. Prefer adjusting parameters/flags of the current "
            "technique before switching to an unrelated approach. "
            "Do not blindly repeat identical failing commands."
        )

    return (
        f"{prefix}"
        "Continue solving. Prefer adjusting parameters, targets, or tool flags "
        "before abandoning a promising technique."
    )
