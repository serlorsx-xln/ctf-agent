"""CLI model expansion and duplicate-runner ids."""

from __future__ import annotations

import pytest

from backend.models import assign_runner_ids, expand_model_cli_args


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
