"""Claude/GLM writeup must wait for ResultMessage before the recap query."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from backend.agents.claude_solver import ClaudeSolver
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND


def _result(session_id: str = "ses") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
    )


def _text(body: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=body)], model="glm-5.2")


class _FakeClient:
    def __init__(self, batches: list[list[object]]) -> None:
        self.queries: list[str] = []
        self._batches = list(batches)
        self._i = 0

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        batch = self._batches[self._i]
        self._i += 1
        for msg in batch:
            yield msg


def _make_solver(monkeypatch: pytest.MonkeyPatch) -> ClaudeSolver:
    settings = MagicMock()
    settings.sandbox_image = "ctf-sandbox-core"
    settings.container_memory_limit = "4g"
    settings.anthropic_api_key = "sk-test"
    settings.anthropic_base_url = "https://example.test/anthropic"
    settings.anthropic_model_id = "bigmodel/glm-5.2"
    settings.auto_confirm_flags = False

    monkeypatch.setattr("backend.agents.claude_solver.DockerSandbox", lambda **kw: MagicMock())
    monkeypatch.setattr("backend.agents.claude_solver.SolverTracer", lambda *a, **kw: MagicMock())

    solver = ClaudeSolver(
        model_spec="claude-sdk/bigmodel/glm-5.2",
        challenge_dir="/tmp/demo",
        meta=ChallengeMeta(name="demo", description="d", flags_required=1),
        cost_tracker=MagicMock(),
        settings=settings,
    )
    solver.tracer = MagicMock()
    return solver


@pytest.mark.asyncio
async def test_solve_turn_consumes_result_after_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solver = _make_solver(monkeypatch)
    client = _FakeClient(
        [
            [
                _text("working on the cipher"),
                _text("flag submitted"),
                _result("solve"),
            ]
        ]
    )
    solver._client = client
    solver._confirmed = True
    solver._flag = "flag{x}"

    result = await solver.run_until_done_or_gave_up()
    assert result.status == FLAG_FOUND
    assert solver._receive_idle is True
    assert solver._session_id == "solve"
    assert client._i == 1


@pytest.mark.asyncio
async def test_writeup_drains_leftover_result_then_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solver = _make_solver(monkeypatch)
    recap = (
        "Challenge\nMonoalphabetic quote cipher.\n\n"
        "Key insight\nTuring frequency quote.\n\n"
        "How\n1. counted letters\n2. mapped the quote\n3. submitted the flag\n\n"
        "What I tried\nWrong key length.\n\n"
        "Why it worked\nThe plaintext was a known quote."
    )
    client = _FakeClient(
        [
            [_result("leftover")],
            [_text(recap), _result("writeup")],
        ]
    )
    solver._client = client
    solver._receive_idle = False

    text = await solver.produce_writeup()
    assert "Turing frequency quote" in text
    assert len(client.queries) == 1
    assert solver._receive_idle is True
    assert solver._session_id == "writeup"
