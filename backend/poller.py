"""Local challenge directory poller — detects new challenges under challenges_root."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PollEvent:
    kind: str  # "new_challenge" | "challenge_solved"
    challenge_name: str
    details: dict = field(default_factory=dict)


@dataclass
class LocalChallengePoller:
    """Watch a local challenges directory; track solves via callback."""

    challenges_root: str
    solved_fn: Callable[[], set[str]]
    interval_s: float = 5.0

    _known_challenges: set[str] = field(default_factory=set)
    _known_solved: set[str] = field(default_factory=set)
    _event_queue: asyncio.Queue[PollEvent] = field(default_factory=asyncio.Queue)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def _scan_names(self) -> set[str]:
        root = Path(self.challenges_root)
        if not root.is_dir():
            return set()
        names: set[str] = set()
        from backend.challenge import is_challenge_dir, load_challenge

        for d in root.iterdir():
            if not d.is_dir() or not is_challenge_dir(d):
                continue
            try:
                names.add(load_challenge(d).name)
            except Exception:
                names.add(d.name)
        return names

    async def start(self) -> None:
        await self._seed()
        logger.info(
            "Local poller initialized: %d challenges, %d solved",
            len(self._known_challenges),
            len(self._known_solved),
        )
        self._task = asyncio.create_task(self._loop(), name="local-challenge-poller")

    async def _seed(self) -> None:
        self._known_challenges = self._scan_names()
        try:
            self._known_solved = set(self.solved_fn())
        except Exception as e:
            logger.warning("Initial solved scan error: %s", e)
            self._known_solved = set()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_event(self, timeout: float = 1.0) -> PollEvent | None:
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            return None

    def drain_events(self) -> list[PollEvent]:
        events: list[PollEvent] = []
        while not self._event_queue.empty():
            try:
                events.append(self._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    @property
    def known_challenges(self) -> set[str]:
        return set(self._known_challenges)

    @property
    def known_solved(self) -> set[str]:
        return set(self._known_solved)

    async def _poll_once(self) -> None:
        try:
            current_names = self._scan_names()
            current_solved = set(self.solved_fn())

            for name in current_names - self._known_challenges:
                logger.info("New local challenge detected: %s", name)
                self._event_queue.put_nowait(PollEvent("new_challenge", name))

            for name in current_solved - self._known_solved:
                logger.info("Challenge solved: %s", name)
                self._event_queue.put_nowait(PollEvent("challenge_solved", name))

            self._known_challenges = current_names
            self._known_solved = current_solved
        except Exception as e:
            logger.warning("Local poll error: %s", e)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.interval_s)
            await self._poll_once()
