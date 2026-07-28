"""Contract tests pinning the daemon JSON-RPC request/response schemas.

The wire contract has three pinned layers:

* **Envelope shape** — every request carries ``jsonrpc == "2.0"``, a
  non-empty string ``method``, and an optional object ``params``; every
  response is either ``{jsonrpc, id, result}`` (success) or
  ``{jsonrpc, id, error: {code, message, data?}}`` (error). The
  :func:`~eawf.runtime.daemon.server._parse_frame` validator + the
  :func:`~eawf.runtime.daemon.server._success` / :func:`~eawf.runtime.daemon.server._error`
  builders own this shape.
* **Error-code band** — the JSON-RPC reserved codes (-32700..-32600) plus
  the daemon-specific server-error band (-32000..-32099). The numeric
  values are part of the wire contract because the CLI client maps them
  onto exit codes; a silent renumber would break that mapping.
* **Method param/result schemas** — the typed ``*Params`` / ``*Result``
  Pydantic models for ``daemon.ping`` / ``daemon.status`` /
  ``daemon.shutdown`` carry ``extra="forbid"`` and a frozen field set.

The socket-level integration suite (``tests/daemon/test_scaffolding.py``)
exercises these end to end over a real UDS; this suite pins the schemas
*structurally* so a model/field/error-code drift fails without booting a
server.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import (
    VALIDATION_FAILED,
    DaemonValidationError,
    MethodContext,
    MethodNotFoundError,
    dispatch,
    registered_methods,
)
from eawf.runtime.daemon.methods.daemon import (
    PingParams,
    PingResult,
    ShutdownParams,
    ShutdownResult,
    StatusParams,
    StatusResult,
)
from eawf.runtime.daemon.server import (
    CATCH_UP_TOO_LARGE,
    DAEMON_SHUTTING_DOWN,
    DISPATCH_CLOSE_BLOCKED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    UNAUTHORIZED,
    _error,
    _parse_frame,
    _success,
    process_frame_bytes,
)

pytestmark = pytest.mark.unit


def _build_ctx() -> MethodContext:
    return MethodContext(
        started_at="2026-05-23T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# Error-code band is pinned (wire contract -> CLI exit-code mapping)
# ---------------------------------------------------------------------------


def test_jsonrpc_reserved_error_codes_are_pinned() -> None:
    """The reserved JSON-RPC 2.0 error codes carry their canonical values."""
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert METHOD_NOT_FOUND == -32601
    assert INVALID_PARAMS == -32602
    assert INTERNAL_ERROR == -32603


def test_daemon_server_error_band_is_pinned() -> None:
    """The daemon-specific server-error extensions stay in the -32000 band."""
    assert UNAUTHORIZED == -32000
    assert VALIDATION_FAILED == -32002
    assert CATCH_UP_TOO_LARGE == -32008
    assert DAEMON_SHUTTING_DOWN == -32009
    assert DISPATCH_CLOSE_BLOCKED == -32011


def test_server_error_codes_are_unique() -> None:
    """No two daemon error codes collide on the wire."""
    codes = [
        PARSE_ERROR,
        INVALID_REQUEST,
        METHOD_NOT_FOUND,
        INVALID_PARAMS,
        INTERNAL_ERROR,
        UNAUTHORIZED,
        VALIDATION_FAILED,
        CATCH_UP_TOO_LARGE,
        DAEMON_SHUTTING_DOWN,
        DISPATCH_CLOSE_BLOCKED,
    ]
    assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------
# Response envelope shape (_success / _error)
# ---------------------------------------------------------------------------


def test_success_envelope_shape() -> None:
    """A success envelope carries exactly jsonrpc / id / result."""
    env = _success("req-1", {"ok": True})
    assert env == {"jsonrpc": "2.0", "id": "req-1", "result": {"ok": True}}
    assert set(env) == {"jsonrpc", "id", "result"}


def test_error_envelope_shape_without_data() -> None:
    """An error envelope omits ``error.data`` when none is supplied."""
    env = _error("req-1", INVALID_PARAMS, "bad params")
    assert env == {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {"code": INVALID_PARAMS, "message": "bad params"},
    }
    assert "data" not in env["error"]


def test_error_envelope_carries_data_when_supplied() -> None:
    """The ``error.data`` forensic block is attached when supplied."""
    env = _error("req-1", UNAUTHORIZED, "unauthorized", data={"platform": "linux"})
    assert env["error"]["data"] == {"platform": "linux"}


def test_error_envelope_echoes_null_id_on_unparseable_request() -> None:
    """When the request id is unknown the envelope echoes ``id == None``."""
    env = _error(None, PARSE_ERROR, "parse error")
    assert env["id"] is None


# ---------------------------------------------------------------------------
# Request-frame validation (_parse_frame) — the request half of the contract
# ---------------------------------------------------------------------------


def test_parse_frame_accepts_well_formed_request() -> None:
    """A well-formed frame returns ``(payload, None)``."""
    line = orjson.dumps({"jsonrpc": "2.0", "id": "x", "method": "daemon.ping", "params": {}})
    payload, error = _parse_frame(line)
    assert error is None
    assert payload is not None
    assert payload["method"] == "daemon.ping"


def test_parse_frame_defaults_missing_params_to_present() -> None:
    """A frame omitting ``params`` is accepted (params default to ``{}``)."""
    line = orjson.dumps({"jsonrpc": "2.0", "id": "x", "method": "daemon.ping"})
    payload, error = _parse_frame(line)
    assert error is None
    assert payload is not None


def test_parse_frame_rejects_non_json() -> None:
    """Malformed JSON yields a PARSE_ERROR envelope."""
    payload, error = _parse_frame(b"{not json")
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == PARSE_ERROR


def test_parse_frame_rejects_non_object_top_level() -> None:
    """A JSON array (not object) at the top level is INVALID_REQUEST."""
    payload, error = _parse_frame(orjson.dumps([1, 2, 3]))
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_REQUEST


def test_parse_frame_rejects_missing_jsonrpc_version() -> None:
    """Absent ``jsonrpc`` field is INVALID_REQUEST and echoes the id."""
    payload, error = _parse_frame(orjson.dumps({"id": "x", "method": "daemon.ping"}))
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_REQUEST
    assert error["id"] == "x"


def test_parse_frame_rejects_wrong_jsonrpc_version() -> None:
    """A non-2.0 ``jsonrpc`` value is INVALID_REQUEST."""
    payload, error = _parse_frame(
        orjson.dumps({"jsonrpc": "1.0", "id": "x", "method": "daemon.ping"})
    )
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_REQUEST


def test_parse_frame_rejects_missing_method() -> None:
    """Absent ``method`` is INVALID_REQUEST."""
    payload, error = _parse_frame(orjson.dumps({"jsonrpc": "2.0", "id": "x"}))
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_REQUEST


def test_parse_frame_rejects_empty_method() -> None:
    """An empty-string ``method`` is INVALID_REQUEST (non-empty contract)."""
    payload, error = _parse_frame(orjson.dumps({"jsonrpc": "2.0", "id": "x", "method": ""}))
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_REQUEST


def test_parse_frame_rejects_non_string_method() -> None:
    """A non-string ``method`` is INVALID_REQUEST."""
    payload, error = _parse_frame(orjson.dumps({"jsonrpc": "2.0", "id": "x", "method": 7}))
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_REQUEST


def test_parse_frame_rejects_non_object_params() -> None:
    """A ``params`` array is INVALID_PARAMS and echoes the id."""
    payload, error = _parse_frame(
        orjson.dumps({"jsonrpc": "2.0", "id": "x", "method": "daemon.ping", "params": [1]})
    )
    assert payload is None
    assert error is not None
    assert error["error"]["code"] == INVALID_PARAMS
    assert error["id"] == "x"


# ---------------------------------------------------------------------------
# process_frame_bytes round-trip — end to end through the byte dispatcher
# ---------------------------------------------------------------------------


def test_process_frame_bytes_returns_newline_terminated_success() -> None:
    """A valid ping frame round-trips to a newline-terminated success frame."""
    ctx = _build_ctx()
    req = orjson.dumps({"jsonrpc": "2.0", "id": "p1", "method": "daemon.ping", "params": {}})

    async def body() -> dict[str, Any]:
        raw = await process_frame_bytes(req, ctx)
        assert raw.endswith(b"\n")
        return orjson.loads(raw)

    response = asyncio.run(body())
    assert response["id"] == "p1"
    assert "error" not in response
    result = response["result"]
    assert result["pid"] == os.getpid()
    assert result["protocol_version"] == PROTOCOL_VERSION
    assert result["version"] == __version__


def test_process_frame_bytes_unknown_method_maps_to_method_not_found() -> None:
    """An unknown method routes through the byte dispatcher to -32601."""
    ctx = _build_ctx()
    req = orjson.dumps({"jsonrpc": "2.0", "id": "u1", "method": "daemon.does_not_exist"})

    async def body() -> dict[str, Any]:
        return orjson.loads(await process_frame_bytes(req, ctx))

    response = asyncio.run(body())
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_process_frame_bytes_extra_param_maps_to_invalid_params() -> None:
    """An unknown param field maps to -32602 (ping params forbid extras)."""
    ctx = _build_ctx()
    req = orjson.dumps(
        {"jsonrpc": "2.0", "id": "e1", "method": "daemon.ping", "params": {"nope": 1}}
    )

    async def body() -> dict[str, Any]:
        return orjson.loads(await process_frame_bytes(req, ctx))

    response = asyncio.run(body())
    assert response["error"]["code"] == INVALID_PARAMS


def test_process_frame_bytes_close_blocked_maps_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``DispatchCloseBlockedError`` maps to the typed -32011, not -32603.

    A FAIL / BLOCKED report verdict is a legitimate agent outcome (the report is
    already persisted; only the close-path advance is refused). The P30-I21 live
    codex e2e showed it surfacing as a generic -32603 internal error, which hid
    the real cause. The typed code carries the verdict + reasons in
    ``error.data`` so the client can distinguish it from a real server fault.
    """
    from eawf.kernel.state.enums import AgentReportVerdict
    from eawf.workflow.verify.dispatch_close import (
        DispatchCloseBlockedError,
        VerifyResult,
    )

    ctx = _build_ctx()

    async def _raise(method: str, ctx_: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        raise DispatchCloseBlockedError(
            wave_id="P30-I21-W05",
            result=VerifyResult(
                passed=False,
                verdict=AgentReportVerdict.BLOCKED,
                reasons=("verdict=blocked not in close-ready set",),
            ),
        )

    monkeypatch.setattr("eawf.runtime.daemon.server.dispatch", _raise)
    req = orjson.dumps({"jsonrpc": "2.0", "id": "b1", "method": "agent.dispatch", "params": {}})

    async def body() -> dict[str, Any]:
        return orjson.loads(await process_frame_bytes(req, ctx))

    response = asyncio.run(body())
    assert response["error"]["code"] == DISPATCH_CLOSE_BLOCKED
    assert response["error"]["code"] != INTERNAL_ERROR
    data = response["error"]["data"]
    assert data["wave_id"] == "P30-I21-W05"
    assert data["verdict"] == "blocked"
    assert data["reasons"] == ["verdict=blocked not in close-ready set"]


# ---------------------------------------------------------------------------
# Method dispatch contract (registry + error-class -> wire-code mapping)
# ---------------------------------------------------------------------------


def test_registered_methods_includes_daemon_namespace() -> None:
    """The daemon control surface is registered under its canonical names."""
    methods = registered_methods()
    assert "daemon.ping" in methods
    assert "daemon.status" in methods
    assert "daemon.shutdown" in methods


def test_dispatch_unknown_method_raises_method_not_found() -> None:
    """Dispatching an unknown method raises :class:`MethodNotFoundError`."""
    ctx = _build_ctx()

    async def body() -> None:
        with pytest.raises(MethodNotFoundError):
            await dispatch("daemon.no_such_method", ctx, {})

    asyncio.run(body())


def test_method_not_found_subclasses_key_error() -> None:
    """``MethodNotFoundError`` is a ``KeyError`` subclass (server maps -32601)."""
    assert issubclass(MethodNotFoundError, KeyError)


def test_daemon_validation_error_subclasses_value_error() -> None:
    """``DaemonValidationError`` is a ``ValueError`` subclass.

    The server's ordered ``except`` clauses catch the subclass first to
    emit -32002 instead of the generic -32602; the subclass relationship
    is part of that contract.
    """
    assert issubclass(DaemonValidationError, ValueError)


def test_protocol_version_is_pinned_string() -> None:
    """The wire protocol version is the pinned string ``"2"``."""
    assert PROTOCOL_VERSION == "2"
    assert isinstance(PROTOCOL_VERSION, str)


# ---------------------------------------------------------------------------
# Typed param/result schemas — daemon.ping
# ---------------------------------------------------------------------------


def test_ping_params_forbid_extra() -> None:
    """``PingParams`` rejects any field (empty by contract)."""
    assert PingParams.model_validate({}) is not None
    with pytest.raises(ValidationError):
        PingParams.model_validate({"unexpected": 1})


def test_ping_result_field_set_is_pinned() -> None:
    """``PingResult`` carries exactly the documented wire fields."""
    assert set(PingResult.model_fields) == {
        "pid",
        "version",
        "protocol_version",
        "started_at",
        "uptime_seconds",
    }


def test_ping_result_forbids_extra_field() -> None:
    """``PingResult`` rejects an out-of-contract field."""
    valid = {
        "pid": 1,
        "version": "0.0.0",
        "protocol_version": "1",
        "started_at": "t",
        "uptime_seconds": 0.0,
    }
    assert PingResult.model_validate(valid) is not None
    with pytest.raises(ValidationError):
        PingResult.model_validate({**valid, "extra": True})


def test_ping_result_rejects_wrong_type() -> None:
    """``PingResult.pid`` must be an int (type pinned)."""
    with pytest.raises(ValidationError):
        PingResult.model_validate(
            {
                "pid": "not-an-int",
                "version": "0.0.0",
                "protocol_version": "1",
                "started_at": "t",
                "uptime_seconds": 0.0,
            }
        )


# ---------------------------------------------------------------------------
# Typed param/result schemas — daemon.status
# ---------------------------------------------------------------------------


def test_status_params_forbid_extra() -> None:
    """``StatusParams`` rejects any field (empty by contract)."""
    assert StatusParams.model_validate({}) is not None
    with pytest.raises(ValidationError):
        StatusParams.model_validate({"unexpected": 1})


def test_status_result_field_set_is_pinned() -> None:
    """``StatusResult`` carries the ping fields plus the three counters."""
    assert set(StatusResult.model_fields) == {
        "pid",
        "version",
        "protocol_version",
        "started_at",
        "uptime_seconds",
        "active_subscriptions",
        "in_flight_mutations",
        "last_event_id",
        # P30-I23-W10 — per-mutation telemetry rows (kind, age) for the
        # self-deadlock watchdog surface.
        "in_flight",
    }


def test_status_result_forbids_extra_field() -> None:
    """``StatusResult`` rejects an out-of-contract field."""
    valid = {
        "pid": 1,
        "version": "0.0.0",
        "protocol_version": "1",
        "started_at": "t",
        "uptime_seconds": 0.0,
        "active_subscriptions": 0,
        "in_flight_mutations": 0,
        "last_event_id": "",
    }
    assert StatusResult.model_validate(valid) is not None
    with pytest.raises(ValidationError):
        StatusResult.model_validate({**valid, "extra": True})


# ---------------------------------------------------------------------------
# Typed param/result schemas — daemon.shutdown
# ---------------------------------------------------------------------------


def test_shutdown_params_defaults_are_pinned() -> None:
    """``ShutdownParams`` defaults to ``drain=True`` / ``timeout_seconds=30``."""
    params = ShutdownParams.model_validate({})
    assert params.drain is True
    assert params.timeout_seconds == 30


def test_shutdown_params_forbid_extra() -> None:
    """``ShutdownParams`` rejects an out-of-contract field."""
    with pytest.raises(ValidationError):
        ShutdownParams.model_validate({"unexpected": 1})


def test_shutdown_params_timeout_lower_bound() -> None:
    """``timeout_seconds`` is bounded at ``>= 0`` (negative rejected)."""
    with pytest.raises(ValidationError):
        ShutdownParams.model_validate({"timeout_seconds": -1})


def test_shutdown_params_timeout_upper_bound() -> None:
    """``timeout_seconds`` is bounded at ``<= 600`` (over-cap rejected)."""
    with pytest.raises(ValidationError):
        ShutdownParams.model_validate({"timeout_seconds": 601})


def test_shutdown_params_timeout_accepts_bounds() -> None:
    """The inclusive bounds 0 and 600 are accepted."""
    assert ShutdownParams.model_validate({"timeout_seconds": 0}).timeout_seconds == 0
    assert ShutdownParams.model_validate({"timeout_seconds": 600}).timeout_seconds == 600


def test_shutdown_result_field_set_is_pinned() -> None:
    """``ShutdownResult`` carries exactly ``shutdown_at`` + ``drained``."""
    assert set(ShutdownResult.model_fields) == {"shutdown_at", "drained"}


# ---------------------------------------------------------------------------
# Method handlers serialise to JSON-mode dicts (wire-ready output)
# ---------------------------------------------------------------------------


def test_ping_handler_returns_json_serialisable_dict() -> None:
    """``daemon.ping`` returns a dict that orjson can serialise verbatim."""
    ctx = _build_ctx()

    async def body() -> dict[str, Any]:
        return await dispatch("daemon.ping", ctx, {})

    result = asyncio.run(body())
    # Round-trips through orjson without raising — proves wire-readiness.
    reparsed = orjson.loads(orjson.dumps(result))
    assert reparsed == result
    assert PingResult.model_validate(result) is not None
