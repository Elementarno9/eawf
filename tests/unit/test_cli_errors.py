"""Tests for :mod:`eawf.cli.errors` — CliError taxonomy + envelope emission.

C05 § 5.3 compresses the legacy nine-class taxonomy into five buckets:
``UserError``, ``ValidationError``, ``StateConflict``,
``DaemonUnreachable``, ``InternalError``. Each maps 1:1 onto the new
0..5 exit-code surface. The legacy class names (``NotFound``,
``LockConflict``, ...) are gone; callers raise the canonical bucket with
the fine-grained cause carried in the ``kind=`` constructor kwarg, which
folds into ``ErrorEnvelope.data.kind`` per § 5.3 disambiguation.

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


# --- Legacy cause tags now ride on the bucket's ``kind=`` kwarg ------------


@pytest.mark.parametrize(
    ("bucket", "kind"),
    [
        (errors.UserError, "NotFound"),
        (errors.UserError, "InvalidInput"),
        (errors.UserError, "InstrumentMissing"),
        (errors.UserError, "UserDeclined"),
        (errors.ValidationError, None),
        (errors.StateConflict, "LockConflict"),
        (errors.StateConflict, "IntegrityViolation"),
        (errors.StateConflict, "HookBlocked"),
    ],
)
def test_legacy_cause_rides_on_bucket_kind(
    bucket: type[errors.CliError],
    kind: str | None,
) -> None:
    """The migrated form ``Bucket(msg, kind=<legacy>)`` carries the right
    exit code and threads the legacy cause through ``.kind`` (``None`` for
    ``ValidationError`` whose legacy ``ValidationFailed`` bucket coincides)."""
    err = bucket("legacy", kind=kind)
    assert isinstance(err, errors.CliError)
    assert err.exit_code == bucket.exit_code
    assert err.kind == kind


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
    err = errors.UserError("scope/.ea missing", kind="NotFound")
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
    err = errors.StateConflict("sibling lock held", kind="LockConflict")
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


def test_emit_error_for_legacy_kinds_fold_to_buckets() -> None:
    """The envelope reports the bucket name; the threaded ``kind`` lives in data.kind."""
    flags = GlobalFlags(json_output=True)
    for bucket, kind, bucket_name, code in (
        (errors.UserError, "InstrumentMissing", "UserError", exit_codes.USER_ERROR),
        (errors.UserError, "UserDeclined", "UserError", exit_codes.USER_ERROR),
        (errors.StateConflict, "IntegrityViolation", "StateConflict", exit_codes.STATE_CONFLICT),
        (errors.StateConflict, "HookBlocked", "StateConflict", exit_codes.STATE_CONFLICT),
    ):
        err = bucket("msg", kind=kind)
        app = _make_emitting_app(err, flags)
        result = runner.invoke(app, [])
        assert result.exit_code == code, kind
        body = json.loads(result.stdout)
        assert body["error"] == bucket_name
        assert body["exit_code"] == code
        assert body["data"]["kind"] == kind


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
    err = errors.StateConflict("held", kind="LockConflict")
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
    assert err.kind is None


@pytest.mark.parametrize(
    ("rpc_code", "expected_kind"),
    [
        (-32001, "LockConflict"),
        (-32005, "RuntimeUnavailable"),
        (-32006, "RuntimeUnavailable"),
        (-32003, "NotFound"),
        (-32004, "ProtocolMismatch"),
        (-32600, "InvalidInput"),
    ],
)
def test_cli_error_for_rpc_threads_kind(rpc_code: int, expected_kind: str) -> None:
    """The RPC table's fine-grained kind tag rides on the returned error."""
    err = errors.cli_error_for_rpc(rpc_code, "rpc body")
    assert err.kind == expected_kind


@pytest.mark.parametrize(
    "rpc_code",
    [-32700, -32603, -32000, -32002, -32007, -32008, -32009],
)
def test_cli_error_for_rpc_no_kind_codes_have_none(rpc_code: int) -> None:
    """RPC codes mapped to a bare bucket carry no kind tag."""
    err = errors.cli_error_for_rpc(rpc_code, "rpc body")
    assert err.kind is None


def test_rpc_threaded_kind_survives_into_envelope() -> None:
    """A StateConflict built from -32005 surfaces ``RuntimeUnavailable`` in the envelope.

    Because the five buckets coincide with the exit-code surface, the only
    way per-cause specificity survives is the threaded ``kind`` tag — it
    must land in ``data.kind`` and drive the kind-specific hint.
    """
    err = errors.cli_error_for_rpc(-32005, "runtime ladder exhausted")
    env = errors.build_envelope(err)
    assert env.error == "StateConflict"
    assert env.data["kind"] == "RuntimeUnavailable"
    assert env.suggested_next_step == errors._KIND_HINTS["RuntimeUnavailable"]


def test_rpc_threaded_kind_in_json_emit() -> None:
    """End-to-end: the threaded kind reaches the emitted JSON envelope."""
    flags = GlobalFlags(json_output=True)
    err = errors.cli_error_for_rpc(-32001, "sibling lock held")
    app = _make_emitting_app(err, flags)
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.STATE_CONFLICT
    body = json.loads(result.stdout)
    assert body["error"] == "StateConflict"
    assert body["data"]["kind"] == "LockConflict"


def test_explicit_data_kind_overrides_threaded_kind() -> None:
    """An explicit ``data.kind`` from the caller still wins (explicit over implicit)."""
    err = errors.cli_error_for_rpc(-32005, "runtime ladder exhausted")
    env = errors.build_envelope(err, data={"kind": "OperatorOverride"})
    assert env.data["kind"] == "OperatorOverride"


def test_clierror_kind_defaults_to_none() -> None:
    """A plainly-raised CliError carries no kind tag unless one is threaded."""
    assert errors.CliError("boom").kind is None
    assert errors.StateConflict("boom", kind="LockConflict").kind == "LockConflict"
