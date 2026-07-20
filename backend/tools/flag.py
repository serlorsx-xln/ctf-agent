"""Flag submission tool."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.tools.core import do_submit_flag


async def submit_flag(ctx: RunContext[SolverDeps], flag: str) -> str:
    """Submit a recovered flag. Always call this when you have the real flag.

    Submit the exact string the challenge awards (PREFIX{...}, FLAG-..., or a
    compact formatless secret). Do not wrap or rewrite formats just to pass
    the checker.

    Returns CORRECT (challenge complete), ALREADY SOLVED, or REJECTED/INCORRECT.
    Do NOT submit placeholders like CTF{flag} or CTF{placeholder}.
    """
    if ctx.deps.submit_fn:
        display, is_confirmed = await ctx.deps.submit_fn(flag)
    else:
        display, is_confirmed = await do_submit_flag(ctx.deps.challenge_name, flag)
    if is_confirmed:
        ctx.deps.confirmed_flag = flag.strip()
    return display
