"""Usage tracker: tokens always; USD only when a provider reports it."""

from backend.cost_tracker import CostTracker


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
