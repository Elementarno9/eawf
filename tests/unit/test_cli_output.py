"""Tests for :func:`eawf.cli.output.emit_json_or_text`.

The helper is the single emission gate every CLI handler routes through. JSON
output must use orjson with ``OPT_INDENT_2 | OPT_SORT_KEYS`` so golden tests
remain stable; text output is delegated to :func:`typer.echo` and the helper
must not mutate it.
"""

from __future__ import annotations

import json

import typer
from typer.testing import CliRunner

from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

runner = CliRunner()


def _make_app(payload: dict[str, object], text: str, flags: GlobalFlags) -> typer.Typer:
    app = typer.Typer(no_args_is_help=False)

    @app.command()
    def emit() -> None:
        emit_json_or_text(payload, text, flags=flags)

    return app


def test_json_branch_emits_pretty_sorted_payload() -> None:
    flags = GlobalFlags(json_output=True)
    payload = {"b": 2, "a": 1, "c": [3, 2, 1]}
    app = _make_app(payload, "ignored-text", flags)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    body = result.stdout
    parsed = json.loads(body)
    assert parsed == payload
    # OPT_SORT_KEYS guarantees keys appear in lexicographic order.
    assert body.index('"a"') < body.index('"b"') < body.index('"c"')
    # OPT_INDENT_2 -> first indent should be exactly two spaces before the
    # first key, on a new line after '{'.
    assert "{\n  " in body


def test_text_branch_emits_text_payload_verbatim() -> None:
    flags = GlobalFlags(json_output=False)
    text = "hello world"
    app = _make_app({"ignored": True}, text, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert result.stdout.rstrip("\n") == text


def test_text_branch_handles_multiline_text() -> None:
    flags = GlobalFlags(json_output=False)
    text = "line one\nline two\nline three"
    app = _make_app({"ignored": True}, text, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "line one" in result.stdout
    assert "line two" in result.stdout
    assert "line three" in result.stdout


def test_json_branch_emits_valid_json_for_empty_payload() -> None:
    flags = GlobalFlags(json_output=True)
    app = _make_app({}, "ignored", flags)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == {}


def test_json_branch_serialises_nested_payload() -> None:
    flags = GlobalFlags(json_output=True)
    payload = {"outer": {"inner": [1, 2, {"x": 9}]}}
    app = _make_app(payload, "irrelevant", flags)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload


def test_plain_flag_does_not_change_emission_branch() -> None:
    """``--plain`` is reserved for Rich-bypass — it must NOT flip JSON to text."""
    flags = GlobalFlags(json_output=True, plain_output=True)
    payload = {"k": "v"}
    app = _make_app(payload, "text-fallback", flags)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload
