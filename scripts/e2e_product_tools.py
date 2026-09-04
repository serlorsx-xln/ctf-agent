#!/usr/bin/env python3
"""Exercise every product tool against L0. File-only retired challenge; no live lab."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

CHAL = Path("/tmp/artemis-e2e-pico-garden")
OUT = Path("/tmp/artemis-e2e/tools.json")


def rec(name: str, ok: bool, detail: str) -> dict:
    print(f"{'OK' if ok else 'FAIL':4} {name}: {detail[:160]}", flush=True)
    return {"name": name, "ok": ok, "detail": detail[:400]}


async def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    from backend.anti_hole import HoleDetector, apply_hole_guard
    from backend.challenge import guess_connection, list_attachment_names, load_challenge
    from backend.config import Settings
    from backend.flags import accept_flag
    from backend.message_bus import ChallengeMessageBus
    from backend.prompts import build_prompt
    from backend.sandbox.container import DockerSandbox, allowed_sandbox_write_path
    from backend.sandbox.setup_ready import probe_setup_status
    from backend.tool_router import detect_packs
    from backend.tools.core import (
        do_bash,
        do_check_findings,
        do_claim_soft_queue,
        do_list_files,
        do_read_file,
        do_submit_flag,
        do_view_image,
        do_web_fetch,
        do_webhook_create,
        do_webhook_get_requests,
        do_write_file,
        view_image_arg,
    )

    st = probe_setup_status()
    rows.append(rec("setup_gate", st.ready and st.docker_ok and st.core_image, st.message))

    meta = load_challenge(CHAL)
    names = list_attachment_names(CHAL)
    packs = detect_packs(CHAL)
    prompt = build_prompt(meta, names, "arm64", True)
    rows.append(
        rec(
            "load_challenge",
            meta.name == "artemis-e2e-pico-garden" and "garden.jpg" in names,
            f"conn={meta.connection_info!r} packs={packs}",
        )
    )
    rows.append(rec("guess_no_endpoint", guess_connection(meta.description) == "", "empty"))
    rows.append(rec("prefetch_no_steg_from_jpg", "steg" not in packs, str(packs)))
    rows.append(rec("prompt_flag_only", "Flag-only" in prompt and "UNTRUSTED" in prompt, "fenced"))

    decoy, _ = accept_flag("CTF{flag}")
    rows.append(rec("flag_decoy", decoy.startswith("REJECTED"), decoy.split("\n")[0]))

    try:
        allowed_sandbox_write_path("/etc/passwd")
        rows.append(rec("write_allowlist", False, "allowed /etc"))
    except PermissionError:
        rows.append(rec("write_allowlist", True, "denied /etc"))

    bus = ChallengeMessageBus()
    solver = SimpleNamespace(
        hole_detector=HoleDetector(off_target_break=1),
        message_bus=bus,
        notify_coordinator=None,
        runner_id="tools-probe",
        tracer=None,
    )
    hole = await apply_hole_guard(
        solver, "bash", {"command": "curl https://ctftime.org/writeup/1"}, "ok"
    )
    unread = await bus.check("sib")
    rows.append(rec("anti_hole", "DEAD-END" in str(hole) and bool(unread), str(hole)[:80]))

    import backend.pack_preflight as pack_preflight

    pack_preflight.resolve_prefetch_packs = lambda settings, challenge_dir: []
    sb = DockerSandbox(
        image="ctf-sandbox-core",
        challenge_dir=str(CHAL),
        settings=Settings(),
        session_id="e2e-tools",
    )
    try:
        await sb.start()
        listed = await do_list_files(sb)
        rows.append(rec("list_files", "garden.jpg" in listed, listed.split("\n")[0][:80]))

        tools = await do_read_file(sb, "/challenge/TOOLS.txt")
        rows.append(rec("read_file", "CTF SANDBOX TOOLS" in tools, f"len={len(tools)}"))

        wrote = await do_write_file(sb, "probe.txt", "ok\n")
        denied = await do_write_file(sb, "/etc/hosts", "x")
        rows.append(rec("write_file_ok", "error" not in wrote.lower(), wrote[:80]))
        rows.append(
            rec(
                "write_file_deny",
                "confined" in denied.lower() or "error" in denied.lower(),
                denied[:80],
            )
        )

        bash = await do_bash(sb, "ls /challenge/distfiles && echo L0_OK")
        rows.append(rec("bash", "garden.jpg" in bash and "L0_OK" in bash, bash[:80]))

        img = await do_view_image(sb, "garden.jpg", use_vision=False)
        img_ok = isinstance(img, tuple) and img[1].startswith("image/") and len(img[0]) > 1000
        rows.append(
            rec(
                "view_image",
                img_ok,
                f"{img[1]} {len(img[0])}B" if isinstance(img, tuple) else str(img)[:120],
            )
        )
        img_path = await do_view_image(
            sb, view_image_arg({"path": "/challenge/distfiles/garden.jpg"}), use_vision=False
        )
        path_ok = (
            isinstance(img_path, tuple)
            and img_path[1].startswith("image/")
            and len(img_path[0]) > 1000
        )
        rows.append(
            rec(
                "view_image_path",
                path_ok,
                f"{img_path[1]} {len(img_path[0])}B"
                if isinstance(img_path, tuple)
                else str(img_path)[:120],
            )
        )

        strings_out = await do_bash(
            sb,
            "strings /challenge/distfiles/garden.jpg | rg -o 'picoCTF\\{[^}]+\\}' | head -1",
        )
        flag = strings_out.strip().splitlines()[-1] if strings_out.strip() else ""
        rows.append(rec("bash_strings_flag", flag.startswith("picoCTF{"), flag[:80]))

        if flag.startswith("picoCTF{"):
            msg, done = await do_submit_flag(
                meta.name, flag, auto_confirm=True, challenge_dir=str(CHAL)
            )
            rows.append(rec("submit_flag_real", "CORRECT" in msg and done, msg[:120]))
        else:
            rows.append(rec("submit_flag_real", False, "no flag from strings"))

        msg_d, done_d = await do_submit_flag(meta.name, "CTF{flag}", auto_confirm=True)
        rows.append(
            rec("submit_flag_decoy", msg_d.startswith("REJECTED") and not done_d, msg_d[:80])
        )

        fetch = await do_web_fetch("https://example.com/")
        rows.append(
            rec("web_fetch_public", "Example Domain" in fetch or "HTTP 200" in fetch, fetch[:80])
        )
        blocked = await do_web_fetch("http://10.0.0.1/")
        rows.append(rec("web_fetch_rfc1918", "blocked" in blocked.lower(), blocked[:80]))

        created = await do_webhook_create()
        uuid = ""
        try:
            data = json.loads(created)
            uuid = str(data.get("uuid") or "")
        except json.JSONDecodeError:
            pass
        rows.append(rec("webhook_create", bool(uuid), created[:120]))
        if uuid:
            pending = await do_webhook_get_requests(uuid)
            rows.append(
                rec(
                    "webhook_get_requests",
                    "No requests" in pending
                    or pending.startswith("[")
                    or "data" in pending.lower(),
                    pending[:80],
                )
            )
        else:
            rows.append(rec("webhook_get_requests", False, created[:80]))

        await bus.post("sib", "note from sibling")
        findings = await do_check_findings(bus, "tools-probe", runner_id="tools-probe")
        rows.append(
            rec(
                "check_findings",
                "note from sibling" in findings or "No new" in findings,
                findings[:80],
            )
        )

        queued = await do_claim_soft_queue(bus, "tools-probe", runner_id="tools-probe")
        rows.append(rec("claim_soft_queue", isinstance(queued, list), str(queued)[:80]))

        try:
            await bus.post("tools-probe", "coordinator note")
            rows.append(rec("notify_bus", True, "posted"))
        except Exception as e:
            rows.append(rec("notify_bus", False, str(e)))
    finally:
        try:
            await sb.stop()
        except Exception:
            pass

    failed = [r["name"] for r in rows if not r["ok"]]
    OUT.write_text(json.dumps({"failed": failed, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\n{len(rows) - len(failed)}/{len(rows)} passed  failed={failed}  {OUT}", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
