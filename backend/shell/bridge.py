"""JSON stdin bridge for chassis CTF tools → Docker sandbox / submit_flag / swarm / session."""

from __future__ import annotations

import asyncio
import json
import os
import sys


def _sid(payload: dict) -> str:
    """Session id from payload (daemon), else ``ARTEMIS_SESSION_ID``, else ``_default``."""
    from backend.shell.sandbox_session import resolve_session_id

    return resolve_session_id(payload.get("session") or payload.get("session_id"))


def _challenge_dir(payload: dict) -> str:
    """Resolve challenge dir from payload, else persisted session state."""
    from backend.shell.sandbox_session import load_session_state

    sid = _sid(payload)
    return (
        payload.get("challenge_dir") or load_session_state(sid).get("challenge_dir") or ""
    ).strip()


async def _bash(payload: dict) -> str:
    from backend.shell.sandbox_session import get_sandbox, save_session_state
    from backend.tools.core import do_bash

    sid = _sid(payload)
    challenge_dir = _challenge_dir(payload)
    command = payload.get("command") or ""
    if not command:
        return "ERROR: empty command"
    if not challenge_dir:
        return (
            "ERROR: no challenge loaded. Call artemis_load_challenge with a path, then retry."
        )

    save_session_state(sid, challenge_dir=challenge_dir)
    sandbox = await get_sandbox(challenge_dir, session_id=sid)
    return await do_bash(sandbox, command)


async def _read_file(payload: dict) -> str:
    from backend.shell.sandbox_session import get_sandbox
    from backend.tools.core import do_read_file

    sid = _sid(payload)
    challenge_dir = _challenge_dir(payload)
    path = payload.get("path") or ""
    if not challenge_dir:
        return "ERROR: no challenge loaded"
    if not path:
        return "ERROR: empty path"
    sandbox = await get_sandbox(challenge_dir, session_id=sid)
    return await do_read_file(sandbox, path)


async def _write_file(payload: dict) -> str:
    from backend.shell.sandbox_session import get_sandbox
    from backend.tools.core import do_write_file

    sid = _sid(payload)
    challenge_dir = _challenge_dir(payload)
    path = payload.get("path") or ""
    content = payload.get("content")
    if content is None:
        content = ""
    if not challenge_dir:
        return "ERROR: no challenge loaded"
    if not path:
        return "ERROR: empty path"
    sandbox = await get_sandbox(challenge_dir, session_id=sid)
    return await do_write_file(sandbox, path, str(content))


async def _list_files(payload: dict) -> str:
    from backend.shell.sandbox_session import get_sandbox
    from backend.tools.core import do_list_files

    sid = _sid(payload)
    challenge_dir = _challenge_dir(payload)
    path = payload.get("path") or "/challenge/distfiles"
    if not challenge_dir:
        return "ERROR: no challenge loaded"
    sandbox = await get_sandbox(challenge_dir, session_id=sid)
    return await do_list_files(sandbox, path)


async def _submit_flag(payload: dict) -> str:
    """Soft submit: returns ACCEPTED/CORRECT text but does not kill the TUI session."""
    from backend.challenge import load_challenge
    from backend.shell.sandbox_session import load_session_state, save_session_state
    from backend.tools.core import do_submit_flag

    sid = _sid(payload)
    flag = (payload.get("flag") or "").strip()
    st = load_session_state(sid)
    challenge_dir = (payload.get("challenge_dir") or st.get("challenge_dir") or "").strip()
    if not flag:
        return "ERROR: empty flag"

    name = "challenge"
    required = int(st.get("flags_required") or 1)
    accepted = list(st.get("accepted_flags") or [])
    if challenge_dir:
        try:
            meta = load_challenge(challenge_dir)
            name = meta.name
            if not st.get("flags_required"):
                required = meta.flags_required
        except Exception:
            pass

    soft = payload.get("soft", True)
    display, done = await do_submit_flag(
        name,
        flag,
        already_accepted=accepted,
        required=required,
        challenge_dir=challenge_dir or None,
    )
    if display.startswith(("ACCEPTED", "CORRECT")) and flag not in accepted:
        accepted.append(flag)
        save_session_state(sid, accepted_flags=accepted, flags_required=required)
    # Soft submit keeps the TUI session open after ACCEPTED/CORRECT.
    # Swarm hard-stop is ChallengeSwarm via artemis_swarm — not this path.
    if soft and done:
        return (
            f"{display}\n"
            f"[Artemis] Progress {len(accepted)}/{required} — CORRECT. "
            "Session stays open — keep chatting or load another challenge."
        )
    if soft and display.startswith("ACCEPTED"):
        return (
            f"{display}\n"
            f"[Artemis] Progress {len(accepted)}/{required}. "
            "Session stays open — continue for remaining flags."
        )
    return display


async def _load_challenge(payload: dict) -> str:
    from backend.challenge import is_challenge_dir, load_challenge, resolve_load_target
    from backend.flags import normalize_flags_required
    from backend.shell.sandbox_session import (
        clear_flags_required,
        load_session_state,
        save_session_state,
        stop_sandbox,
    )

    sid = _sid(payload)
    text = (payload.get("prompt") or payload.get("description") or "").strip()
    path = (payload.get("path") or "").strip() or None
    attachments = payload.get("attachments") or payload.get("files") or []
    if isinstance(attachments, str):
        attachments = [attachments]

    if not path and not text:
        return (
            "ERROR: path or prompt/description required "
            "(paste challenge text / web links, and/or folder or file paths)"
        )

    try:
        root = resolve_load_target(
            path=path,
            prompt=text or None,
            name=payload.get("name"),
            attachments=list(attachments) if attachments else None,
        )
    except (FileNotFoundError, ValueError, OSError) as e:
        return f"ERROR: {e}"

    # Stop only this session's prior sandbox — never wipe other windows.
    prev = (load_session_state(sid).get("challenge_dir") or "").strip()
    if prev:
        await stop_sandbox(prev, session_id=sid)
    if is_challenge_dir(root):
        meta = load_challenge(root)
    else:
        from backend.prompts import ChallengeMeta

        meta = ChallengeMeta(name=root.name, description=f"Workspace `{root.name}`", flags_required=1)

    # flags_required is no longer parsed from the challenge text/description.
    # The operator is ALWAYS asked via the TUI digits dialog (propagated as
    # None through the daemon). An explicit payload value (rare) overrides.
    flags_explicit = payload.get("flags_required") is not None
    if flags_explicit:
        flags = normalize_flags_required(int(payload["flags_required"]))
        save_session_state(
            sid,
            challenge_dir=str(root),
            challenge_name=meta.name,
            flags_required=flags,
            flags_explicit=True,
            accepted_flags=[],
            mode=payload.get("mode") or "artemis",
        )
    else:
        save_session_state(
            sid,
            challenge_dir=str(root),
            challenge_name=meta.name,
            accepted_flags=[],
            mode=payload.get("mode") or "artemis",
        )
        # Erase any stale flags_required so the daemon asks the dialog.
        clear_flags_required(sid)
        flags = "unknown"

    how = "existing path" if path else "pasted prompt → cache workspace"
    next_step = "Next: artemis_ask_flags (TUI: flags → single/swarm → models → start)."
    return (
        f"Loaded challenge `{meta.name}` at {root}\n"
        f"source={how}\n"
        f"flags_required={flags}\n"
        f"flags_explicit={flags_explicit}\n"
        f"mode=artemis\n"
        f"{next_step} Host bash is disabled for CTF."
    )


async def _set_flags(payload: dict) -> str:
    from backend.flags import normalize_flags_required
    from backend.shell.sandbox_session import save_session_state

    sid = _sid(payload)
    n = normalize_flags_required(int(payload.get("flags_required") or 1))
    st = save_session_state(sid, flags_required=n, flags_explicit=True)
    return f"flags_required={n} (challenge={st.get('challenge_dir', '?')})"


async def _status(payload: dict) -> str:
    from backend.shell.sandbox_session import load_session_state

    return json.dumps(load_session_state(_sid(payload)), indent=2)


async def _clear_session(payload: dict) -> str:
    """Wipe challenge progress — used when TUI opens a new chat session."""
    from backend.shell.sandbox_session import clear_session_state, load_session_state

    sid = _sid(payload)
    clear_session_state(sid)
    return json.dumps(load_session_state(sid), indent=2)


async def _stop(payload: dict) -> str:
    from backend.shell.sandbox_session import stop_sandbox

    sid = _sid(payload)
    parts = [await stop_sandbox(payload.get("challenge_dir"), session_id=sid)]
    parts.append(await _stop_race(payload))
    return "\n".join(parts)


def _pid_command(pid: int) -> str:
    from backend.process_hygiene import pid_command

    return pid_command(pid)


def _windows_executable_looks_like_python(pid: int) -> bool:
    from backend.process_hygiene import windows_image_looks_like_python

    return windows_image_looks_like_python(pid)


def _is_artemis_race_pid(pid: int) -> bool:
    """True if pid exists and looks like our swarm (not an unrelated recycled pid)."""
    import sys

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    from backend.process_hygiene import is_artemis_swarm_command

    cmd = _pid_command(pid)
    if not cmd:
        # Windows: CommandLine is often empty — fall back to image name so
        # pidfiles for live python/uv swarms are not discarded (orphan risk).
        # Non-python recycled PIDs stay False (do not kill strangers).
        if sys.platform == "win32":
            return _windows_executable_looks_like_python(pid)
        # Unix: process exists but cmdline unreadable (permissions) — keep prior
        # conservative "treat as live" behaviour for adopt/stop.
        return True
    return is_artemis_swarm_command(cmd)

def _kill_pid_tree(pid: int) -> None:
    """Best-effort kill process and children (Unix + Windows)."""
    import signal
    import subprocess
    import sys

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        except OSError:
            pass
        return
    try:
        subprocess.run(
            ["pkill", "-TERM", "-P", str(pid)],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, sigkill)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _stop_race(_payload: dict) -> str:
    """Stop the active TUI swarm for one session (pidfile only).

    When ``session`` / ``session_id`` is set (or ``ARTEMIS_SESSION_ID``), only
    that session's pidfile is killed — no global ``pgrep`` sweep (multi-session
    safe). Pass ``all=true`` to also sweep stray ``artemis swarm`` processes.
    """
    import subprocess

    from backend.daemon.session_id import DEFAULT_SESSION_ID, normalize_session_id
    from backend.shell.sandbox_session import resolve_session_id, swarm_pid_path

    sid = normalize_session_id(
        _payload.get("session")
        or _payload.get("session_id")
        or resolve_session_id(None)
    )
    stop_all = bool(_payload.get("all"))

    pid_paths = [swarm_pid_path(sid)]
    if sid == DEFAULT_SESSION_ID:
        from backend.cache import cache_dir

        cache = cache_dir()
        pid_paths.extend([cache / "swarm.pid", cache / "race.pid"])

    killed: list[str] = []
    for pid_path in pid_paths:
        if not pid_path.is_file():
            continue
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            if _is_artemis_race_pid(old_pid):
                _kill_pid_tree(old_pid)
                killed.append(str(old_pid))
        except (ValueError, OSError):
            pass
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    # Global pgrep only when explicitly requested (legacy single-session stop-all).
    if stop_all:
        for pattern in ("artemis swarm --challenge", "artemis race --challenge"):
            try:
                out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
                for line in out.splitlines():
                    try:
                        pid = int(line.strip())
                    except ValueError:
                        continue
                    if str(pid) not in killed and _is_artemis_race_pid(pid):
                        _kill_pid_tree(pid)
                        killed.append(str(pid))
            except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                pass

    if killed:
        return f"Stopped swarm process(es): {', '.join(killed)}"
    return "No active swarm to stop"


async def _swarm(payload: dict) -> str:
    """Fire Python swarm (hard stop on CORRECT). One swarm at a time.

    When the daemon control plane is running, route through it: send
    ``swarm_start`` and stream ``swarm_log`` push events back to our own stdout
    (legacy bridge callers still stream stdout). Falls back to the direct
    detached-subprocess path when no daemon socket is present (headless CLI /
    tests).
    """
    if _daemon_available():
        return await _swarm_via_daemon(payload)
    return await _swarm_direct(payload)


def _daemon_available() -> bool:
    """True if a daemon is listening on the control-plane endpoint."""
    from backend.daemon.transport import daemon_alive

    return daemon_alive()


async def _swarm_via_daemon(payload: dict) -> str:
    """Stream a daemon-supervised swarm into this process's stdout."""
    import json as _json

    from backend.daemon.transport import open_connection
    from backend.shell.sandbox_session import resolve_session_id

    sid = resolve_session_id(payload.get("session") or payload.get("session_id"))
    reader, writer = await open_connection()
    try:
        writer.write(
            _json.dumps(
                {"v": 1, "id": None, "type": "hello", "role": "tui", "session": sid}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        # Read hello ack.
        await reader.readline()
        # Send swarm_start.
        req_id = "bridge-swarm"
        writer.write(
            _json.dumps(
                {"v": 1, "id": req_id, "type": "swarm_start", "session": sid, **dict(payload)}
            ).encode()
            + b"\n"
        )
        await writer.drain()

        lines = 0
        while True:
            raw = await reader.readline()
            if not raw:
                break
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            # Ignore pushes for other sessions (multi-window daemon).
            msg_sid = msg.get("session")
            if msg_sid is not None and str(msg_sid) != sid:
                continue
            mtype = msg.get("type")
            if mtype == "swarm_log":
                text = msg.get("text", "")
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
                lines += 1
            elif mtype == "boot":
                sys.stdout.write(f"[artemis] boot {msg.get('text', '')}\n")
                sys.stdout.flush()
            elif mtype == "swarm_exit":
                code = msg.get("code")
                return f"[swarm exit {code}] ({lines} lines streamed)"
            elif mtype == "swarm_start":
                # Spawn ack from daemon (type mirrors the request name).
                if msg.get("id") == req_id and not msg.get("ok"):
                    err = msg.get("error", "swarm_start failed")
                    return f"ERROR: {err}"
            elif mtype == "swarm_start.response":
                # Legacy alias — keep for older daemon builds.
                if not msg.get("ok"):
                    err = msg.get("error", "swarm_start failed")
                    return f"ERROR: {err}"
            # Ignore other push events (usage/session/dialog) in the bridge shim —
            # the TUI sidebar reads those directly from the daemon in Phase 5+.
        return f"[swarm stream ended] ({lines} lines streamed)"
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _swarm_direct(payload: dict) -> str:
    """Direct detached-subprocess swarm (headless / no daemon). Original path."""
    from pathlib import Path

    from backend.models import missing_swarm_credentials, normalize_swarm_specs
    from backend.shell.sandbox_session import (
        load_session_state,
        resolve_session_id,
        swarm_pid_path,
    )

    sid = resolve_session_id(payload.get("session") or payload.get("session_id"))
    st = load_session_state(sid)
    challenge = (payload.get("challenge_dir") or st.get("challenge_dir") or "").strip()
    if not challenge:
        return "ERROR: load a challenge first (paste challenge text or path into artemis_load_challenge)"
    models = payload.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.replace(",", " ").split() if m.strip()]
    try:
        models = normalize_swarm_specs(models) if models else []
    except ValueError as e:
        return f"ERROR: invalid model spec — {e}"
    if not models:
        from backend.models import missing_models_error

        return missing_models_error()
    missing = missing_swarm_credentials(models)
    if missing:
        from backend.models import missing_credentials_error

        return missing_credentials_error(missing)

    flags = payload.get("flags_required") or st.get("flags_required") or 1
    try:
        from backend.shell.sandbox_session import save_session_state
        from backend.shell.swarm_reset import reset_for_new_swarm

        reset_for_new_swarm(sid)
        if payload.get("flags_required") is not None:
            save_session_state(sid, flags_required=int(flags), flags_explicit=True)
    except Exception:
        pass
    pid_path = swarm_pid_path(sid)
    from backend.cache import cache_dir
    from backend.daemon.session_id import DEFAULT_SESSION_ID

    if sid == DEFAULT_SESSION_ID:
        legacy_pid = cache_dir() / "race.pid"
        legacy_swarm = cache_dir() / "swarm.pid"
        if not pid_path.is_file():
            for legacy in (legacy_swarm, legacy_pid):
                if legacy.is_file():
                    try:
                        pid_path.parent.mkdir(parents=True, exist_ok=True)
                        legacy.rename(pid_path)
                        break
                    except OSError:
                        try:
                            pid_path.parent.mkdir(parents=True, exist_ok=True)
                            pid_path.write_text(
                                legacy.read_text(encoding="utf-8"), encoding="utf-8"
                            )
                            legacy.unlink(missing_ok=True)
                            break
                        except OSError:
                            pass
    # One swarm at a time *per session*: stop any previous for this sid
    force = payload.get("force")
    if force is None:
        force = True
    if pid_path.is_file():
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            old_pid = 0
        if old_pid and _is_artemis_race_pid(old_pid):
            if force:
                sys.stdout.write(f"[artemis] stopping previous swarm pid={old_pid}\n")
                sys.stdout.flush()
                await _stop_race({"session": sid})
            else:
                return (
                    f"ERROR: a swarm is already running (pid {old_pid}). "
                    "Call artemis_stop_swarm, or pass force=true to replace it."
                )
        else:
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass

    repo = os.environ.get("ARTEMIS_REPO_ROOT") or str(
        Path(__file__).resolve().parents[2]
    )
    swarm_args = [
        "--challenge",
        challenge,
        "--flags-required",
        str(flags),
    ]
    for m in models:
        swarm_args.extend(["--models", m])
    if payload.get("auto_confirm"):
        swarm_args.append("--auto-confirm-flags")

    # TUI owns the keyboard — solvers ask via FLAG_CONFIRM file handshake + dialog.
    from backend.shell.sandbox_session import swarm_log_path_for_session
    from backend.subprocess_platform import (
        sanitize_child_env,
        swarm_command,
        swarm_subprocess_kwargs,
    )

    cmd = swarm_command(repo, swarm_args)
    env = sanitize_child_env()
    env.update(
        {
            "ARTEMIS_FLAG_CONFIRM": "1",
            "ARTEMIS_SESSION_ID": sid,
            "ARTEMIS_SWARM_LOG": str(swarm_log_path_for_session(challenge, sid)),
        }
    )
    sys.stdout.write(
        f"[artemis] boot Starting swarm · {Path(challenge).name} · flags={flags}\n"
        f"[artemis] boot models={', '.join(models)}\n"
    )
    sys.stdout.flush()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=repo,
        env=env,
        **swarm_subprocess_kwargs(),
    )
    chunks: list[str] = []
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            chunks.append(text)
            # Live stream to parent (plugin reads stdout incrementally)
            sys.stdout.write(text)
            sys.stdout.flush()
        await proc.wait()
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
    # Body already streamed; return a short footer so main() does not reprint the log
    return f"[swarm exit {proc.returncode}] ({len(chunks)} lines streamed)"


async def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python -m backend.shell.bridge "
            "<bash|read_file|write_file|list_files|submit_flag|load|flags|status|clear_session|swarm|stop|stop_swarm>",
            file=sys.stderr,
        )
        sys.exit(2)
    op = sys.argv[1]
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}

    ops = {
        "bash": _bash,
        "read_file": _read_file,
        "write_file": _write_file,
        "list_files": _list_files,
        "submit_flag": _submit_flag,
        "load": _load_challenge,
        "flags": _set_flags,
        "status": _status,
        "clear_session": _clear_session,
        "swarm": _swarm,
        "race": _swarm,  # legacy alias
        "stop": _stop,
        "stop_swarm": _stop_race,
        "stop_race": _stop_race,  # legacy alias
    }
    if op not in ops:
        print(f"Unknown op: {op}", file=sys.stderr)
        sys.exit(2)
    result = ops[op](payload)
    if asyncio.iscoroutine(result):
        result = await result
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
