"""Tests for the ``context_tokens`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from typing import Any

from eawf.runtime.runtimes.claude.statusline_modules import context_tokens


def _usage(**counts: Any) -> dict[str, Any]:
    """Return a host payload whose last call reported *counts*."""
    return {"context_window": {"context_window_size": 200_000, "current_usage": counts}}


def test_current_usage_renders_occupancy_against_the_window() -> None:
    seg = context_tokens.build(
        _usage(
            input_tokens=500,
            output_tokens=56,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        None,
    )
    assert seg.module == "context_tokens"
    assert seg.text == "ctx:500/200.0k"
    assert seg.status == "ok"


def test_output_tokens_do_not_count_toward_occupancy() -> None:
    seg = context_tokens.build(
        _usage(
            input_tokens=10,
            output_tokens=99_999,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=5,
        ),
        None,
    )
    assert seg.text == "ctx:20/200.0k"


def test_missing_payload_names_why() -> None:
    seg = context_tokens.build({}, None)
    assert seg.text == "ctx:n/a(no-current-usage)"
    assert seg.status == "missing"


def test_legacy_token_usage_shapes_are_not_host_fields() -> None:
    seg = context_tokens.build({"token_usage": {"input_tokens": 1234, "output_tokens": 56}}, None)
    assert seg.text == "ctx:n/a(no-current-usage)"


def test_negative_or_wrong_type_is_not_a_count() -> None:
    seg = context_tokens.build(
        _usage(input_tokens=-1, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        None,
    )
    assert seg.text == "ctx:n/a(partial-usage)"
    seg = context_tokens.build(
        _usage(input_tokens="1234", cache_creation_input_tokens=0, cache_read_input_tokens=0),
        None,
    )
    assert seg.text == "ctx:n/a(partial-usage)"


def test_non_positive_window_size_renders_the_count_alone() -> None:
    payload = _usage(input_tokens=7, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    payload["context_window"]["context_window_size"] = 0
    assert context_tokens.build(payload, None).text == "ctx:7"
