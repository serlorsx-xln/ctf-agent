"""Usage tracker: tokens always; USD only when a provider reports it."""

from types import SimpleNamespace

from backend.cost_tracker import CostTracker, usage_from_provider


def test_usage_from_provider_anthropic_and_openai_aliases():
    assert usage_from_provider(None) == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cost_usd": None,
    }
    anth = usage_from_provider(
        {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 8}
    )
    assert anth["input"] == 100
    assert anth["output"] == 20
    assert anth["cache_read"] == 8

    openai = usage_from_provider({"prompt_tokens": 50, "completion_tokens": 7})
    assert openai["input"] == 50
    assert openai["output"] == 7

    camel = usage_from_provider({"inputTokens": 9, "outputTokens": 2, "cachedInputTokens": 1})
    assert camel == {"input": 9, "output": 2, "cache_read": 1, "cost_usd": None}

    gemini = usage_from_provider(
        SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=80,
                candidates_token_count=12,
                cached_content_token_count=4,
            )
        )
    )
    assert gemini["input"] == 80
    assert gemini["output"] == 12
    assert gemini["cache_read"] == 4


def test_usage_from_provider_nested_and_getattr():
    nested = usage_from_provider({"usage": {"prompt_tokens": 11, "completion_tokens": 3}})
    assert nested["input"] == 11
    assert nested["output"] == 3

    obj = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=40, output_tokens=5, total_cost_usd=0.12)
    )
    parsed = usage_from_provider(obj)
    assert parsed["input"] == 40
    assert parsed["output"] == 5
    assert parsed["cost_usd"] == 0.12


def test_tokens_without_reported_cost():
    t = CostTracker()
    t.record_tokens(
        "agent/a",
        "grok-4.5",
        input_tokens=1_000_000,
        output_tokens=1000,
        cache_read_tokens=900_000,
        duration_seconds=12.5,
    )
    assert t.total_tokens > 0
    assert t.total_reported_cost_usd is None
    assert t.total_cost_usd == 0.0
    text = t.format_usage("agent/a")
    assert "1.0M in" in text
    assert "12.5s" in text
    assert "$" not in text
    assert "reported" not in t.format_total()


def test_provider_reported_cost_accumulates():
    t = CostTracker()
    t.record_tokens(
        "agent/claude",
        "claude-opus-4-6",
        input_tokens=1000,
        output_tokens=100,
        reported_cost_usd=0.05,
        duration_seconds=1.0,
    )
    t.record_tokens(
        "agent/claude",
        "claude-opus-4-6",
        input_tokens=500,
        output_tokens=50,
        reported_cost_usd=0.02,
        duration_seconds=0.5,
    )
    assert t.total_reported_cost_usd == 0.07
    assert "reported" in t.format_usage("agent/claude")
    assert "$0.07 reported" in t.format_total()


def test_mixed_agents_only_sum_reported():
    t = CostTracker()
    t.record_tokens("cursor/a", "grok-4.5", input_tokens=100, output_tokens=10)
    t.record_tokens(
        "claude/b",
        "claude-opus-4-6",
        input_tokens=100,
        output_tokens=10,
        reported_cost_usd=1.25,
    )
    assert t.total_reported_cost_usd == 1.25
    assert "$" not in t.format_usage("cursor/a")
    assert "$1.25 reported" in t.format_usage("claude/b")


def test_publish_usage_json(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    from backend.cost_tracker import clear_published_usage

    clear_published_usage()
    t = CostTracker()
    t.record_tokens("a", "m", input_tokens=1000, output_tokens=200)
    t.publish(force=True)
    path = tmp_path / "usage.json"
    assert path.is_file()
    data = __import__("json").loads(path.read_text())
    assert data["tokens"] == 1200
    assert data["cost_usd"] is None

    t.record_tokens("a", "m", input_tokens=10, output_tokens=5, reported_cost_usd=0.5)
    t.publish(force=True)
    data = __import__("json").loads(path.read_text())
    assert data["tokens"] == 1215
    assert data["cost_usd"] == 0.5

    clear_published_usage()
    assert not path.exists()


def test_usage_json_is_session_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    from backend.cost_tracker import clear_published_usage, publish_usage_snapshot

    monkeypatch.setenv("ARTEMIS_SESSION_ID", "ses_a")
    publish_usage_snapshot(tokens=10, force=True)
    path_a = tmp_path / "sessions" / "ses_a" / "usage.json"
    assert path_a.is_file()

    monkeypatch.setenv("ARTEMIS_SESSION_ID", "ses_b")
    publish_usage_snapshot(tokens=20, force=True)
    path_b = tmp_path / "sessions" / "ses_b" / "usage.json"
    assert path_b.is_file()

    clear_published_usage("ses_a")
    assert not path_a.exists()
    assert path_b.is_file()


def test_publish_with_pending_does_not_mutate_totals(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CACHE", str(tmp_path))
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    from backend.cost_tracker import clear_published_usage

    clear_published_usage()
    t = CostTracker()
    t.record_tokens("a", "m", input_tokens=100, output_tokens=20)
    t.publish(force=True)
    assert t.total_tokens == 120

    t.publish_with_pending(input_tokens=50, output_tokens=10, force=True)
    data = __import__("json").loads((tmp_path / "usage.json").read_text())
    assert data["tokens"] == 180  # committed 120 + pending 60
    assert t.total_tokens == 120  # pending not committed

    t.record_tokens("a", "m", input_tokens=50, output_tokens=10)
    t.publish(force=True)
    data = __import__("json").loads((tmp_path / "usage.json").read_text())
    assert data["tokens"] == 180
    assert t.total_tokens == 180
