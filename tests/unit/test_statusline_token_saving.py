"""Tests for the ``token_saving`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from typing import Any

from eawf.runtime.runtimes.claude.statusline_modules import token_saving


def _usage(**counts: Any) -> dict[str, Any]:
    """Return a host payload whose last call reported *counts*."""
    return {"context_window": {"current_usage": counts}}


def test_missing_payload_names_why() -> None:
    seg = token_saving.build({}, None)
    assert seg.text == "save:n/a(no-current-usage)"
    assert seg.status == "missing"


def test_current_usage_with_cache_read_renders_percent() -> None:
    seg = token_saving.build(
        _usage(cache_read_input_tokens=800, cache_creation_input_tokens=0, input_tokens=200),
        None,
    )
    # 800 / (800 + 0 + 200) = 0.8 → "save:80%"
    assert seg.text == "save:80%"
    assert seg.status == "ok"


def test_cache_creation_counts_in_the_denominator() -> None:
    seg = token_saving.build(
        _usage(cache_read_input_tokens=100, cache_creation_input_tokens=100, input_tokens=800),
        None,
    )
    # 100 / 1000 = 0.10 → "save:10%"
    assert seg.text == "save:10%"


def test_a_missing_class_is_not_a_zero() -> None:
    seg = token_saving.build(_usage(cache_read_input_tokens=100, input_tokens=800), None)
    assert seg.text == "save:n/a(partial-usage)"


def test_zero_total_names_why() -> None:
    """When every class is zero (no tokens were billed yet), the module
    declines to compute a ratio rather than render ``save:0%`` for an empty
    session."""
    seg = token_saving.build(
        _usage(input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        None,
    )
    assert seg.text == "save:n/a(no-input-tokens)"
