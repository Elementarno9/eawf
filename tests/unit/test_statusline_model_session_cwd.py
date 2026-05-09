"""Tests for the ``model_session_cwd`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from eawf.runtimes.claude.statusline_modules import model_session_cwd


def test_string_model_session_id_and_cwd_basename() -> None:
    seg = model_session_cwd.build(
        {"model": "claude-opus-4-7", "session_id": "abcd1234efgh5678", "cwd": "/tmp/work"},
        None,
    )
    assert seg.module == "model_session_cwd"
    # session id truncated to 8 chars per module docstring.
    assert seg.text == "model:claude-opus-4-7 ses:abcd1234 cwd:work"
    assert seg.status == "ok"


def test_dict_model_uses_id_then_name() -> None:
    seg = model_session_cwd.build(
        {"model": {"id": "opus-4"}, "session_id": "S1", "cwd": "/x"},
        None,
    )
    assert seg.text.startswith("model:opus-4 ")

    seg = model_session_cwd.build(
        {"model": {"name": "Opus"}, "session_id": "S2", "cwd": "/y"},
        None,
    )
    assert seg.text.startswith("model:Opus ")

    seg = model_session_cwd.build(
        {"model": {"display_name": "Opus 4.7"}, "session_id": "S3", "cwd": "/z"},
        None,
    )
    assert seg.text.startswith("model:Opus 4.7 ")


def test_empty_payload_renders_dashes_with_missing_status() -> None:
    seg = model_session_cwd.build({}, None)
    assert seg.text == "model:- ses:- cwd:-"
    assert seg.status == "missing"


def test_partial_payload_status_ok() -> None:
    seg = model_session_cwd.build({"model": "haiku"}, None)
    assert seg.text == "model:haiku ses:- cwd:-"
    # At least one populated field flips to ``ok``.
    assert seg.status == "ok"


def test_dict_model_unknown_keys_falls_back_to_dash() -> None:
    seg = model_session_cwd.build({"model": {"vendor": "anthropic"}, "cwd": "/z"}, None)
    # cwd present, model dict has no known keys → model:- ses:- cwd:z
    assert seg.text == "model:- ses:- cwd:z"
    assert seg.status == "ok"


def test_cwd_basename_strips_directories() -> None:
    seg = model_session_cwd.build({"cwd": "/srv/projects/eawf"}, None)
    assert "cwd:eawf" in seg.text
