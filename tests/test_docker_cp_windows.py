"""Windows-safe docker cp path sanitization."""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

from backend.sandbox.docker_client import _extract_tar_to_dest, _sanitize_windows_path


def test_sanitize_windows_path_replaces_colons() -> None:
    assert _sanitize_windows_path("opt/sagemath/man/man3/App::Cpan.3") == (
        "opt/sagemath/man/man3/App__Cpan.3"
    )


def test_extract_tar_sanitizes_invalid_filenames_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        data = b"pod\n"
        info = tarfile.TarInfo(name="opt/sagemath/man/man3/App::Cpan.3")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    _extract_tar_to_dest(buf.getvalue(), tmp_path)
    assert (tmp_path / "opt" / "sagemath" / "man" / "man3" / "App__Cpan.3").read_bytes() == data


def test_extract_tar_materializes_symlinks_as_copies(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo(name="opt/sagemath/bin/python3")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        sym = tarfile.TarInfo(name="opt/sagemath/bin/python")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "python3"
        tf.addfile(sym)
    _extract_tar_to_dest(buf.getvalue(), tmp_path)
    assert (tmp_path / "opt" / "sagemath" / "bin" / "python3").read_bytes() == data
    assert (tmp_path / "opt" / "sagemath" / "bin" / "python").read_bytes() == data
