"""Writable pack scratch: blutter's Dart VM build must survive the container.

Without a host bind, every new sandbox recompiled the Dart SDK for the APK's
snapshot version — tens of minutes before the agent sees a single symbol.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend.platform_paths import docker_volume_path
from backend.sandbox.container import DockerSandbox
from backend.tool_router import (
    PACK_SPECS,
    blutter_wrapper_source,
    bootstrap_script,
    pack_marker_path,
    pack_state_dir,
    pack_state_root,
)


def test_mobile_declares_blutter_state_dir() -> None:
    assert PACK_SPECS["mobile"].state_dirs == ("/var/cache/ctf-blutter",)
    assert PACK_SPECS["mobile"].shared_state is True


def test_state_root_is_outside_evictable_pack_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CTF_PACK_CACHE", str(tmp_path / "packs"))
    monkeypatch.delenv("CTF_PACK_STATE", raising=False)
    root = pack_state_root()
    assert not str(root).startswith(str(tmp_path / "packs"))
    assert str(pack_state_dir("mobile", "/var/cache/ctf-blutter")).replace("\\", "/").endswith(
        "var/cache/ctf-blutter"
    )


def test_state_dirs_bind_rw_and_are_created(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CTF_PACK_STATE", str(tmp_path / "state"))
    sandbox = DockerSandbox(image="ctf-sandbox-core", challenge_dir=str(tmp_path))
    binds = sandbox._pack_state_bind_strings()

    host = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id=sandbox.session_id)
    assert host.is_dir()
    assert f"{docker_volume_path(host)}:/var/cache/ctf-blutter:rw" in binds
    assert all(b.endswith(":rw") for b in binds)


def test_state_binds_even_when_pack_binds_disabled(tmp_path: Path, monkeypatch) -> None:
    """CTF_PACK_BIND=0 must not skip the RW blutter cache mount."""
    monkeypatch.setenv("CTF_PACK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("CTF_PACK_BIND", "0")
    sandbox = DockerSandbox(image="ctf-sandbox-core", challenge_dir=str(tmp_path))
    # Method itself always returns RW binds; start() must call it even when
    # pack_binds_enabled() is false (regression covered in container.py).
    binds = sandbox._pack_state_bind_strings()
    assert any(b.endswith(":/var/cache/ctf-blutter:rw") for b in binds)
    from backend.tool_router import pack_binds_enabled

    assert pack_binds_enabled() is False


def test_blutter_state_is_shared_across_sessions(tmp_path: Path, monkeypatch) -> None:
    """Dart VM cache must not recompile per Artemis session."""
    monkeypatch.setenv("CTF_PACK_STATE", str(tmp_path / "state"))
    a = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id="ses_a")
    b = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id="ses_b")
    assert a == b
    assert "ses_a" not in str(a)
    assert "ses_b" not in str(b)


def test_adopt_session_scoped_blutter_into_shared(tmp_path: Path, monkeypatch) -> None:
    import platform

    monkeypatch.setenv("CTF_PACK_STATE", str(tmp_path / "state"))
    arch = platform.machine().replace("aarch64", "arm64")
    # Simulate an older session-scoped build.
    legacy = (
        tmp_path
        / "state"
        / "mobile"
        / arch
        / "_default"
        / "var"
        / "cache"
        / "ctf-blutter"
    )
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "blutter_dartvm3.10.4_android_arm64").write_bytes(b"vm")
    shared = pack_state_dir("mobile", "/var/cache/ctf-blutter", session_id="ses_new")
    assert (shared / "bin" / "blutter_dartvm3.10.4_android_arm64").is_file()


def test_blutter_wrapper_written_by_bootstrap_not_bound_readonly() -> None:
    # Bound RO from the donor, the wrapper could not be fixed without a rebuild.
    assert "/usr/local/bin/blutter" not in PACK_SPECS["mobile"].paths_to_bind()
    script = bootstrap_script("mobile")
    assert "cat > /usr/local/bin/blutter <<'CTF_BLUTTER_WRAPPER_EOF'" in script
    assert "chmod +x /usr/local/bin/blutter" in script
    if sys.platform != "win32":
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_marker_tracks_wrapper_changes(monkeypatch) -> None:
    before = pack_marker_path("mobile")
    monkeypatch.setattr(
        "backend.tool_router.blutter_wrapper_source", lambda: "#!/bin/bash\necho changed\n"
    )
    assert pack_marker_path("mobile") != before


def test_wrapper_keeps_compiled_dart_vm_across_runs() -> None:
    src = blutter_wrapper_source()
    # rsync --delete mirrors the read-only install tree; anything not excluded
    # (notably bin/, which holds blutter_dartvm*) would be wiped every run.
    assert "CACHED_DIRS=(dartsdk build bin packages .build.lock)" in src
    assert "--delete" in src
    assert "flock" in src
