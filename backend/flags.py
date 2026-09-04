"""Local flag submission with human confirmation — no external scoreboard.

Agents may submit any non-empty candidate. Obvious decoys / packaging artifacts
are rejected automatically. Everything else becomes a **CANDIDATE** until a
human confirms it. Only confirmed flags count toward ``flags_required: N``
and ``CORRECT``.

Without a scoreboard, the operator is the oracle — format heuristics must not
auto-complete a run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

_DECOY_MARKERS = (
    "fake_flag",
    "fake-flag",
    "fakeflag",
    "placeholder",
    "tryharder",
    "try_harder",
    "try-harder",
    "ctf{flag}",
    "flag{flag}",
    "default_flag",
    # leakme-style local decoy file body (typo intentional)
    "thie_is_test",
    "your_flag_here",
    "insert_flag",
    "put_flag_here",
    "flag_goes_here",
    "flag_here",
    "not_the_flag",
    "nottheflag",
    "wrong_flag",
    "dummy_flag",
    "dummyflag",
    "this_is_a_flag",
    "thisisaflag",
    "todo_flag",
    "lorem_ipsum",
    "loremipsum",
    "replace_me",
    "put_your_flag",
    "insert_your_flag",
    "redacted",
    "censored",
    "coming_soon",
    "changeme",
)
# Whole-string / body-only decoys (avoid substring hits on real flags)
_DECOY_EXACT = frozenset(
    {
        "test_flag",
        "this_is_test_flag",
        "thie_is_test_flag",
        "local_test_flag",
        "example_flag",
        "sample_flag",
        "flag",
        "the_flag",
        "test",
        "todo",
        "tbd",
        "wip",
        "xxx",
        "...",
        "…",
        "???",
        "***",
        "your_flag",
        "not_a_flag",
        "notflag",
        "dummy",
        "sample",
        "example",
        "foo",
        "bar",
        "baz",
        "hello",
        "hello_world",
        "password",
        "admin",
        "secret",
        "none",
        "null",
        "undefined",
        "n/a",
        "na",
        "true",
        "false",
        "yes",
        "no",
        "success",
        "correct",
        "winner",
        "you_win",
        "congratulations",
        "decoy",
        "1234",
        "12345",
        "123456",
        "password123",
    }
)
# 4→a 0→o etc. Applied to the brace body only (never as a substring of a long flag).
_LEET_TABLE = str.maketrans("043571!@$", "oaestliaa")
_PUNCT_BODY = re.compile(r"^[.?!*_\-–—•·x\s]{1,16}$", re.IGNORECASE)
_REPEAT_BODY = re.compile(r"^(.)\1+$")

# Formatless secret token (no whitespace)
_FLAG_TOKEN = re.compile(r"^[A-Za-z0-9_+\/=-]{16,200}$")
_LICENSE_LIKE = re.compile(r"^(?:[A-Za-z0-9]{1,5}-){2,}[A-Za-z0-9]{1,5}$")
# Dockerfile-style flag *filename* (leakme): ENV FLAG flag_<md5/sha>
_FLAG_FILENAME_TOKEN = re.compile(r"^flag_[0-9a-f]{16,128}$", re.IGNORECASE)
_ENV_FLAG_ASSIGN = re.compile(
    r"^\s*(?:ENV|ARG)\s+FLAG(?:\s+|=)\s*[\"']?([^\s\"'#]+)",
    re.IGNORECASE | re.MULTILINE,
)

# Optional in challenge.txt: flags_required: 2  (default 1 if omitted)
# NOTE: flags_required is no longer parsed from challenge text — the operator
# is always asked via the TUI digits dialog. normalize_flags_required clamps
# the dialog answer.

_ARTIFACT_TEXT_NAMES = frozenset(
    {
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        ".env",
        ".env.example",
        "challenge.txt",
        "metadata.yml",
        "metadata.yaml",
        "readme.md",
        "readme.txt",
    }
)
_SKIP_BASENAME_REJECT = frozenset(
    {
        "challenge.txt",
        "challenge.md",
        "readme.md",
        "readme.txt",
        "dockerfile",
        "metadata.yml",
        "metadata.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "flag.txt",
        "flag",
        ".gitkeep",
        ".ds_store",
    }
)
_MAX_ARTIFACT_FILE_BYTES = 256_000
_MAX_ARTIFACT_WALK_FILES = 400

ConfirmFn = Callable[[str], bool]


def normalize_flags_required(value: int | None) -> int:
    """Clamp / default a flags_required value to >= 1."""
    if value is None:
        return 1
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def is_counted_accept_message(message: str) -> bool:
    """True if this flag should be tracked locally as already accepted.

    Includes swarm ``Already accepted this flag`` so siblings sync progress
    without re-submitting.
    """
    return bool(message) and (
        message.startswith("ACCEPTED")
        or message.startswith("CORRECT")
        or message.startswith("Already accepted")
    )


def _flag_body(flag_lower: str) -> str:
    if "{" in flag_lower and flag_lower.endswith("}"):
        return flag_lower[flag_lower.find("{") + 1 : -1]
    return flag_lower


def flag_core(flag: str) -> str:
    """Inner body of PREFIX{body}, else the stripped flag itself.

    Used to detect rewraps of already-tried intermediate tokens
    (e.g. ``deadbeef`` → ``v1t{deadbeef}``) without enforcing a prefix.
    """
    f = (flag or "").strip()
    if "{" in f and f.endswith("}"):
        return f[f.find("{") + 1 : -1].strip()
    return f


def is_rewrap_of_tried(flag: str, tried: Sequence[str]) -> str | None:
    """If ``flag`` is a brace-wrap / unwrap variant of a prior try, return that prior.

    Does not enforce flag prefix — only catches recycling the same core token.
    """
    f = (flag or "").strip()
    if not f or not tried:
        return None
    core = flag_core(f)
    if not core:
        return None
    core_l = core.lower()
    f_l = f.lower()
    for prev in tried:
        p = (prev or "").strip()
        if not p or p == f:
            continue
        p_core = flag_core(p)
        if not p_core:
            continue
        # Same core under different wrapping (or raw vs wrapped)
        if core_l == p_core.lower() or core_l == p.lower() or f_l == p_core.lower():
            return p
    return None


def _leet_plain(text: str) -> str:
    return (text or "").lower().replace("-", "_").translate(_LEET_TABLE)


def _structural_decoy_body(body: str) -> bool:
    """Empty / punctuation / single-char / repeated-char brace bodies (CTF{}, CTF{...})."""
    b = (body or "").strip()
    if not b or len(b) == 1:
        return True
    if _PUNCT_BODY.fullmatch(b):
        return True
    return bool(_REPEAT_BODY.fullmatch(b))


def is_decoy_flag(flag: str) -> bool:
    raw = (flag or "").strip()
    if not raw:
        return True
    f = raw.lower()
    body = _flag_body(f)
    if _structural_decoy_body(body):
        return True
    if any(m in f for m in _DECOY_MARKERS):
        return True
    compact = f.replace("-", "_")
    body_compact = body.replace("-", "_")
    leet_body = _leet_plain(body)
    if f in _DECOY_EXACT or body in _DECOY_EXACT:
        return True
    if compact in _DECOY_EXACT or body_compact in _DECOY_EXACT:
        return True
    if leet_body in _DECOY_EXACT:
        return True
    # Leetspeak of instructional markers (f4ke_fl4g → fake_flag), body-only.
    return any(m == leet_body or m in leet_body for m in _DECOY_MARKERS)


def is_filename_like_flag_token(flag: str) -> bool:
    """True for bare ``flag_<hex>`` tokens (often Dockerfile ENV / on-disk names)."""
    return bool(_FLAG_FILENAME_TOKEN.match((flag or "").strip()))


def _looks_secret_token(f: str) -> bool:
    """Compact formatless flag: mixed charset, not a license/PIN/filename pattern."""
    if not _FLAG_TOKEN.match(f) or _LICENSE_LIKE.match(f):
        return False
    if is_filename_like_flag_token(f):
        return False
    classes = sum(
        (
            any(c.islower() for c in f),
            any(c.isupper() for c in f),
            any(c.isdigit() for c in f),
        )
    )
    return classes >= 2


def collect_artifact_flag_candidates(challenge_dir: str | Path | None) -> set[str]:
    """Strings that look like packaging artifacts, not awarded flags.

    Collects Dockerfile/compose ``ENV|ARG FLAG=...`` values and attachment
    basenames that resemble flag filenames. Used to reject false accepts from
    skimming distfiles (e.g. leakme ``ENV FLAG flag_<hex>``).
    """
    if not challenge_dir:
        return set()
    root = Path(challenge_dir)
    if not root.is_dir():
        return set()

    out: set[str] = set()
    seen_files = 0
    for path in root.rglob("*"):
        if seen_files >= _MAX_ARTIFACT_WALK_FILES:
            break
        if not path.is_file():
            continue
        seen_files += 1
        name = path.name
        lower = name.lower()
        if lower not in _SKIP_BASENAME_REJECT and (
            is_filename_like_flag_token(name) or _looks_secret_token(name)
        ):
            out.add(name)

        if lower not in _ARTIFACT_TEXT_NAMES and not lower.startswith("dockerfile"):
            continue
        try:
            if path.stat().st_size > _MAX_ARTIFACT_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _ENV_FLAG_ASSIGN.finditer(text):
            val = (m.group(1) or "").strip().strip("\"'")
            if not val:
                continue
            # Keep PREFIX{...} ENV values out — those may be intentional local flags.
            # Reject filename-like / formatless tokens commonly mistaken for flags.
            if "{" in val or "}" in val:
                continue
            if is_filename_like_flag_token(val) or _looks_secret_token(val) or is_decoy_flag(val):
                out.add(val)
    return out


def env_auto_confirm_flags() -> bool:
    """True when CTF_AUTO_CONFIRM_FLAGS is set (tests / unattended runs)."""
    return os.environ.get("CTF_AUTO_CONFIRM_FLAGS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )


# While True, solver live-logs / INFO should stay quiet so the prompt is visible.
_confirm_active = False
_confirm_filter_installed = False
_confirm_cancel = threading.Event()


class _QuietDuringConfirmFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _confirm_active


def confirm_in_progress() -> bool:
    """True while the operator is being asked to confirm a flag.

    Under ``ARTEMIS_FLAG_CONFIRM`` the TUI owns the dialog bar — do not mute
    sibling agent live-logs (soft races keep streaming while one agent waits).
    Stdin / CLI confirms still mute so the y/n banner stays readable.
    """
    if os.environ.get("ARTEMIS_FLAG_CONFIRM", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    ):
        return False
    return _confirm_active


def cancel_flag_confirmation() -> None:
    """Abort any in-flight operator confirm (infra recover / swarm cancel)."""
    _confirm_cancel.set()


def reset_flag_confirmation_cancel() -> None:
    """Clear the confirm-cancel latch before a new prompt."""
    _confirm_cancel.clear()


def _install_confirm_log_filter() -> None:
    global _confirm_filter_installed
    if _confirm_filter_installed:
        return
    filt = _QuietDuringConfirmFilter()
    root = logging.getLogger()
    if root.handlers:
        for h in root.handlers:
            h.addFilter(filt)
    else:
        root.addFilter(filt)
    # Also quiet common noisy loggers used by solvers.
    for name in ("backend", "backend.agents", "httpx", "httpcore"):
        logging.getLogger(name).addFilter(filt)
    _confirm_filter_installed = True


def _emit_line(msg: str) -> None:
    """Print once to stdout.

    Swarm bridge merges stderr into stdout; printing both streams duplicates
    every confirm / outcome line in the TUI live log.
    """
    try:
        print(msg, end="" if msg.endswith("\n") else "\n", flush=True)
    except OSError:
        pass


def _emit_confirm_banner(flag: str) -> None:
    """Print a hard-to-miss banner (stdout only — see ``_emit_line``)."""
    bar = "=" * 72
    if os.environ.get("ARTEMIS_FLAG_CONFIRM", "").strip() in ("1", "true", "yes"):
        body = (
            f"\n\n{bar}\n  FLAG CANDIDATE — confirm in the TUI dialog (y/n)\n{bar}\n  {flag}\n{bar}\n"
        )
    else:
        body = (
            f"\n\n{bar}\n  FLAG CANDIDATE — type y or n (Enter alone ignored)\n{bar}\n  {flag}\n{bar}\n"
        )
    _emit_line(body)


def _flush_stdin_buffer() -> None:
    """Drop already-buffered keystrokes (e.g. stray Enter while logs scroll)."""
    if not sys.stdin.isatty():
        return
    try:
        import termios

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _stdin_line_or_cancel(prompt: str, *, poll_s: float = 0.4) -> str | None:
    """Read a stdin line, or return None if confirm was cancelled.

    Uses short select polls so infra_recover / swarm kill can abort a stuck prompt
    without waiting forever for the operator.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf = ""
    while not _confirm_cancel.is_set():
        try:
            ready, _, _ = select.select([sys.stdin], [], [], poll_s)
        except (ValueError, OSError):
            # Non-selectable stdin — fall back to blocking input.
            try:
                return input().strip().lower()
            except EOFError:
                return ""
        if not ready:
            continue
        chunk = sys.stdin.readline()
        if chunk == "":
            return ""  # EOF
        buf += chunk
        if "\n" in chunk:
            return buf.strip().lower()
    return None


def normalize_confirm(value: object) -> tuple[bool, str]:
    """Coerce a confirm result to ``(ok, reason)``.

    Confirm callables predate the reason channel and many still return a bare
    bool (stdin path, ``--auto-confirm-flags``, test doubles), so both shapes
    have to keep working.
    """
    if isinstance(value, tuple):
        ok = bool(value[0]) if value else False
        reason = str(value[1]).strip() if len(value) > 1 and value[1] else ""
        return ok, reason
    return bool(value), ""


def prompt_flag_confirmation(flag: str, *, auto_confirm: bool = False) -> bool | tuple[bool, str]:
    """Ask the operator whether ``flag`` is correct.

    Returns True (or ``(True, "")``) only on explicit yes, or when auto-confirm
    is enabled. The TUI/daemon path returns ``(ok, reason)`` so a rejection can
    carry the operator's explanation; pass the result through
    :func:`normalize_confirm`.
    Empty Enter does **not** count as no (re-prompts) — a buffered newline
    from scrolling logs must not auto-reject. Non-TTY / EOF → False.
    Cancel via ``cancel_flag_confirmation()`` → False.

    When ``ARTEMIS_FLAG_CONFIRM=1`` (TUI swarm), ask via file handshake so the
    TUI can show a dialog — not stdin y/N (TUI owns the keyboard).
    """
    global _confirm_active

    f = (flag or "").strip()
    reset_flag_confirmation_cancel()
    try:
        from backend.agents.live_log import flush_stream

        flush_stream()
    except Exception:
        pass
    from backend.daemon.transport import daemon_configured_in_env

    _install_confirm_log_filter()
    _confirm_active = True
    try:
        # TUI owns the keyboard — never auto-confirm away the dialog unless
        # the caller explicitly passed auto_confirm=True on this call.
        tui_confirm = os.environ.get("ARTEMIS_FLAG_CONFIRM", "").strip() in ("1", "true", "yes")
        _emit_confirm_banner(f)
        if auto_confirm or (env_auto_confirm_flags() and not tui_confirm):
            msg = "Auto-confirm enabled — accepting candidate.\n"
            _emit_line(msg)
            return True
        if tui_confirm:
            # Prefer daemon socket (single dialog channel). File handshake is
            # legacy fallback only when ARTEMIS_DAEMON_SOCK is unset.
            if daemon_configured_in_env():
                return _prompt_flag_confirmation_daemon(f)
            return _prompt_flag_confirmation_tui(f)
        if not sys.stdin.isatty():
            msg = (
                "No TTY for confirmation — rejecting candidate "
                "(set CTF_AUTO_CONFIRM_FLAGS=1 or pass --auto-confirm-flags).\n"
            )
            _emit_line(msg)
            return False
        _flush_stdin_buffer()
        while True:
            if _confirm_cancel.is_set():
                msg = ">>> Confirm cancelled (session recovering) — not counting.\n"
                _emit_line(msg)
                return False
            try:
                ans = _stdin_line_or_cancel(">>> Confirm this flag as correct? [y/n]: ")
            except EOFError:
                return False
            if ans is None:
                msg = ">>> Confirm cancelled (session recovering) — not counting.\n"
                _emit_line(msg)
                return False
            if ans in ("y", "yes"):
                ok = True
                break
            if ans in ("n", "no"):
                ok = False
                break
            retry = ">>> Type y or n (empty Enter ignored).\n"
            _emit_line(retry)
        _emit_confirm_verdict(ok)
        return ok
    finally:
        _confirm_active = False


def _emit_confirm_verdict(ok: bool) -> None:
    """Accept is announced here. Reject is announced once by ``do_submit_flag``."""
    if ok:
        _emit_line(">>> Confirmed — counting this flag.\n")


def _artemis_cache_dir() -> Path:
    from backend.cache import cache_dir

    return cache_dir()


def _prompt_flag_confirmation_tui(flag: str) -> bool:
    """Block until TUI writes an answer file for this confirm request."""
    import time
    import uuid

    req_id = uuid.uuid4().hex[:12]
    cache = _artemis_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    pending = cache / f"flag-confirm-{req_id}.pending.json"
    answer = cache / f"flag-confirm-{req_id}.answer.json"
    try:
        if answer.exists():
            answer.unlink()
    except OSError:
        pass
    pending.write_text(
        json.dumps({"id": req_id, "flag": flag, "created": time.time()}),
        encoding="utf-8",
    )
    line = f"[artemis] FLAG_CONFIRM id={req_id} flag={flag}\n"
    _emit_line(line)
    # Wait up to 30 minutes for TUI reply
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        if _confirm_cancel.is_set():
            msg = ">>> Confirm cancelled (session recovering) — not counting.\n"
            _emit_line(msg)
            return False
        if answer.is_file():
            try:
                data = json.loads(answer.read_text(encoding="utf-8"))
                ok = bool(data.get("ok"))
            except Exception:
                ok = False
            try:
                answer.unlink(missing_ok=True)
                pending.unlink(missing_ok=True)
            except OSError:
                pass
            _emit_confirm_verdict(ok)
            return ok
        time.sleep(0.25)

    msg = ">>> Confirm timed out — not counting.\n"
    _emit_line(msg)
    try:
        pending.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _prompt_flag_confirmation_daemon(flag: str) -> bool | tuple[bool, str]:
    """Ask the TUI (via the daemon socket) to confirm a flag.

    Returns ``(ok, reason)`` when the daemon answers, where ``reason`` is the
    operator's optional one-line explanation for a rejection. Fallback paths
    return a bare bool; use :func:`normalize_confirm` on the result.

    The solver runs in the detached swarm subprocess; this opens a synchronous
    Unix socket to the daemon, sends a ``flag_confirm_request``, and blocks
    until the daemon relays the TUI's answer. Cancel-aware via
    ``_confirm_cancel`` (checked in the select poll loop) so ``swarm.kill()``
    and infra-recovery can abort a stuck confirm.
    """
    import json as _json
    import select
    import time
    import uuid

    from backend.daemon.auth import with_hello_token
    from backend.daemon.transport import daemon_configured_in_env, sync_connect

    if not daemon_configured_in_env():
        return _prompt_flag_confirmation_tui(flag)

    session = os.environ.get("ARTEMIS_SESSION_ID")
    req_id = uuid.uuid4().hex[:12]
    deadline = time.monotonic() + 1800  # 30 min, same as file handshake

    line = f"[artemis] FLAG_CONFIRM id={req_id} flag={flag}\n"
    _emit_line(line)

    try:
        s = sync_connect(timeout=0.25)
    except OSError:
        # Daemon unreachable — fall back to file handshake so the solve isn't lost.
        return _prompt_flag_confirmation_tui(flag)

    try:
        # hello
        s.sendall(
            _json.dumps(
                with_hello_token(
                    {"v": 1, "id": None, "type": "hello", "role": "swarm", "session": session}
                )
            ).encode()
            + b"\n"
        )
        # request
        s.sendall(
            _json.dumps(
                {
                    "v": 1,
                    "id": req_id,
                    "type": "flag_confirm_request",
                    "request_id": req_id,
                    "flag": flag,
                    "session": session,
                }
            ).encode()
            + b"\n"
        )
        buf = b""
        while time.monotonic() < deadline:
            if _confirm_cancel.is_set():
                _emit_line(">>> Confirm cancelled (session recovering) — not counting.\n")
                return False
            r, _, _ = select.select([s], [], [], 0.25)
            if not r:
                continue
            try:
                chunk = s.recv(4096)
            except (TimeoutError, OSError):
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    msg = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id and msg.get("type") in (
                    "flag_confirm_request",
                    "error",
                ):
                    ok = bool(msg.get("ok"))
                    reason = str(msg.get("reason") or "").strip()
                    _emit_confirm_verdict(ok)
                    return ok, reason
        _emit_line(">>> Confirm timed out — not counting.\n")
        return False, ""
    finally:
        try:
            s.close()
        except OSError:
            pass


def accept_flag(
    flag: str,
    *,
    already_accepted: Sequence[str] = (),
    required: int = 1,
    challenge_dir: str | Path | None = None,
    artifact_flags: Sequence[str] | None = None,
    human_confirmed: bool = False,
    by: str = "",
) -> tuple[str, bool]:
    """Validate a submission; count it only when ``human_confirmed`` is True.

    Returns (display_message, challenge_complete).
    Unconfirmed valid submissions return ``CANDIDATE`` and False.
    ``challenge_complete`` is True only when ``required`` distinct flags are in.
    ``by`` names the submitting runner so a swarm accept says who earned it.
    """
    f = (flag or "").strip()
    req = normalize_flags_required(required)
    prior = [a.strip() for a in already_accepted if a and a.strip()]
    artifacts = set(artifact_flags or ())
    if challenge_dir is not None:
        artifacts |= collect_artifact_flag_candidates(challenge_dir)

    if not f:
        return "Empty flag — nothing to submit.", False
    if is_decoy_flag(f):
        return (
            f'REJECTED decoy/placeholder "{f}". Recover the real flag from challenge logic.',
            False,
        )
    # Wrap of a decoy body: PREFIX{fake_flag} / PREFIX{placeholder}
    body = flag_core(f)
    if body and body != f and is_decoy_flag(body):
        return (
            f'REJECTED decoy/placeholder wrap "{f}". Recover the real flag from challenge logic.',
            False,
        )
    if is_filename_like_flag_token(f) or f in artifacts:
        return (
            f'REJECTED "{f}" — looks like a packaging artifact (Dockerfile ENV / '
            "filename), not the awarded flag. Recover the real flag from challenge logic.",
            False,
        )
    # PREFIX{artifact_basename} / wrap of collected packaging tokens
    if body and body != f and (is_filename_like_flag_token(body) or body in artifacts):
        return (
            f'REJECTED "{f}" — wraps a packaging artifact, not the awarded flag. '
            "Recover the real flag from challenge logic.",
            False,
        )

    # Already complete — do not accept additional distinct flags.
    if len(prior) >= req:
        joined = " | ".join(prior)
        return (
            f"ALREADY SOLVED — all {req} flag(s) already accepted: {joined}",
            True,
        )

    if f in prior:
        n = len(prior)
        return (
            f"Already accepted this flag ({n}/{req}). "
            "Continue and submit the remaining distinct flag(s).",
            False,
        )

    if not human_confirmed:
        return (
            f'CANDIDATE "{f}" — awaiting human confirmation. '
            "Do not assume this is correct; keep solving until CORRECT.",
            False,
        )

    from backend.agents.live_log import emit_line

    accepted = [*prior, f]
    n = len(accepted)
    author = f" via {by}" if by else ""
    if n < req:
        msg = (
            f'ACCEPTED "{f}"{author} ({n}/{req}). '
            "Continue — submit the remaining distinct flag(s). "
            "Do not stop until all required flags are accepted."
        )
        emit_line(f"[artemis] outcome {msg}")
        return msg, False

    joined = " | ".join(accepted)
    if req == 1:
        msg = f'CORRECT — accepted "{f}"{author}. Challenge complete for this run.'
    else:
        msg = (
            f"CORRECT — accepted all {req} flags: {joined}"
            f"{f' (last{author})' if by else ''}. Challenge complete for this run."
        )
    emit_line(f"[artemis] outcome {msg}")
    return msg, True
