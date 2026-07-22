"""pip3 CTF wrapper: bare `pip install` must add --break-system-packages."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "sandbox" / "scripts" / "pip3_ctf_wrapper.sh"


def test_pip3_wrapper_script_exists_and_executable_bits():
    assert WRAPPER.is_file()
    text = WRAPPER.read_text()
    assert "--break-system-packages" in text
    assert "install" in text


def test_pip3_wrapper_injects_break_system_packages(tmp_path: Path):
    fake = tmp_path / "fake_pip3"
    log = tmp_path / "args.log"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {log}\nexit 0\n')
    fake.chmod(0o755)
    env = {**os.environ, "CTF_REAL_PIP3": str(fake)}
    r = subprocess.run(
        ["bash", str(WRAPPER), "install", "openpyxl"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    args = log.read_text().splitlines()
    assert args[0] == "install"
    assert "--break-system-packages" in args
    assert "openpyxl" in args


def test_pip3_wrapper_preserves_explicit_break_flag(tmp_path: Path):
    fake = tmp_path / "fake_pip3"
    log = tmp_path / "args.log"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {log}\nexit 0\n')
    fake.chmod(0o755)
    env = {**os.environ, "CTF_REAL_PIP3": str(fake)}
    r = subprocess.run(
        ["bash", str(WRAPPER), "install", "--break-system-packages", "x"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    args = log.read_text().splitlines()
    assert args.count("--break-system-packages") == 1


def test_pip3_wrapper_passes_non_install_through(tmp_path: Path):
    fake = tmp_path / "fake_pip3"
    log = tmp_path / "args.log"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {log}\nexit 0\n')
    fake.chmod(0o755)
    env = {**os.environ, "CTF_REAL_PIP3": str(fake)}
    r = subprocess.run(
        ["bash", str(WRAPPER), "show", "requests"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == ["show", "requests"]
