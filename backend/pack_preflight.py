"""Resolve which tool packs to prefetch before the first agent turn."""

from __future__ import annotations

from typing import Any


def resolve_prefetch_packs(settings: Any, challenge_dir: str) -> list[str]:
    """Return ordered pack ids for sandbox preflight.

    Honors ``settings.force_packs`` when non-empty; otherwise uses
    ``settings.detected_packs`` if already populated by the router, else
    runs ``detect_packs(challenge_dir)``.
    """
    force = list(getattr(settings, "force_packs", None) or [])
    if force:
        # Preserve order, drop empties/dupes
        seen: set[str] = set()
        out: list[str] = []
        for p in force:
            p = (p or "").strip()
            if not p or p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out

    detected = list(getattr(settings, "detected_packs", None) or [])
    if detected:
        return list(detected)

    from backend.tool_router import detect_packs

    return detect_packs(challenge_dir)
