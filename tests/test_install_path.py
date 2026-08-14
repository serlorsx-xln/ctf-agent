"""Install-path registry (relocatable global ``artemis`` command)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.install_path import (
    ensure_global_cli,
    looks_like_repo,
    read_install_path,
    remember_install_path,
)


def _fake_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='artemis'\n", encoding="utf-8")
    bin_dir = root / "chassis" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "artemis").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = tmp_path / "share" / "install-path.txt"
    cli = tmp_path / "bin" / "artemis"
    monkeypatch.setattr("backend.install_path.install_path_file", lambda: registry)
    monkeypatch.setattr("backend.install_path.global_cli_path", lambda: cli)
    return cli


def test_looks_like_repo(tmp_path: Path) -> None:
    assert looks_like_repo(tmp_path) is False
    _fake_repo(tmp_path)
    assert looks_like_repo(tmp_path) is True


def test_remember_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _fake_repo(tmp_path / "checkout")
    cli = _isolate_home(tmp_path, monkeypatch)
    assert remember_install_path(repo) == repo.resolve()
    assert read_install_path() == repo.resolve()
    # Unchanged write is a no-op (still readable).
    assert remember_install_path(repo) == repo.resolve()
    registry = tmp_path / "share" / "install-path.txt"
    assert registry.read_text(encoding="utf-8").strip() == str(repo.resolve())
    assert cli.is_file()
    assert "install-path.txt" in cli.read_text(encoding="utf-8")
    # Unchanged path + real wrapper: do not rewrite the CLI file.
    before = cli.stat().st_mtime_ns
    assert remember_install_path(repo) == repo.resolve()
    assert cli.stat().st_mtime_ns == before


def test_remember_rejects_random_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_home(tmp_path, monkeypatch)
    assert remember_install_path(tmp_path / "nope") is None
    assert not (tmp_path / "share" / "install-path.txt").is_file()


def test_ensure_global_cli_replaces_broken_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "bin" / "artemis"
    dest.parent.mkdir(parents=True)
    try:
        dest.symlink_to(tmp_path / "missing" / "artemis")
    except OSError:
        pytest.skip("symlinks unavailable")
    assert dest.is_symlink()
    monkeypatch.setattr("backend.install_path.global_cli_path", lambda: dest)
    assert ensure_global_cli() == dest
    assert dest.is_file()
    assert not dest.is_symlink()
    text = dest.read_text(encoding="utf-8")
    assert "install-path.txt" in text
    assert "ARTEMIS_REPO_ROOT" in text


def test_ensure_global_cli_leaves_foreign_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "bin" / "artemis"
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    monkeypatch.setattr("backend.install_path.global_cli_path", lambda: dest)
    assert ensure_global_cli() is None
    assert dest.read_text(encoding="utf-8") == "#!/bin/sh\necho custom\n"
