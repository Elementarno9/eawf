"""Tests for :mod:`eawf.cli.errors` — CliError taxonomy + envelope emission.

Each non-zero canonical exit code in :mod:`eawf.cli.exit_codes` has a
corresponding :class:`eawf.cli.errors.CliError` subclass. :func:`emit_error`
prints the canonical envelope and raises :class:`typer.Exit` with the right
code. JSON envelope shape is:

    {
        "error": "<class name>",
        "message": "<str(err)>",
        "exit_code": <int>,
        "exit_name": "<canonical name>",
    }
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from eawf.cli import errors, exit_codes
from eawf.cli.flags import GlobalFlags

runner = CliRunner()


def test_clierror_default_exit_code_is_generic() -> None:
    err = errors.CliError("something")
    assert err.exit_code == exit_codes.GENERIC_ERROR


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (errors.NotFound, exit_codes.NOT_FOUND),
        (errors.InvalidInput, exit_codes.INVALID_INPUT),
        (errors.ValidationFailed, exit_codes.VALIDATION_FAILED),
        (errors.LockConflict, exit_codes.LOCK_CONFLICT),
        (errors.InstrumentMissing, exit_codes.INSTRUMENT_MISSING),
        (errors.UserDeclined, exit_codes.USER_DECLINED),
        (errors.IntegrityViolation, exit_codes.INTEGRITY_VIOLATION),
        (errors.HookBlocked, exit_codes.HOOK_BLOCKED),
    ],
)
def test_each_error_subclass_carries_its_exit_code(cls: type[errors.CliError], code: int) -> None:
    err = cls("boom")
    assert err.exit_code == code
    assert isinstance(err, errors.CliError)
    assert isinstance(err, Exception)


def _make_emitting_app(err: errors.CliError, flags: GlobalFlags) -> typer.Typer:
    app = typer.Typer(no_args_is_help=False)

    @app.command()
    def go() -> None:
        errors.emit_error(err, flags=flags)

    return app


def test_emit_error_text_envelope_and_exit_code() -> None:
    flags = GlobalFlags(json_output=False)
    err = errors.NotFound("scope/.ea missing")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.NOT_FOUND
    assert "error: scope/.ea missing" in result.stdout


def test_emit_error_json_envelope_shape() -> None:
    flags = GlobalFlags(json_output=True)
    err = errors.LockConflict("sibling lock held")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.LOCK_CONFLICT
    body = json.loads(result.stdout)
    assert body == {
        "error": "LockConflict",
        "message": "sibling lock held",
        "exit_code": exit_codes.LOCK_CONFLICT,
        "exit_name": "LOCK_CONFLICT",
    }


def test_emit_error_for_validation_failed_uses_canonical_name() -> None:
    flags = GlobalFlags(json_output=True)
    err = errors.ValidationFailed("bad payload")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.VALIDATION_FAILED
    body = json.loads(result.stdout)
    assert body["exit_name"] == "VALIDATION_FAILED"
    assert body["error"] == "ValidationFailed"


def test_emit_error_for_clierror_base_uses_generic_code() -> None:
    flags = GlobalFlags(json_output=True)
    err = errors.CliError("uncategorised")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.GENERIC_ERROR
    body = json.loads(result.stdout)
    assert body["exit_code"] == exit_codes.GENERIC_ERROR
    assert body["exit_name"] == "GENERIC_ERROR"
    assert body["error"] == "CliError"


def test_emit_error_for_instrument_missing_and_user_declined() -> None:
    """The envelope is consistent across all non-zero codes."""
    flags = GlobalFlags(json_output=True)
    for cls, name, code in (
        (errors.InstrumentMissing, "INSTRUMENT_MISSING", exit_codes.INSTRUMENT_MISSING),
        (errors.UserDeclined, "USER_DECLINED", exit_codes.USER_DECLINED),
        (errors.IntegrityViolation, "INTEGRITY_VIOLATION", exit_codes.INTEGRITY_VIOLATION),
        (errors.HookBlocked, "HOOK_BLOCKED", exit_codes.HOOK_BLOCKED),
    ):
        err = cls("msg")
        app = _make_emitting_app(err, flags)
        result = runner.invoke(app, [])
        assert result.exit_code == code, name
        body = json.loads(result.stdout)
        assert body["exit_name"] == name
        assert body["exit_code"] == code
