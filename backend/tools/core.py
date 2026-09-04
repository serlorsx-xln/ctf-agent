"""SDK-agnostic tool logic — pure async functions, no Pydantic AI types."""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Callable
from pathlib import Path

import httpx

MAX_OUTPUT = 24_000


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head = "\n".join(lines[:200])
    return head[:limit] + f"\n... [truncated — {len(text)} total chars, {len(lines)} lines]"


async def _try_lazy_pip(
    sandbox,
    command: str,
    timeout_seconds: int,
    result,
    out: str,
) -> str | None:
    """Install a one-shot pip package (e.g. angr) and retry the command."""
    from backend.tool_router import infer_lazy_pip

    pkg = infer_lazy_pip(command, result.stderr or "", result.stdout or "")
    if not pkg:
        return None
    done = getattr(sandbox, "_lazy_pip_done", None)
    if done is None:
        done = set()
        sandbox._lazy_pip_done = done
    if pkg in done:
        return None
    done.add(pkg)
    pip_r = await sandbox.exec(f"pip3 install {shlex.quote(pkg)}", timeout_s=600)
    if pip_r.exit_code != 0:
        return f"{out}\n\n[sandbox] lazy pip {pkg} failed\n{_format_exec(pip_r)}"
    result2 = await sandbox.exec(command, timeout_s=timeout_seconds)
    return f"[sandbox] pip installed {pkg}\n\n{_format_exec(result2)}"


async def do_bash(sandbox, command: str, timeout_seconds: int = 300) -> str:
    from backend.tool_router import (
        infer_pack_from_failure,
        parse_ensure_pack_command,
    )

    ensure_arg = parse_ensure_pack_command(command)
    if ensure_arg is not None:
        if hasattr(sandbox, "ensure_pack"):
            return await sandbox.ensure_pack(ensure_arg)
        return "Additional tool install is not available in this sandbox."

    result = await sandbox.exec(command, timeout_s=timeout_seconds)
    out = _format_exec(result)

    # Also ensure on "command not found" when pipelines hide a non-zero exit
    # (e.g. `nmap … | head` → head exits 0).
    blob = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    missing_cmd = "command not found" in blob
    if result.exit_code == 0 and not missing_cmd:
        return out
    if not hasattr(sandbox, "ensure_pack"):
        lazy = await _try_lazy_pip(sandbox, command, timeout_seconds, result, out)
        return lazy if lazy is not None else out

    pack = infer_pack_from_failure(command, result.stderr, result.stdout)
    if not pack:
        lazy = await _try_lazy_pip(sandbox, command, timeout_seconds, result, out)
        return lazy if lazy is not None else out
    if pack in getattr(sandbox, "ensured_packs", ()):
        lazy = await _try_lazy_pip(sandbox, command, timeout_seconds, result, out)
        return lazy if lazy is not None else out

    ensure_msg = await sandbox.ensure_pack(pack)
    # If ensure failed, don't retry forever.
    if ensure_msg.startswith("Failed") or "not installed" in ensure_msg:
        return f"{out}\n\n[sandbox] {ensure_msg}"

    result2 = await sandbox.exec(command, timeout_s=timeout_seconds)
    if result2.exit_code == 0:
        return f"[sandbox] {ensure_msg}\n\n{_format_exec(result2)}"
    lazy = await _try_lazy_pip(
        sandbox, command, timeout_seconds, result2, _format_exec(result2)
    )
    if lazy is not None:
        return f"[sandbox] {ensure_msg}\n\n{lazy}"
    return f"[sandbox] {ensure_msg}\n\n{_format_exec(result2)}"


def _format_exec(result) -> str:
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.exit_code != 0:
        parts.append(f"[exit {result.exit_code}]")
        from backend.loop_detect import RESOURCE_HINTS

        hint = RESOURCE_HINTS.get(result.exit_code)
        if hint:
            parts.append(hint)
    out = "\n".join(parts).strip() or "(no output)"
    return _truncate(out)


def _humanize_read_error(path: str, exc: BaseException) -> str:
    """Turn Docker/archive 404s into a short path-only miss."""
    text = str(exc)
    low = text.lower()
    if (
        isinstance(exc, FileNotFoundError)
        or "could not find the file" in low
        or "no such file" in low
        or "no file found" in low
        or ("404" in low and "file" in low)
    ):
        return f"File not found: {path}"
    return f"Error reading file: {exc}"


async def do_read_file(sandbox, path: str) -> str:
    try:
        data = await sandbox.read_file(path)
    except Exception as e:
        return _humanize_read_error(path, e)

    if isinstance(data, bytes):
        sample = data[:4096]
        non_text = sum(
            1
            for b in sample
            if b == 0 or (b < 9 and b not in (7, 8)) or (9 < b < 13) or (13 < b < 32 and b != 27)
        )
        if len(sample) > 0 and non_text / len(sample) > 0.05:
            return (
                f"Binary file ({len(data)} bytes) — use bash to inspect it:\n"
                f"  file {path}\n"
                f"  xxd {path} | head -40\n"
                f"  strings {path}\n"
                f"  exiftool {path}\n"
                f"  binwalk {path}"
            )
        return _truncate(data.decode("utf-8", errors="replace"))

    return _truncate(data) if isinstance(data, str) else data


async def do_write_file(sandbox, path: str, content: str) -> str:
    try:
        await sandbox.write_file(path, content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


async def do_list_files(sandbox, path: str = "/challenge/distfiles") -> str:
    result = await sandbox.exec(f"ls -la {shlex.quote(path)}")
    out = result.stdout.strip()
    if result.exit_code != 0:
        return result.stderr.strip() or f"Error listing {path}"
    return out or f"{path} is empty."


async def do_submit_flag(
    _challenge_name: str,
    flag: str,
    *,
    already_accepted: list[str] | tuple[str, ...] = (),
    required: int = 1,
    challenge_dir: str | None = None,
    auto_confirm: bool = False,
    confirm_fn: Callable[[str], bool | tuple[bool, str]] | None = None,
    by: str = "",
) -> tuple[str, bool]:
    """Submit a flag locally with human confirmation.

    Returns (display_message, challenge_complete). Hard rejects (empty/decoy/
    artifact) skip the prompt. Other candidates ask the operator (or
    ``confirm_fn`` / auto-confirm) before counting toward ``required``.
    A confirm may answer ``(ok, reason)``; a rejection reason is relayed to the
    solver so it can tell a wrong flag from a wrong technique.
    ``_challenge_name`` is kept for call-site compatibility / logging only.
    ``by`` names the submitting runner in the accept message.
    """
    from backend.flags import (
        accept_flag,
        normalize_confirm,
        normalize_flags_required,
        prompt_flag_confirmation,
    )

    req = normalize_flags_required(required)
    prior = list(already_accepted)
    preview, done = accept_flag(
        flag,
        already_accepted=prior,
        required=req,
        challenge_dir=challenge_dir,
        human_confirmed=False,
    )
    if not preview.startswith("CANDIDATE"):
        # ALREADY SOLVED must not propagate challenge_complete=True — solo
        # solvers and bridge soft-submit would treat it as a fresh CORRECT.
        if preview.startswith("ALREADY SOLVED"):
            return preview, False
        return preview, done

    confirm = confirm_fn or (lambda f: prompt_flag_confirmation(f, auto_confirm=auto_confirm))
    ok, reason = normalize_confirm(await asyncio.to_thread(confirm, (flag or "").strip()))
    if not ok:
        f = (flag or "").strip()
        # Relay the operator's reason verbatim — a bare "no" leaves the solver
        # unable to tell a wrong flag from a wrong technique, and it starts
        # resubmitting variants or doubting the checker instead of moving on.
        why = f" Operator says: {reason}" if reason else ""
        return (
            f'REJECTED by operator — "{f}" not confirmed.{why} Continue hunting.',
            False,
        )
    return accept_flag(
        flag,
        already_accepted=prior,
        required=req,
        challenge_dir=challenge_dir,
        human_confirmed=True,
        by=by,
    )


def _is_internal_url(url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith("169.254.") or host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second_octet = int(host.split(".")[1])
            if 16 <= second_octet <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return False


async def do_web_fetch(url: str, method: str = "GET", body: str = "") -> str:
    if _is_internal_url(url):
        return "Fetch error: access to internal/private networks is blocked."
    try:
        # verify=False: CTF challenge services often use self-signed certs
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.request(
                method,
                url,
                content=body or None,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            text = resp.text
            prefix = f"HTTP {resp.status_code} {resp.reason_phrase}\n{'─' * 40}\n"
            if len(text) > 20_000:
                text = text[:20_000] + f"\n... [truncated, total {len(resp.text)} bytes]"
            return prefix + text
    except Exception as e:
        return f"Fetch error: {e}"


async def do_webhook_create() -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post("https://webhook.site/token")
            # webhook.site returns 201 Created on success (not 200).
            if resp.status_code not in (200, 201):
                return f"webhook.site error: HTTP {resp.status_code}"
            data = resp.json()
            return json.dumps({"uuid": data["uuid"], "url": f"https://webhook.site/{data['uuid']}"})
    except Exception as e:
        return f"webhook_create error: {e}"


async def do_webhook_get_requests(uuid: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://webhook.site/token/{uuid}/requests")
            if resp.status_code not in (200, 201):
                return f"webhook.site error: HTTP {resp.status_code}"
            data = resp.json()
            if not data.get("data"):
                return "No requests received yet."
            out = json.dumps(data["data"], indent=2)
            return out[:8000] if len(out) > 8000 else out
    except Exception as e:
        return f"webhook_get_requests error: {e}"


async def do_check_findings(
    message_bus,
    model_spec: str,
    *,
    runner_id: str | None = None,
) -> str:
    """Get unread findings from sibling solvers.

    Send-now (**steer**) notes are claimed by provider interrupt watchers
    (Cursor force-followup / ``backend.agents.soft_steer``), not here — so a
    mid-tool tick cannot race and swallow an interrupt. **Queue** rows wait for
    ``do_claim_soft_queue`` at turn idle.

    ``runner_id`` must be the swarm-assigned id (may include ``#N``) so fan-out
    inbox targets match ``agent_display_key`` / TUI roster labels.
    """
    if not message_bus:
        return "No message bus available."
    rid = (runner_id or model_spec or "").strip()
    findings = await message_bus.check(rid)
    if findings:
        formatted = message_bus.format_unread(findings)
        if formatted:
            return formatted
    return "No new findings from other agents."


async def do_claim_soft_queue(
    message_bus,
    model_spec: str,
    *,
    runner_id: str | None = None,
) -> list[str]:
    """Drain **queue** operator notes at soft-solver turn idle (no bus broadcast)."""
    if not message_bus:
        return []
    rid = (runner_id or model_spec or "").strip()
    spec = (model_spec or rid).strip()
    try:
        import os

        from backend.models import agent_display_key
        from backend.operator_inbox import drain_operator_notes_to_bus

        claimer = agent_display_key(rid, spec)
        require_target = os.environ.get("ARTEMIS_CURSOR_OWNS_INBOX") == "1"
        return await drain_operator_notes_to_bus(
            message_bus,
            delivery="queue",
            claimer=claimer,
            require_target=require_target,
            broadcast=False,
        )
    except Exception:
        return []


async def soft_idle_operator_notes(
    message_bus,
    model_spec: str,
    *,
    runner_id: str | None = None,
    cancel_event=None,
    confirmed: bool = False,
) -> str | None:
    """Claim Queue (+ any pending steer) at soft-solver turn idle/start.

    Returns text to append to the next prompt, or ``None`` when nothing new.
    """
    if not message_bus:
        return None
    # Dying siblings must not steal unscoped notes parked for Hold.
    # Hold winner (confirmed + cancelled) may still claim.
    if (
        cancel_event is not None
        and getattr(cancel_event, "is_set", lambda: False)()
        and not confirmed
    ):
        return None
    queue_notes = await do_claim_soft_queue(message_bus, model_spec, runner_id=runner_id)
    # Pending Send-now that arrived between turns (watcher not running).
    steer_notes: list[str] = []
    try:
        from types import SimpleNamespace

        from backend.agents.soft_steer import claim_soft_steer_notes

        steer_notes = await claim_soft_steer_notes(
            SimpleNamespace(
                message_bus=message_bus,
                model_spec=model_spec,
                runner_id=runner_id or model_spec,
                cancel_event=cancel_event,
                _confirmed=confirmed,
            )
        )
    except Exception:
        steer_notes = []
    rest = await do_check_findings(message_bus, model_spec, runner_id=runner_id)
    parts: list[str] = []
    op_notes = [n for n in (queue_notes + steer_notes) if str(n or "").strip()]
    if op_notes:
        from backend.prompts import fence_untrusted

        parts.append(
            "**Operator notes:**\n\n"
            + fence_untrusted("\n\n".join(str(n) for n in op_notes), kind="operator")
        )
    if rest and "No new findings from other agents." not in rest:
        parts.append(rest)
    if not parts:
        return None
    return "\n\n".join(parts)


# Image constants (shared with vision wrapper)
IMAGE_EXTS_FOR_VISION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}

IMAGE_MAGIC: dict[str, list[int]] = {
    "image/png": [0x89, 0x50, 0x4E, 0x47],
    "image/jpeg": [0xFF, 0xD8, 0xFF],
    "image/gif": [0x47, 0x49, 0x46],
    "image/bmp": [0x42, 0x4D],
    "image/webp": [0x52, 0x49, 0x46, 0x46],
}

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB

# When pixels cannot be delivered (too large / corrupt / bridge), steer to local tools.
VISION_BASH_FALLBACK = (
    "Use bash tools on the file instead: steghide, zsteg, exiftool, strings, xxd, binwalk."
)


def _has_valid_magic(data: bytes, mime_type: str) -> bool:
    magic = IMAGE_MAGIC.get(mime_type)
    if not magic:
        return True
    return all(i < len(data) and data[i] == b for i, b in enumerate(magic))


async def do_view_image(sandbox, filename: str, use_vision: bool = True) -> tuple[bytes, str] | str:
    """Load image bytes for multimodal delivery (or an error string).

    ``use_vision`` is kept for call-site compatibility; we always attempt to
    load bytes. Callers that cannot attach pixels should surface
    ``VISION_BASH_FALLBACK`` rather than blocking on a model allowlist.
    """
    _ = use_vision  # no allowlist gate — try deliver, else bash tools
    # Strip leading path if model passes full container path
    basename = Path(filename).name
    ext = Path(basename).suffix.lower()
    mime_type = IMAGE_EXTS_FOR_VISION.get(ext)
    if not mime_type:
        return f"Not a supported image type: {filename}"

    # Try the filename as-is first (if it's an absolute path), then search standard dirs
    search_paths = []
    if filename.startswith("/"):
        search_paths.append(filename)
    search_paths.extend([f"/challenge/distfiles/{basename}", f"/challenge/workspace/{basename}"])

    for path in search_paths:
        try:
            data = await sandbox.read_file_bytes(path)
            if not _has_valid_magic(data, mime_type):
                return (
                    "Cannot load image: file appears invalid or corrupted. "
                    "Fix the magic bytes in the sandbox first, save to /challenge/workspace/, "
                    f"then call view_image again. {VISION_BASH_FALLBACK}"
                )
            if len(data) > MAX_IMAGE_BYTES:
                return (
                    f"Image too large for vision ({len(data) / 1024 / 1024:.1f} MB > 4 MB limit). "
                    f"{VISION_BASH_FALLBACK}"
                )
            return (data, mime_type)
        except Exception:
            continue

    return f"File not found: {filename} (searched: {', '.join(search_paths)})"
