#!/usr/bin/env python3
"""Report / evict host pack cache under CTF_PACK_CACHE_MAX_GB (default 25)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.tool_router import (  # noqa: E402
    evict_pack_cache,
    pack_cache_entries,
    pack_cache_max_bytes,
    pack_cache_root,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting",
    )
    p.add_argument(
        "--protect",
        action="append",
        default=[],
        help="Pack id to keep (repeatable)",
    )
    args = p.parse_args()

    root = pack_cache_root()
    max_b = pack_cache_max_bytes()
    entries = pack_cache_entries()
    total = sum(size for _, size, _, _ in entries)
    print(f"cache root: {root}")
    print(f"max bytes:  {max_b or 'unlimited (CTF_PACK_CACHE_MAX_GB=0)'}")
    print(f"total:      {total} ({total / (1024**3):.2f} GiB)")
    for mtime, size, pack_id, path in sorted(entries, key=lambda e: e[0]):
        print(
            f"  {pack_id}/{path.name}: {size / (1024**3):.2f} GiB  "
            f"mtime={mtime:.0f}"
        )

    if args.dry_run:
        if max_b <= 0 or total <= max_b:
            print("dry-run: nothing to evict")
            return 0
        # Simulate without deleting
        protected = set(args.protect)
        remaining = total
        would = []
        for mtime, size, pack_id, path in sorted(entries, key=lambda e: e[0]):
            if remaining <= max_b:
                break
            if pack_id in protected:
                continue
            would.append(f"{pack_id}/{path.name}")
            remaining -= size
        print("dry-run would remove:", ", ".join(would) or "(none)")
        return 0

    removed = evict_pack_cache(protect=args.protect)
    print("removed:", ", ".join(removed) or "(none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
