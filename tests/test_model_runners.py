"""CLI model expansion and duplicate-runner ids."""

from __future__ import annotations

import pytest

from backend.models import (
    agent_display_key,
    assign_runner_ids,
    effort_from_spec,
    expand_model_cli_args,
    model_id_from_spec,
)


def test_expand_star_multiplier():
    assert expand_model_cli_args(["cursor/grok-4.5*3"]) == [
        "cursor/grok-4.5",
        "cursor/grok-4.5",
        "cursor/grok-4.5",
    ]


def test_expand_x_multiplier():
    assert expand_model_cli_args(["cursor/composer-2.5x2"]) == [
        "cursor/composer-2.5",
        "cursor/composer-2.5",
    ]


def test_expand_comma_separated_single_arg():
    assert expand_model_cli_args(["claude-sdk/glm-5.2,cursor/grok-4.5"]) == [
        "claude-sdk/glm-5.2",
        "cursor/grok-4.5",
    ]


def test_expand_comma_with_multiplier():
    assert expand_model_cli_args(["cursor/a*2,codex/b"]) == [
        "cursor/a",
        "cursor/a",
        "codex/b",
    ]


def test_expand_mixed_and_passthrough():
    assert expand_model_cli_args(
        ["cursor/grok-4.5*2", "claude-sdk/claude-opus-4-6", "codex/gpt-5.4"]
    ) == [
        "cursor/grok-4.5",
        "cursor/grok-4.5",
        "claude-sdk/claude-opus-4-6",
        "codex/gpt-5.4",
    ]


def test_expand_does_not_mangle_ids_ending_in_x2():
    # No slash → not treated as xN multiplier
    assert expand_model_cli_args(["somethingx2"]) == ["somethingx2"]


def test_expand_rejects_zero():
    with pytest.raises(ValueError):
        expand_model_cli_args(["cursor/grok*0"])


def test_model_id_keeps_custom_claude_slashes():
    assert model_id_from_spec("claude-sdk/bigmodel/glm-5.2") == "bigmodel/glm-5.2"
    assert model_id_from_spec("claude-sdk/bigmodel/glm-5.2/max") == "bigmodel/glm-5.2"
    assert model_id_from_spec("claude-sdk/claude-opus-4-6/max") == "claude-opus-4-6"
    assert effort_from_spec("claude-sdk/bigmodel/glm-5.2/max") == "max"
    assert effort_from_spec("claude-sdk/bigmodel/glm-5.2") is None


def test_agent_display_key_keeps_slashy_claude_ids():
    specs = [
        "claude-sdk/PSU-araya/psu-gemma",
        "claude-sdk/aliyuncs/MiniMax-M2.1",
        "claude-sdk/aliyuncs/MiniMax-M2.5",
        "claude-sdk/aliyuncs/glm-4.7",
    ]
    keys = [agent_display_key(rid, spec) for rid, spec in assign_runner_ids(specs)]
    assert keys == [
        "PSU-araya/psu-gemma",
        "aliyuncs/MiniMax-M2.1",
        "aliyuncs/MiniMax-M2.5",
        "aliyuncs/glm-4.7",
    ]


def test_assign_runner_ids_unique():
    assert assign_runner_ids(["a", "b"]) == [("a", "a"), ("b", "b")]


def test_assign_runner_ids_duplicates():
    specs = ["cursor/grok-4.5", "cursor/grok-4.5", "cursor/grok-4.5"]
    assert assign_runner_ids(specs) == [
        ("cursor/grok-4.5#1", "cursor/grok-4.5"),
        ("cursor/grok-4.5#2", "cursor/grok-4.5"),
        ("cursor/grok-4.5#3", "cursor/grok-4.5"),
    ]


def test_assign_runner_ids_partial_dup():
    specs = ["cursor/a", "cursor/b", "cursor/a"]
    assert assign_runner_ids(specs) == [
        ("cursor/a#1", "cursor/a"),
        ("cursor/b", "cursor/b"),
        ("cursor/a#2", "cursor/a"),
    ]
