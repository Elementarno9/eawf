"""Tests for the ``context_tokens`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from eawf.runtimes.claude.statusline_modules import context_tokens


def test_token_usage_sub_object_renders_in_out() -> None:
    seg = context_tokens.build(
        {"token_usage": {"input_tokens": 1234, "output_tokens": 56}},
        None,
    )
    assert seg.module == "context_tokens"
    assert seg.text == "ctx:1234/56"
    assert seg.status == "ok"


def test_usage_sub_object_renders_in_out() -> None:
    seg = context_tokens.build({"usage": {"input_tokens": 10, "output_tokens": 5}}, None)
    assert seg.text == "ctx:10/5"


def test_flat_keys_render_in_out() -> None:
    seg = context_tokens.build({"input_tokens": 100, "output_tokens": 200}, None)
    assert seg.text == "ctx:100/200"


def test_missing_payload_returns_dash() -> None:
    seg = context_tokens.build({}, None)
    assert seg.text == "ctx:-"
    assert seg.status == "missing"


def test_negative_or_wrong_type_falls_through_to_dash() -> None:
    seg = context_tokens.build({"input_tokens": -1, "output_tokens": 5}, None)
    assert seg.text == "ctx:-"
    seg = context_tokens.build({"input_tokens": "1234", "output_tokens": 5}, None)
    assert seg.text == "ctx:-"
