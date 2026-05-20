"""Tests for :mod:`eawf.cli.errors` — CliError taxonomy + envelope emission.

C05 § 5.3 compresses the legacy nine-class taxonomy into five buckets:
``UserError``, ``ValidationError``, ``StateConflict``,
``DaemonUnreachable``, ``InternalError``. Each maps 1:1 onto the new
0..5 exit-code surface. Legacy class names (``NotFound``, ``LockConflict``,
...) remain importable as deprecation-aliased subclasses of the new
buckets; their legacy specificity is preserved in
``ErrorEnvelope.data.kind`` per § 5.3 disambiguation.

The JSON envelope shape per :class:`ErrorEnvelope` (C05 § 5.4):

    {
        "schema_version": "1.0",
        "error": "<canonical bucket name>",
        "message": "<str(err)>",
        "exit_code": <int>,
        "exit_name": "<canonical name>",
        "suggested_next_step": "<hint>",
        "data": {"kind": "<legacy class name>"},
        "correlation_id": null,
        "protocol_version": null,
        "timestamp": "<iso8601 UTC>",
    }
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import typer
from typer.testing import CliRunner

from eawf.cli import errors, exit_codes
from eawf.cli.flags import GlobalFlags

runner = CliRunner()


# --- New five-class taxonomy -----------------------------------------------


def test_clierror_default_exit_code_is_internal_error() -> None:
    """Per C05 § 5.3 the base CliError default code is INTERNAL_ERROR."""
    err = errors.CliError("something")
    assert err.exit_code == exit_codes.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (errors.UserError, exit_codes.USER_ERROR),
        (errors.ValidationError, exit_codes.VALIDATION_ERROR),
        (errors.StateConflict, exit_codes.STATE_CONFLICT),
        (errors.DaemonUnreachable, exit_codes.DAEMON_UNREACHABLE),
        (errors.InternalError, exit_codes.INTERNAL_ERROR),
    ],
)
def test_each_new_subclass_carries_its_exit_code(cls: type[errors.CliError], code: int) -> None:
    err = cls("boom")
    assert err.exit_code == code
    assert isinstance(err, errors.CliError)
    assert isinstance(err, Exception)


# --- Legacy nine-class deprecation aliases ---------------------------------


@pytest.mark.parametrize(
    ("legacy_cls", "new_bucket"),
    [
        (errors.NotFound, errors.UserError),
        (errors.InvalidInput, errors.UserError),
        (errors.InstrumentMissing, errors.UserError),
        (errors.UserDeclined, errors.UserError),
        (errors.ValidationFailed, errors.ValidationError),
        (errors.LockConflict, errors.StateConflict),
        (errors.IntegrityViolation, errors.StateConflict),
        (errors.HookBlocked, errors.StateConflict),
    ],
)
def test_legacy_subclass_bridges_to_new_bucket(
    legacy_cls: type[errors.CliError],
    new_bucket: type[errors.CliError],
) -> None:
    """Legacy class names subclass their new five-bucket parent."""
    err = legacy_cls("legacy")
    assert isinstance(err, new_bucket)
    assert err.exit_code == new_bucket.exit_code


# --- emit_error / envelope rendering ---------------------------------------


def _make_emitting_app(err: errors.CliError, flags: GlobalFlags) -> typer.Typer:
    app = typer.Typer(no_args_is_help=False)

    @app.command()
    def go() -> None:
        errors.emit_error(err, flags=flags)

    return app


def test_emit_error_text_envelope_and_exit_code() -> None:
    flags = GlobalFlags(json_output=False)
    err = errors.UserError("scope/.ea missing")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "error: scope/.ea missing" in result.stdout
    assert "exit_code: 1 (USER_ERROR)" in result.stdout


def test_emit_error_text_envelope_includes_legacy_kind() -> None:
    """Legacy subclass instance emits ``kind: <LegacyName>`` line."""
    flags = GlobalFlags(json_output=False)
    err = errors.NotFound("scope/.ea missing")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "kind: NotFound" in result.stdout


def test_emit_error_json_envelope_shape() -> None:
    flags = GlobalFlags(json_output=True)
    err = errors.StateConflict("sibling writer holds lock")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.STATE_CONFLICT
    body = json.loads(result.stdout)
    assert body["schema_version"] == "1.0"
    assert body["error"] == "StateConflict"
    assert body["message"] == "sibling writer holds lock"
    assert body["exit_code"] == exit_codes.STATE_CONFLICT
    assert body["exit_name"] == "STATE_CONFLICT"
    assert body["data"] == {}
    assert body["correlation_id"] is None
    assert body["protocol_version"] is None
    # timestamp is ISO-8601; pydantic emits a string when ``mode="json"``.
    datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00"))


def test_emit_error_json_envelope_with_legacy_kind() -> None:
    """Legacy class folds its name into ``data.kind`` automatically."""
    flags = GlobalFlags(json_output=True)
    err = errors.LockConflict("sibling lock held")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.STATE_CONFLICT
    body = json.loads(result.stdout)
    assert body["error"] == "StateConflict"
    assert body["exit_name"] == "STATE_CONFLICT"
    assert body["data"]["kind"] == "LockConflict"


def test_emit_error_for_validation_uses_canonical_name() -> None:
    flags = GlobalFlags(json_output=True)
    err = errors.ValidationError("bad payload")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.VALIDATION_ERROR
    body = json.loads(result.stdout)
    assert body["exit_name"] == "VALIDATION_ERROR"
    assert body["error"] == "ValidationError"


def test_emit_error_for_clierror_base_uses_internal_code() -> None:
    flags = GlobalFlags(json_output=True)
    err = errors.CliError("uncategorised")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.INTERNAL_ERROR
    body = json.loads(result.stdout)
    assert body["exit_code"] == exit_codes.INTERNAL_ERROR
    assert body["exit_name"] == "INTERNAL_ERROR"


def test_emit_error_for_legacy_subclasses_fold_to_buckets() -> None:
    """The envelope reports the new bucket name; legacy name lives in data.kind."""
    flags = GlobalFlags(json_output=True)
    for legacy_cls, bucket_name, code in (
        (errors.InstrumentMissing, "UserError", exit_codes.USER_ERROR),
        (errors.UserDeclined, "UserError", exit_codes.USER_ERROR),
        (errors.IntegrityViolation, "StateConflict", exit_codes.STATE_CONFLICT),
        (errors.HookBlocked, "StateConflict", exit_codes.STATE_CONFLICT),
    ):
        err = legacy_cls("msg")
        app = _make_emitting_app(err, flags)
        result = runner.invoke(app, [])
        assert result.exit_code == code, legacy_cls.__name__
        body = json.loads(result.stdout)
        assert body["error"] == bucket_name
        assert body["exit_code"] == code
        assert body["data"]["kind"] == legacy_cls.__name__


# --- ErrorEnvelope direct model tests --------------------------------------


def test_error_envelope_extra_forbid() -> None:
    """Per project rule 2 + C05 § 5.4, ErrorEnvelope rejects extras."""
    with pytest.raises(ValueError, match=r"[Ee]xtra"):
        errors.ErrorEnvelope(
            error="UserError",
            message="m",
            exit_code=1,
            exit_name="USER_ERROR",
            bogus_field="reject me",  # type: ignore[call-arg]
        )


def test_build_envelope_carries_correlation_and_protocol() -> None:
    err = errors.UserError("upgrade required")
    env = errors.build_envelope(
        err,
        correlation_id="req-42",
        protocol_version="0.3.0",
        data={"kind": "ProtocolMismatch", "cli_version": "0.3.1"},
    )
    assert env.correlation_id == "req-42"
    assert env.protocol_version == "0.3.0"
    assert env.data["kind"] == "ProtocolMismatch"
    assert env.suggested_next_step == errors._KIND_HINTS["ProtocolMismatch"]


def test_build_envelope_legacy_kind_injected_when_data_omits_it() -> None:
    err = errors.LockConflict("held")
    env = errors.build_envelope(err, data={"held_by_pid": 1234})
    assert env.data["kind"] == "LockConflict"
    assert env.data["held_by_pid"] == 1234


def test_build_envelope_uses_bucket_default_hint_when_no_kind() -> None:
    err = errors.DaemonUnreachable("connection refused")
    env = errors.build_envelope(err)
    assert env.suggested_next_step == errors._DEFAULT_HINTS["DaemonUnreachable"]
    # No legacy kind, no data injection.
    assert env.data == {}


# --- Daemon RPC code mapping (C05 § 5.3) -----------------------------------


@pytest.mark.parametrize(
    ("rpc_code", "expected_cls", "expected_code"),
    [
        (-32700, errors.InternalError, exit_codes.INTERNAL_ERROR),
        (-32600, errors.UserError, exit_codes.USER_ERROR),
        (-32601, errors.UserError, exit_codes.USER_ERROR),
        (-32602, errors.UserError, exit_codes.USER_ERROR),
        (-32603, errors.InternalError, exit_codes.INTERNAL_ERROR),
        (-32000, errors.InternalError, exit_codes.INTERNAL_ERROR),
        (-32001, errors.StateConflict, exit_codes.STATE_CONFLICT),
        (-32002, errors.ValidationError, exit_codes.VALIDATION_ERROR),
        (-32003, errors.UserError, exit_codes.USER_ERROR),
        (-32004, errors.UserError, exit_codes.USER_ERROR),
        (-32005, errors.StateConflict, exit_codes.STATE_CONFLICT),
        (-32006, errors.StateConflict, exit_codes.STATE_CONFLICT),
        (-32007, errors.InternalError, exit_codes.INTERNAL_ERROR),
        (-32008, errors.InternalError, exit_codes.INTERNAL_ERROR),
        (-32009, errors.DaemonUnreachable, exit_codes.DAEMON_UNREACHABLE),
    ],
)
def test_cli_error_for_rpc_table(
    rpc_code: int,
    expected_cls: type[errors.CliError],
    expected_code: int,
) -> None:
    err = errors.cli_error_for_rpc(rpc_code, "rpc body")
    assert isinstance(err, expected_cls)
    assert err.exit_code == expected_code
    assert str(err) == "rpc body"


def test_cli_error_for_rpc_unknown_falls_back_to_internal() -> None:
    err = errors.cli_error_for_rpc(-99999, "weird rpc code")
    assert isinstance(err, errors.InternalError)
    assert err.exit_code == exit_codes.INTERNAL_ERROR
