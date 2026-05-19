"""Tests for the ``agent.*`` JSON-RPC handlers (P24-W07).

Covers the fresh-dispatch path of :func:`eawf.daemon.methods.agent.dispatch`
plus the inspection helpers :func:`agent.session` and the placeholder
:func:`agent.kill`. W07 ships only the fresh path; the suite asserts that
``session_policy="continue"`` is rejected with ``-32602 invalid params``
(via :class:`ValueError`, which the server maps to that code).

The handlers are driven directly through the module-level coroutines
— the JSON-RPC framing is exercised in :mod:`tests.daemon.test_scaffolding`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.methods import MethodContext
from eawf.daemon.methods.agent import dispatch, kill, session
from eawf.state.enums import DispatchNote
from eawf.state.models import SessionAttempt, Wave

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _build_ctx(*, state_path: Path | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        state_path=state_path,
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def _build_state_payload(
    *,
    wave_id: str,
    sessions: dict[int, SessionAttempt] | None = None,
    runtime_preference: list[str] | None = None,
) -> dict[str, object]:
    """Construct a minimal valid State payload for fixtures."""
    sessions = sessions or {}
    wave = Wave.model_validate(
        {
            "id": wave_id,
            "iter_id": "P24-I01",
            "title": "agent-dispatch-test",
            "status": "in_progress",
            "opened_at": _now().isoformat(),
            "sessions": {str(k): v.model_dump(mode="json") for k, v in sessions.items()},
            "runtime_preference": runtime_preference,
        }
    )
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {wave_id: wave.model_dump(mode="json")},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload))


def test_dispatch_fresh_path_returns_attempt_one_with_uuid_session(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
        )
        assert result["attempt"] == 1
        assert result["runtime"] == "claude-code"
        # session_id is a UUID4 — round-trip parse.
        uuid.UUID(result["session_id"])
        assert result["annotation"]["note"] == DispatchNote.FRESH_DISPATCH.value
        assert result["annotation"]["attempt"] == 1
        assert result["session_attempt"]["attempt"] == 1
        assert result["session_attempt"]["runtime"] == "claude-code"
        assert result["session_attempt"]["session_log_handle"].startswith(
            "urn:eawf:v1:session-log:claude-code:"
        )

    _run(body)


def test_dispatch_uses_runtime_override_over_preference(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "runtime": "codex", "session_policy": "fresh"},
        )
        assert result["runtime"] == "codex"

    _run(body)


def test_dispatch_increments_attempt_when_wave_has_prior_sessions(
    tmp_path: Path,
) -> None:
    """A wave with one prior attempt yields attempt=2 on the next dispatch."""
    state_path = tmp_path / "state.json"
    prior = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="prior-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: prior},
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
        )
        assert result["attempt"] == 2

    _run(body)


def test_dispatch_rejects_session_policy_continue(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="not implemented in W07"):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "session_policy": "continue"},
            )

    _run(body)


def test_dispatch_rejects_unknown_wave(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown wave"):
            await dispatch(
                ctx,
                {"wave_id": "P99-I01-W99", "session_policy": "fresh"},
            )

    _run(body)


def test_dispatch_rejects_extra_params(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "rogue": True},
            )

    _run(body)


def test_dispatch_fails_without_runtime_when_preference_missing(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=None),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="no runtime resolved"):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
            )

    _run(body)


def test_session_returns_typed_sessions_table(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempt_1 = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="abc-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    attempt_2 = SessionAttempt(
        attempt=2,
        runtime="codex",
        session_id="def-uuid",
        session_log_handle="urn:eawf:v1:session-log:codex:def",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: attempt_1, 2: attempt_2},
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await session(ctx, {"wave_id": "P24-I01-W07"})
        sessions_dict = result["sessions"]
        keys = sorted(int(k) for k in sessions_dict)
        assert keys == [1, 2]

    _run(body)


def test_session_returns_single_attempt_when_attempt_supplied(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    attempt_1 = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="abc-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    attempt_2 = SessionAttempt(
        attempt=2,
        runtime="codex",
        session_id="def-uuid",
        session_log_handle="urn:eawf:v1:session-log:codex:def",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: attempt_1, 2: attempt_2},
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await session(ctx, {"wave_id": "P24-I01-W07", "attempt": 2})
        keys = [int(k) for k in result["sessions"]]
        assert keys == [2]

    _run(body)


def test_session_returns_empty_when_attempt_missing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", sessions={}),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await session(ctx, {"wave_id": "P24-I01-W07", "attempt": 99})
        assert result["sessions"] == {}

    _run(body)


def test_session_rejects_unknown_wave(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07"),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown wave"):
            await session(ctx, {"wave_id": "P99-I01-W99"})

    _run(body)


def test_session_raises_runtime_error_without_state_path() -> None:
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await session(ctx, {"wave_id": "P24-I01-W07"})

    _run(body)


def test_kill_returns_placeholder_false() -> None:
    ctx = _build_ctx()

    async def body() -> None:
        result: dict[str, Any] = await kill(
            ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "term"}
        )
        assert result == {"killed": False, "signal": "term"}

    _run(body)


def test_kill_defaults_signal_to_term() -> None:
    ctx = _build_ctx()

    async def body() -> None:
        result: dict[str, Any] = await kill(ctx, {"wave_id": "P24-I01-W07", "attempt": 1})
        assert result == {"killed": False, "signal": "term"}

    _run(body)


def test_kill_accepts_signal_kill_value() -> None:
    ctx = _build_ctx()

    async def body() -> None:
        result: dict[str, Any] = await kill(
            ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "kill"}
        )
        assert result["signal"] == "kill"

    _run(body)


def test_kill_rejects_unknown_signal() -> None:
    ctx = _build_ctx()

    async def body() -> None:
        with pytest.raises(ValidationError):
            await kill(ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "hup"})

    _run(body)
