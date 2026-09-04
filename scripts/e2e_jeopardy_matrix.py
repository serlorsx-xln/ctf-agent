#!/usr/bin/env python3
"""Run owned Jeopardy fixtures through CLI swarm. CORRECT + exit 0 per category.

Does not touch live contests or labs we do not own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.jeopardy_fixtures import SPECS, build_all, fixture_dir, load_expected  # noqa: E402

OUT_DIR = Path("/tmp/artemis-e2e/jeopardy-matrix")
DEFAULT_MODEL = "cursor/composer-2.5"


def _run_one(slug: str, *, model: str, wall_s: float) -> dict:
    chal = fixture_dir(slug)
    eval_out = OUT_DIR / f"{slug}.json"
    log_path = OUT_DIR / f"{slug}.log"
    cmd = [
        "uv",
        "run",
        "artemis",
        "swarm",
        "--challenge",
        str(chal),
        "--models",
        model,
        "--auto-confirm-flags",
        "--eval-max-wall-s",
        str(wall_s),
        "--eval-out",
        str(eval_out),
    ]
    print(f"\n=== {slug} wall≤{wall_s}s ===", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    summary: dict = {}
    if eval_out.is_file():
        try:
            summary = json.loads(eval_out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = {}
    expected = load_expected()[slug]
    want = expected["flag"]
    got = summary.get("flag") or ""
    ok = (
        proc.returncode == 0
        and summary.get("status") == "flag_found"
        and got == want
    )
    rec = {
        "slug": slug,
        "ok": ok,
        "exit": proc.returncode,
        "status": summary.get("status"),
        "flag": got,
        "expected": want,
        "steps": summary.get("steps"),
        "eval": str(eval_out),
        "log": str(log_path),
    }
    print(
        f"{'OK' if ok else 'FAIL':4} {slug}: status={rec['status']} "
        f"exit={rec['exit']} flag={got!r}",
        flush=True,
    )
    return rec


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--skip-build", action="store_true")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not args.skip_build:
        build_all(require_elfs=True)

    slugs = [s.slug for s in SPECS]
    if args.only:
        slugs = [s for s in slugs if s in set(args.only)]

    rows = []
    for spec in SPECS:
        if spec.slug not in slugs:
            continue
        dest = fixture_dir(spec.slug)
        if spec.needs_elf:
            binary = dest / "distfiles" / spec.needs_elf
            if not binary.is_file():
                rows.append(
                    {
                        "slug": spec.slug,
                        "ok": False,
                        "exit": -1,
                        "status": "missing_elf",
                        "flag": "",
                        "expected": load_expected()[spec.slug]["flag"],
                    }
                )
                print(f"FAIL {spec.slug}: missing {binary}", flush=True)
                continue
        rows.append(_run_one(spec.slug, model=args.model, wall_s=spec.wall_s))

    out = OUT_DIR / "matrix.json"
    failed = [r["slug"] for r in rows if not r["ok"]]
    payload = {"ok": not failed, "failed": failed, "rows": rows}
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(rows) - len(failed)}/{len(rows)} passed  failed={failed}  {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
