"""Pydantic AI tool wrappers — thin delegation to backend.tools.core."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.tools.core import (
    do_bash,
    do_check_findings,
    do_list_files,
    do_read_file,
    do_web_fetch,
    do_webhook_create,
    do_webhook_get_requests,
    do_write_file,
)


async def bash(ctx: RunContext[SolverDeps], command: str, timeout_seconds: int = 60) -> str:
    """Execute a bash command inside the sandboxed Docker container.

    Distfiles are at /challenge/distfiles/ (read-only).
    Write generated/repaired files to /challenge/workspace/ (writable).
    Challenge services are reachable via host.docker.internal.
    Run `cat /tools.txt` to see all installed tools.
    """
    return await do_bash(ctx.deps.sandbox, command, timeout_seconds)


async def read_file(ctx: RunContext[SolverDeps], path: str) -> str:
    """Read a file from the container. For distfiles use paths like /challenge/distfiles/readme.txt."""
    return await do_read_file(ctx.deps.sandbox, path)


async def write_file(ctx: RunContext[SolverDeps], path: str, content: str) -> str:
    """Write a file into the container."""
    return await do_write_file(ctx.deps.sandbox, path, content)


async def list_files(ctx: RunContext[SolverDeps], path: str = "/challenge/distfiles") -> str:
    """List files in a directory inside the container."""
    return await do_list_files(ctx.deps.sandbox, path)


async def check_findings(ctx: RunContext[SolverDeps]) -> str:
    """Check for new findings from other agents working on the same challenge.

    Call this periodically to see if siblings have discovered useful information.
    """
    return await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)


async def notify_coordinator(ctx: RunContext[SolverDeps], message: str) -> str:
    """Send a message to the coordinator about a strategic discovery or request.

    Use this when you find something that affects the overall competition strategy,
    like discovering a flag format pattern, a shared vulnerability across challenges,
    or when you need help from other solvers.
    """
    if ctx.deps.notify_coordinator:
        try:
            await ctx.deps.notify_coordinator(message)
            return "Message sent to coordinator."
        except Exception as e:
            return f"Notification failed: {e}"
    return "No coordinator connected."


async def web_fetch(
    ctx: RunContext[SolverDeps], url: str, method: str = "GET", body: str = ""
) -> str:
    """Fetch a URL from the host. Useful for web challenges.

    Prefer bash+curl inside the sandbox for cookies/sessions.
    """
    return await do_web_fetch(url, method, body)


async def webhook_create(ctx: RunContext[SolverDeps]) -> str:
    """Create a webhook.site token for out-of-band HTTP callbacks (XSS, SSRF, bot challenges)."""
    return await do_webhook_create()


async def webhook_get_requests(ctx: RunContext[SolverDeps], uuid: str) -> str:
    """Retrieve HTTP requests received by a webhook.site token."""
    return await do_webhook_get_requests(uuid)
