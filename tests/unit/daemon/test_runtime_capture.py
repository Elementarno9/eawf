"""Tests for the ``runtime.capture`` daemon RPC."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import runtime_capture

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _state_payload(*, active_wave_ids: list[str]) -> dict[str, Any]:
    iter_id = "P30-I05"
    phase_id = "P30"
    waves: dict[str, Any] = {}
    for wave_id in active_wave_ids or ["P30-I05-W04"]:
        waves[wave_id] = {
            "id": wave_id,
            "iter_id": iter_id,
            "title": "capture runtime",
            "status": "claimed",
            "claim_session_id": "session-1",
            "opened_at": _now().isoformat(),
            "claimed_at": _now().isoformat(),
            "sessions": {},
        }
    return {
        "schema_version": "1.9",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {
            "project_code": "ABC",
            "phase_id": phase_id,
            "iter_id": iter_id,
            "active_wave_ids": active_wave_ids,
        },
        "workspace": None,
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "ABC",
                "track_id": None,
                "title": "Runtime capture",
                "status": "active",
                "iter_ids": [iter_id],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "title": "Harness EU",
                "status": "active",
                "wave_ids": list(waves),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _ctx(tmp_path: Path, payload: dict[str, Any]) -> tuple[MethodContext, Path]:
    state_path = tmp_path / "state.json"
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path


def _run(body: Any) -> None:
    asyncio.run(body())


def _capture_params(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "api_duration_ms": 17000,
        "total_duration_ms": 21000,
        "cost_usd": "0.42",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7,
        "session_id": "session-1",
        "captured_at": _now().isoformat(),
    }
    payload.update(overrides)
    return payload


def test_runtime_capture_updates_one_active_wave(tmp_path: Path) -> None:
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        result = await runtime_capture(ctx, _capture_params())

        assert result["active_wave_ids"] == ["P30-I05-W04"]
        assert result["active_count"] == 1
        payload = orjson.loads(state_path.read_bytes())
        latest = payload["waves"]["P30-I05-W04"]["runtime_latest"]
        assert latest["api_duration_ms"] == 17000
        assert latest["cost_usd"] == 0.42
        assert latest["captured_at"] == "2026-06-10T12:00:00Z"

    _run(body)


def test_runtime_capture_updates_two_active_waves_and_logs_ambiguity(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    active_wave_ids = ["P30-I05-W03", "P30-I05-W04"]
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=active_wave_ids))

    async def body() -> None:
        result = await runtime_capture(ctx, _capture_params())

        assert result["active_count"] == 2
        payload = orjson.loads(state_path.read_bytes())
        for wave_id in active_wave_ids:
            assert payload["waves"][wave_id]["runtime_latest"]["api_duration_ms"] == 17000
        assert "active_count=2" in caplog.text

    _run(body)


def test_runtime_capture_threads_harness_and_model_attribution(tmp_path: Path) -> None:
    """W25: the runtime-capture writer threads harness+model onto RuntimeLatest.

    W19 added the parser-stamped ``harness`` + ``model`` attribution to the
    capture params, but the daemon writer dropped them when persisting. With the
    wiring in place the persisted ``runtime_latest`` carries NON-NULL attribution
    so a recorded actual derived from it is calibratable by harness+model.
    """
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        await runtime_capture(
            ctx,
            _capture_params(harness="claude-code", model="claude-opus-4-1"),
        )

        payload = orjson.loads(state_path.read_bytes())
        latest = payload["waves"]["P30-I05-W04"]["runtime_latest"]
        assert latest["harness"] == "claude-code"
        assert latest["model"] == "claude-opus-4-1"

    _run(body)


def test_runtime_capture_attribution_defaults_null_when_absent(tmp_path: Path) -> None:
    """Boundary: a capture with no harness/model persists null attribution.

    The fields stay nullable -- a payload that supplies no attribution does not
    fabricate a value, so the persisted snapshot carries ``None`` for both.
    """
    ctx, state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        await runtime_capture(ctx, _capture_params())

        payload = orjson.loads(state_path.read_bytes())
        latest = payload["waves"]["P30-I05-W04"]["runtime_latest"]
        assert latest["harness"] is None
        assert latest["model"] is None

    _run(body)


def test_runtime_capture_refuses_without_active_waves(tmp_path: Path) -> None:
    ctx, _state_path = _ctx(tmp_path, _state_payload(active_wave_ids=[]))

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="requires active waves"):
            await runtime_capture(ctx, _capture_params())

    _run(body)


def test_runtime_capture_params_forbid_extra_keys(tmp_path: Path) -> None:
    ctx, _state_path = _ctx(tmp_path, _state_payload(active_wave_ids=["P30-I05-W04"]))

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="extra"):
            await runtime_capture(ctx, _capture_params(unexpected=True))

    _run(body)
