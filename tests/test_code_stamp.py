from pathlib import Path

from backend.daemon.code_stamp import (
    backend_code_stamp,
    running_stamp,
    write_running_stamp,
)


def test_stamp_stable_then_changes_with_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    (backend / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = backend_code_stamp(repo)
    assert first == backend_code_stamp(repo)
    (backend / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert backend_code_stamp(repo) != first


def test_missing_running_stamp_is_empty(tmp_path: Path) -> None:
    assert running_stamp(tmp_path) == ""


def test_write_running_stamp(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "a.py").write_text("x = 1\n", encoding="utf-8")
    cache = tmp_path / "cache"
    write_running_stamp(cache, repo, pid=4242)
    assert running_stamp(cache) == backend_code_stamp(repo)
    assert (cache / "daemon.pid").read_text(encoding="utf-8") == "4242"
