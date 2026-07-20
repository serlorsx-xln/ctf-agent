"""Flag submission tool."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.flags import is_counted_accept_message, normalize_flags_required
from backend.tools.core import do_submit_flag


async def submit_flag(ctx: RunContext[SolverDeps], flag: str) -> str:
    """Submit a recovered flag. Always call this when you have the real flag.

    Submit the exact string the challenge awards (PREFIX{...}, FLAG-..., or a
    compact formatless secret). Do not wrap or rewrite formats just to pass
    the checker.

    Returns ACCEPTED (n/m) if more flags are needed, CORRECT when complete,
    or REJECTED. When a swarm already finished, may return ALREADY SOLVED.
    Do NOT submit placeholders like CTF{flag} or CTF{placeholder}.
    """
    if ctx.deps.submit_fn:
        display, is_confirmed = await ctx.deps.submit_fn(flag)
    else:
        display, is_confirmed = await do_submit_flag(
            ctx.deps.challenge_name,
            flag,
            already_accepted=list(ctx.deps.accepted_flags),
            required=normalize_flags_required(ctx.deps.flags_required),
        )
    if is_counted_accept_message(display):
        normalized = flag.strip()
        if normalized and normalized not in ctx.deps.accepted_flags:
            ctx.deps.accepted_flags.append(normalized)
    if is_confirmed:
        ctx.deps.confirmed_flag = (
            " | ".join(ctx.deps.accepted_flags) if ctx.deps.accepted_flags else flag.strip()
        )
    return display
