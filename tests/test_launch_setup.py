"""Pre-TUI launch setup gate tests."""

from __future__ import annotations

import io

from backend.launch_setup import (
    LaunchSetupReport,
    assess_launch_setup,
    run_launch_setup_gate,
)


def test_assess_reports_warm_missing(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: type(
            "S",
            (),
            {
                "ready": True,
                "docker_ok": True,
                "core_image": True,
                "packs_missing": [],
                "message": "ok",
            },
        )(),
    )
    monkeypatch.setattr(
        "backend.sandbox.warm_runtime.read_warm_runtime",
        lambda pack_id, **kwargs: None,
    )
    report = assess_launch_setup()
    assert report.gate_ready is True
    assert report.needs_prompt is False
    assert "ghidra" in report.warm_missing


def test_assess_no_prompt_when_fully_warm(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.setattr(
        "backend.sandbox.setup_ready.probe_setup_status",
        lambda: type(
            "S",
            (),
            {
                "ready": True,
                "docker_ok": True,
                "core_image": True,
                "packs_missing": [],
                "message": "ok",
            },
        )(),
    )
    monkeypatch.setattr(
        "backend.sandbox.warm_runtime.read_warm_runtime",
        lambda pack_id, **kwargs: f"ctf-sandbox-warm-{pack_id}",
    )
    report = assess_launch_setup()
    assert report.needs_prompt is False


def test_skip_env_bypasses_prompt(monkeypatch):
    monkeypatch.setenv("ARTEMIS_SKIP_LAUNCH_SETUP", "1")
    stderr = io.StringIO()
    assert run_launch_setup_gate(stderr=stderr, interactive=True) == 0
    assert stderr.getvalue() == ""


def test_decline_continues(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_LAUNCH_SETUP", raising=False)
    monkeypatch.delenv("ARTEMIS_SKIP_SETUP_GATE", raising=False)
    monkeypatch.delenv("ARTEMIS_SETUP_AUTO", raising=False)
    monkeypatch.setattr(
        "backend.launch_setup.assess_launch_setup",
        lambda: LaunchSetupReport(
            gate_ready=False,
            docker_ok=True,
            core_image=False,
            packs_missing=["web"],
            warm_missing=["ghidra"],
            message="Missing core",
        ),
    )
    stdin = io.StringIO("n\n")
    stderr = io.StringIO()
    called = {"setup": False}

    async def _fake(*, stderr):  # noqa: ANN001
        called["setup"] = True
        return []

    monkeypatch.setattr("backend.launch_setup._run_full_setup", _fake)
    assert run_launch_setup_gate(stdin=stdin, stderr=stderr, interactive=True) == 0
    assert called["setup"] is False
    assert "continuing with what is installed" in stderr.getvalue()


def test_yes_runs_setup(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_LAUNCH_SETUP", raising=False)
    monkeypatch.delenv("ARTEMIS_SETUP_AUTO", raising=False)
    stdin = io.StringIO("y\n")
    stderr = io.StringIO()

    async def _fake(*, stderr):  # noqa: ANN001
        stderr.write("  Building…\n")
        return ["OK  Built L0 image: ctf-sandbox-core"]

    monkeypatch.setattr("backend.launch_setup._run_full_setup", _fake)
    calls = {"n": 0}

    def _assess():
        calls["n"] += 1
        if calls["n"] == 1:
            return LaunchSetupReport(
                gate_ready=False,
                docker_ok=True,
                core_image=False,
                packs_missing=[],
                warm_missing=["web"],
                message="Missing core",
            )
        return LaunchSetupReport(
            gate_ready=True,
            docker_ok=True,
            core_image=True,
            packs_missing=[],
            warm_missing=[],
            message="ok",
        )

    monkeypatch.setattr("backend.launch_setup.assess_launch_setup", _assess)
    assert run_launch_setup_gate(stdin=stdin, stderr=stderr, interactive=True) == 0
    out = stderr.getvalue()
    assert "Building" in out
    assert "setup complete" in out


def test_non_interactive_skips(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SKIP_LAUNCH_SETUP", raising=False)
    monkeypatch.delenv("ARTEMIS_SETUP_AUTO", raising=False)
    monkeypatch.setattr(
        "backend.launch_setup.assess_launch_setup",
        lambda: LaunchSetupReport(
            gate_ready=False,
            docker_ok=False,
            core_image=False,
            packs_missing=["web"],
            warm_missing=[],
            message="Docker down",
        ),
    )
    stderr = io.StringIO()
    assert run_launch_setup_gate(stdin=io.StringIO(), stderr=stderr, interactive=False) == 0
    assert "non-interactive" in stderr.getvalue()


def test_auto_yes_runs_without_tty(monkeypatch):
    monkeypatch.setenv("ARTEMIS_SETUP_AUTO", "1")
    monkeypatch.delenv("ARTEMIS_SKIP_LAUNCH_SETUP", raising=False)
    monkeypatch.setattr(
        "backend.launch_setup.assess_launch_setup",
        lambda: LaunchSetupReport(
            gate_ready=False,
            docker_ok=True,
            core_image=False,
            packs_missing=[],
            warm_missing=["linux"],
            message="Missing core",
        ),
    )

    async def _fake(*, stderr):  # noqa: ANN001
        return ["OK  warm"]

    monkeypatch.setattr("backend.launch_setup._run_full_setup", _fake)
    stderr = io.StringIO()
    assert run_launch_setup_gate(stdin=io.StringIO(), stderr=stderr, interactive=False) == 0
    assert "ARTEMIS_SETUP_AUTO=1" in stderr.getvalue()


def test_full_setup_bakes_packs(monkeypatch):
    import asyncio

    from backend.launch_setup import _run_full_setup

    captured: dict = {}

    async def fake_setup(**kwargs):
        captured.update(kwargs)
        return ["ok"]

    monkeypatch.setattr("backend.sandbox.setup_bake.run_setup", fake_setup)
    stderr = io.StringIO()
    assert asyncio.run(_run_full_setup(stderr=stderr)) == ["ok"]
    assert captured["packs"] is None
    assert captured["skip_core"] is False
    assert captured["skip_warm_runtime"] is False
    assert captured["skip_blutter_vm"] is True
    assert "full sandbox setup" in stderr.getvalue()
