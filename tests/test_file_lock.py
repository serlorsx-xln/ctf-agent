"""Cross-platform file lock tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from backend.file_lock import acquire, release


def test_file_lock_serializes_threads(tmp_path: Path) -> None:
    lock_path = tmp_path / "test.lock"
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        fd = acquire(lock_path)
        try:
            order.append(f"{name}:in")
            time.sleep(0.1)
            order.append(f"{name}:out")
        finally:
            release(fd)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert order in (
        ["a:in", "a:out", "b:in", "b:out"],
        ["b:in", "b:out", "a:in", "a:out"],
    )
