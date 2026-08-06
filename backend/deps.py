"""Shared dependency types — avoids circular imports between agents and tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from backend.cost_tracker import CostTracker


@dataclass
class CoordinatorDeps:
    cost_tracker: CostTracker
    settings: Any
    model_specs: list[str] = field(default_factory=list)
    challenges_root: str = "challenges"
    max_concurrent_challenges: int = 10

    msg_port: int = 0  # 0 = auto-pick free port

    # Runtime state
    coordinator_inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    operator_inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    swarms: dict[str, Any] = field(default_factory=dict)
    swarm_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)
    challenge_dirs: dict[str, str] = field(default_factory=dict)
    challenge_metas: dict[str, Any] = field(default_factory=dict)
