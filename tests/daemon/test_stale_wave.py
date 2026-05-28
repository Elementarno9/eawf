"""Tests for the daemon stale-wave detector."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.stale_wave import (
    DEFAULT_STALE_WINDOW_SECONDS,
    build_stale_wave_envelope,
    plan_stale_waves,
    sweep_once,
)
from eawf.workflow.skills.needs_user import list_open_pauses

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _state_payload(
    *,
    opened_at: datetime,
    status: str = "claimed",
    wave_id: str = "P28-I02-W20",
) -> dict[str, Any]:
    phase_id = "P28"
    iter_id = "P28-I02"
    return {
        "schema_version": "1.0",
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
            "active_wave_ids": [wave_id],
        },
        "workspace": None,
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "ABC",
                "subproject_id": None,
                "title": "P28",
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
                "title": "I02",
                "status": "active",
                "wave_ids": [wave_id],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            wave_id: {
                "id": wave_id,
                "iter_id": iter_id,
                "title": "stale detector",
                "status": status,
                "claim_session_id": "SES-test",
                "opened_at": opened_at.isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def test_plan_stale_waves_detects_15_minute_window(tmp_path: Path) -> None:
    state = State.model_validate(
        _state_payload(opened_at=_now() - timedelta(seconds=DEFAULT_STALE_WINDOW_SECONDS))
    )
    events_path = store_path(tmp_path / "state.json", StoreKind.EVENT)

    stale = plan_stale_waves(state, events_path=events_path, now=_now())

    assert [row.wave_id for row in stale] == ["P28-I02-W20"]


def test_plan_stale_waves_ignores_wave_before_window(tmp_path: Path) -> None:
    state = State.model_validate(
        _state_payload(opened_at=_now() - timedelta(seconds=DEFAULT_STALE_WINDOW_SECONDS - 1))
    )
    events_path = store_path(tmp_path / "state.json", StoreKind.EVENT)

    assert plan_stale_waves(state, events_path=events_path, now=_now()) == []


def test_build_stale_wave_envelope_is_needs_user_pause(tmp_path: Path) -> None:
    state = State.model_validate(
        _state_payload(opened_at=_now() - timedelta(minutes=20), status="in_progress")
    )
    events_path = store_path(tmp_path / "state.json", StoreKind.EVENT)
    plan = plan_stale_waves(state, events_path=events_path, now=_now())[0]

    envelope = build_stale_wave_envelope(plan, now=_now())

    assert envelope.scope_id == state.urn
    assert envelope.payload["event_type"] == "needs_user_pause"
    assert envelope.payload["event_kind"] == "stale_wave_detected"
    assert envelope.payload["status"] == "needs_user"
    assert envelope.payload["extras"]["wave_id"] == "P28-I02-W20"
    assert envelope.payload["message"].startswith("stale-wave advisory:")
    assert "user_question" in envelope.payload["extras"]


def test_sweep_once_appends_publishes_and_preserves_state(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    payload = _state_payload(opened_at=_now() - timedelta(minutes=20), status="in_progress")
    _write_state(state_path, payload)
    before = state_path.read_bytes()
    published: list[Envelope] = []

    async def body() -> None:
        plans = await sweep_once(state_path=state_path, publish=published.append, now=_now())
        assert [plan.wave_id for plan in plans] == ["P28-I02-W20"]

    _run(body)

    assert state_path.read_bytes() == before
    events_path = store_path(state_path, StoreKind.EVENT)
    rows = events_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    on_disk = orjson.loads(rows[0])
    assert on_disk["payload"]["event_kind"] == "stale_wave_detected"
    assert len(published) == 1
    assert published[0].id == on_disk["id"]
    pauses = list_open_pauses(state_path, scope_id="urn:eawf:v1:state:ABC")
    assert len(pauses) == 1
    assert pauses[0].question.options[0].label == "keep"


def test_sweep_once_does_not_duplicate_detected_wave(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(opened_at=_now() - timedelta(minutes=20)))

    async def body() -> None:
        assert len(await sweep_once(state_path=state_path, now=_now())) == 1
        assert await sweep_once(state_path=state_path, now=_now()) == []

    _run(body)

    rows = store_path(state_path, StoreKind.EVENT).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
