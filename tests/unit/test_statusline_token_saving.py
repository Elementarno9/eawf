"""Tests for the ``token_saving`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from eawf.runtime.runtimes.claude.statusline_modules import token_saving


def test_missing_payload_returns_dash() -> None:
    seg = token_saving.build({}, None)
    assert seg.text == "save:-"
    assert seg.status == "missing"


def test_token_usage_with_cache_read_renders_percent() -> None:
    seg = token_saving.build(
        {
            "token_usage": {
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 0,
                "input_tokens": 200,
            }
        },
        None,
    )
    # 800 / (800 + 0 + 200) = 0.8 → "save:80%"
    assert seg.text == "save:80%"
    assert seg.status == "ok"


def test_usage_subobject_shape_supported() -> None:
    seg = token_saving.build(
        {
            "usage": {
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 100,
                "input_tokens": 800,
            }
        },
        None,
    )
    # 100 / 1000 = 0.10 → "save:10%"
    assert seg.text == "save:10%"


def test_zero_total_falls_through_to_dash() -> None:
    """When all probes return zero (no tokens were billed yet), the module
    declines to compute a ratio rather than render ``save:0%`` for an empty
    session."""
    seg = token_saving.build(
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
        None,
    )
    assert seg.text == "save:-"
