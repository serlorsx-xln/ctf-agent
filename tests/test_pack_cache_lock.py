"""Cross-process pack extract lock: one materialize, waiters share cache."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import backend.sandbox as sandbox


def test_pack_flock_serializes_extract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CTF_PACK_CACHE", str(tmp_path))

    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        fd = sandbox._acquire_pack_flock("crypto")
        try:
            order.append(f"{name}:in")
            time.sleep(0.15)
            order.append(f"{name}:out")
        finally:
            sandbox._release_pack_flock(fd)

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
    assert (tmp_path / "crypto" / ".extract.lock").is_file()


def test_pack_cache_is_ready_requires_marker_and_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CTF_PACK_CACHE", str(tmp_path))
    # Force arch dir used by pack_cache_dir
    arch = "arm64" if os.uname().machine in ("arm64", "aarch64") else os.uname().machine
    cache = tmp_path / "crypto" / arch
    assert sandbox._pack_cache_is_ready("crypto") is False

    (cache / "opt" / "sagemath").mkdir(parents=True)
    (cache / "usr" / "local" / "bin").mkdir(parents=True)
    (cache / "usr" / "local" / "bin" / "sage").write_text("#!/bin/bash\n")
    (cache / "usr" / "local" / "bin" / "sage-python").write_text("#!/bin/bash\n")
    assert sandbox._pack_cache_is_ready("crypto") is False

    (cache / ".ready").write_text("ok\n")
    assert sandbox._pack_cache_is_ready("crypto") is True
