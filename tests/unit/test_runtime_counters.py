"""Tests for Claude Code runtime counter parsing."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.claude.runtime_counters import (
    RuntimeCounters,
    parse_runtime_counters,
)


def test_parse_runtime_counters_real_status_payload() -> None:
    payload = {
        "hook_event_name": "Status",
        "session_id": "abc123",
        "cwd": "workspace/project",
        "model": {"id": "claude-opus-4-1", "display_name": "Opus"},
        "workspace": {
            "current_dir": "workspace/project",
            "project_dir": "workspace/project",
        },
        "version": "2.1.153",
        "cost": {
            "total_cost_usd": 0.01234,
            "total_duration_ms": 45_000,
            "total_api_duration_ms": 2_300,
            "total_lines_added": 156,
            "total_lines_removed": 23,
        },
        "context_window": {
            "total_input_tokens": 15_234,
            "total_output_tokens": 4_521,
            "context_window_size": 200_000,
            "used_percentage": 42.5,
            "remaining_percentage": 57.5,
            "current_usage": {
                "input_tokens": 8_500,
                "output_tokens": 1_200,
                "cache_creation_input_tokens": 5_000,
                "cache_read_input_tokens": 2_000,
            },
        },
    }

    counters = parse_runtime_counters(payload)

    assert counters == RuntimeCounters(
        api_duration_ms=2_300,
        total_duration_ms=45_000,
        cost_usd=Decimal("0.01234"),
        input_tokens=8_500,
        output_tokens=1_200,
        cache_creation_input_tokens=5_000,
        cache_read_input_tokens=2_000,
        harness="claude-code",
        model="claude-opus-4-1",
    )


def test_parse_runtime_counters_accepts_canonical_cost_aliases() -> None:
    counters = parse_runtime_counters(
        {
            "cost": {
                "cost_usd": 1.25,
                "api_duration_ms": 300,
                "total_duration_ms": 900,
            },
            "context_window": {"current_usage": {"input_tokens": 10, "output_tokens": 5}},
        }
    )

    assert counters is not None
    assert counters.api_duration_ms == 300
    assert counters.total_duration_ms == 900
    assert counters.cost_usd == Decimal("1.25")
    assert counters.input_tokens == 10
    assert counters.output_tokens == 5


def test_parse_runtime_counters_missing_cost_returns_none() -> None:
    assert parse_runtime_counters({}) is None
    assert (
        parse_runtime_counters({"context_window": {"current_usage": {"input_tokens": 1}}}) is None
    )


def test_runtime_counters_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RuntimeCounters.model_validate({"cost_usd": Decimal("0.1"), "unknown": 1})


def test_parse_runtime_counters_drops_type_mismatched_fields() -> None:
    counters = parse_runtime_counters(
        {
            "cost": {
                "total_cost_usd": "0.01234",
                "total_duration_ms": 45_000,
                "total_api_duration_ms": "2300",
            },
            "context_window": {
                "current_usage": {
                    "input_tokens": "8500",
                    "output_tokens": 1_200,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": 2_000,
                }
            },
        }
    )

    assert counters == RuntimeCounters(
        total_duration_ms=45_000,
        output_tokens=1_200,
        cache_read_input_tokens=2_000,
        harness="claude-code",
        model=None,
    )


def test_parse_runtime_counters_stamps_harness_and_model() -> None:
    """The capture path stamps ``harness="claude-code"`` + the billed model id."""
    counters = parse_runtime_counters(
        {
            "model": {"id": "claude-opus-4-1", "display_name": "Opus"},
            "cost": {"cost_usd": 1.25},
        }
    )

    assert counters is not None
    # The ``id`` key wins over ``display_name`` so calibration keys on the
    # canonical model string, not the human label.
    assert counters.harness == "claude-code"
    assert counters.model == "claude-opus-4-1"


def test_parse_runtime_counters_accepts_string_model_shape() -> None:
    """Claude has shipped ``model`` as a bare string too -- both shapes parse."""
    counters = parse_runtime_counters({"model": "claude-sonnet-4-6", "cost": {"cost_usd": 0.5}})

    assert counters is not None
    assert counters.harness == "claude-code"
    assert counters.model == "claude-sonnet-4-6"


def test_parse_runtime_counters_model_none_when_absent() -> None:
    """A payload with no ``model`` block stamps harness but leaves model NULL."""
    counters = parse_runtime_counters({"cost": {"cost_usd": 0.5}})

    assert counters is not None
    assert counters.harness == "claude-code"
    assert counters.model is None


def test_parse_runtime_counters_model_none_when_wrong_typed() -> None:
    """A wrong-typed ``model`` block (e.g. an int) leaves the model id NULL."""
    counters = parse_runtime_counters({"model": 7, "cost": {"cost_usd": 0.5}})

    assert counters is not None
    assert counters.harness == "claude-code"
    assert counters.model is None


def test_parse_runtime_counters_falls_back_to_display_name() -> None:
    """A ``model`` mapping lacking ``id`` falls back to ``display_name``."""
    counters = parse_runtime_counters(
        {"model": {"display_name": "Opus 4.7"}, "cost": {"cost_usd": 0.5}}
    )

    assert counters is not None
    assert counters.model == "Opus 4.7"


def test_runtime_counters_model_validate_rejects_bad_harness_type() -> None:
    """``harness`` must be a string -- a non-string value fails validation."""
    with pytest.raises(ValidationError, match="harness"):
        RuntimeCounters.model_validate({"harness": 7})


def test_runtime_counters_model_validate_rejects_bad_model_type() -> None:
    """``model`` must be a string -- a non-string value fails validation."""
    with pytest.raises(ValidationError, match="model"):
        RuntimeCounters.model_validate({"model": ["claude-opus-4-1"]})
