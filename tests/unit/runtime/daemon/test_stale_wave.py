"""Tests for the daemon's estimate-crossing producer.

Each active wave is measured against its own estimate (an explicit
estimate row, else its effort bucket's default, else the absolute
backstop). Short of the estimate nothing is written; at it the crossing is
recorded once per claim as activity; and only after the no-progress grace
does it open one budget notice. Nothing it writes is a pause.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.economics.notice_policy import DEFAULT_NOTICE_POLICY
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.daemon.stale_wave import (
    ESTIMATE_PASSED_EVENT_TYPE,
    NOTICE_OPENED_EVENT_TYPE,
    EstimateCrossing,
    plan_estimate_crossings,
    run_sweep_loop,
    sweep_once,
)
from eawf.workflow.estimation.buckets import EFFORT_DISPERSION_MINUTES
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import (
    AUTO_RESOLVED_CHOICE,
    PAUSE_EVENT_TYPE,
    list_open_pauses,
    retract_wave_pauses,
)

pytestmark = pytest.mark.unit

_WAVE_ID = "P28-I02-W20"
_POLICY = DEFAULT_NOTICE_POLICY.estimated_time
_GRACE = timedelta(seconds=_POLICY.require_no_progress_for)


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


#: The estimate of a wave with no estimate row, whatever its size label.
_BUDGET = EFFORT_DISPERSION_MINUTES["p90"]


def _state_payload(
    *,
    claimed_at: datetime | None,
    status: str = "claimed",
    wave_id: str = _WAVE_ID,
    effort_bucket: str | None = "M",
    estimate_pessimistic_minutes: float | None = None,
) -> dict[str, Any]:
    phase_id = "P28"
    iter_id = "P28-I02"
    wave: dict[str, Any] = {
        "id": wave_id,
        "iter_id": iter_id,
        "title": "stale detector",
        "status": status,
        "claim_session_id": "SES-test",
        "effort_bucket": effort_bucket,
        "opened_at": (_now() - timedelta(hours=12)).isoformat(),
        "claimed_at": claimed_at.isoformat() if claimed_at is not None else None,
        "sessions": {},
    }
    payload: dict[str, Any] = {
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
                "track_id": None,
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
        "waves": {wave_id: wave},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    if estimate_pessimistic_minutes is not None:
        payload["estimates"] = {
            wave_id: {
                "id": f"EST-{wave_id}",
                "scope_id": wave_id,
                "expected_eu": 1.0,
                "pessimistic_eu": 2.0,
                "expected_minutes": estimate_pessimistic_minutes / 2.0,
                "pessimistic_minutes": estimate_pessimistic_minutes,
                "display": "test",
                "reference_class": None,
                "confidence": "medium",
                "current_store_record_id": "EST-REC-001",
                "updated_at": _now().isoformat(),
            }
        }
    return payload


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def _state(payload: dict[str, Any]) -> State:
    return State.model_validate(payload)


def _claimed_at(fraction: float) -> datetime:
    """Return the claim stamp that puts the wave at *fraction* of its estimate now."""
    return _now() - timedelta(minutes=_BUDGET * fraction)


def _plan(
    payload: dict[str, Any], events: list[tuple[str | None, EventPayload]] | None = None
) -> list[EstimateCrossing]:
    return plan_estimate_crossings(_state(payload), events=events or [], policy=_POLICY, now=_now())


def _event(scope_id: str, event_type: str, at: datetime) -> tuple[str | None, EventPayload]:
    return scope_id, EventPayload(
        timestamp=at,
        event_type=event_type,
        actor="agent",
        command="x",
        args_hash="0" * 16,
        status="ok",
        message="m",
    )


def _event_types(state_path: Path) -> list[str]:
    path = store_path(state_path, StoreKind.EVENT)
    if not path.exists():
        return []
    return [orjson.loads(r)["payload"]["event_type"] for r in path.read_bytes().splitlines()]


# ---- when an estimate counts as passed -------------------------------------


@pytest.mark.parametrize("fraction", [0.5, 0.8, 0.999])
def test_auth_036_plan_is_quiet_short_of_the_estimate(fraction: float) -> None:
    """The former 0.8x warning fraction, and anything short of 1.0x, is a prediction."""
    assert _plan(_state_payload(claimed_at=_claimed_at(fraction))) == []


@pytest.mark.parametrize("fraction", [1.0, 1.001, 3.0])
def test_plan_crosses_at_and_past_the_estimate(fraction: float) -> None:
    (crossing,) = _plan(_state_payload(claimed_at=_claimed_at(fraction)))

    assert crossing.wave_id == _WAVE_ID
    assert crossing.estimate_seconds == pytest.approx(_BUDGET * 60)


@pytest.mark.parametrize("label", ["XS", "XL", None])
def test_auth_041_no_size_label_moves_the_estimate(label: str | None) -> None:
    """An XS, an XL and an unlabelled wave share the p90 of the effort constant."""
    inside = _plan(_state_payload(claimed_at=_claimed_at(0.99), effort_bucket=label))
    (past,) = _plan(_state_payload(claimed_at=_claimed_at(1.01), effort_bucket=label))

    assert inside == []
    assert past.estimate_seconds == pytest.approx(_BUDGET * 60)


def test_plan_prefers_the_estimate_row_over_the_constant_default() -> None:
    payload = _state_payload(
        claimed_at=_now() - timedelta(minutes=20), estimate_pessimistic_minutes=15.0
    )

    (crossing,) = _plan(payload)

    assert crossing.estimate_seconds == pytest.approx(15.0 * 60)


def test_plan_measures_each_wave_against_its_own_estimate_row() -> None:
    """A short estimate row crosses where a long one, at the same elapsed, does not."""
    claimed = _now() - timedelta(minutes=20)

    short = _plan(_state_payload(claimed_at=claimed, estimate_pessimistic_minutes=15.0))
    long = _plan(_state_payload(claimed_at=claimed, estimate_pessimistic_minutes=60.0))

    assert [c.wave_id for c in short] == [_WAVE_ID]
    assert long == []


def test_plan_skips_an_unclaimed_wave() -> None:
    assert _plan(_state_payload(claimed_at=None)) == []


@pytest.mark.parametrize("status", ["pending", "closed", "failed", "abandoned"])
def test_plan_skips_an_inactive_wave(status: str) -> None:
    assert _plan(_state_payload(claimed_at=_claimed_at(5.0), status=status)) == []


# ---- progress and the grace -------------------------------------------------


def test_plan_meets_the_grace_when_nothing_was_written_since_the_claim() -> None:
    (crossing,) = _plan(_state_payload(claimed_at=_claimed_at(2.0)))

    assert crossing.last_progress_at == _claimed_at(2.0)
    assert crossing.grace_met


def test_plan_reads_a_lane_event_as_progress() -> None:
    recent = _now() - _GRACE / 2
    events = [_event(f"{_WAVE_ID}::executor", "agent.output.chunk", recent)]

    (crossing,) = _plan(_state_payload(claimed_at=_claimed_at(2.0)), events)

    assert crossing.last_progress_at == recent
    assert not crossing.grace_met


@pytest.mark.parametrize(
    "event_type",
    [ESTIMATE_PASSED_EVENT_TYPE, NOTICE_OPENED_EVENT_TYPE, "wave_elapsed_update"],
)
def test_plan_never_reads_the_daemons_own_events_as_progress(event_type: str) -> None:
    events = [_event(_WAVE_ID, event_type, _now())]

    (crossing,) = _plan(_state_payload(claimed_at=_claimed_at(2.0)), events)

    assert crossing.grace_met


def test_plan_ignores_another_waves_progress() -> None:
    events = [_event("P28-I02-W99::executor", "agent.output.chunk", _now())]

    (crossing,) = _plan(_state_payload(claimed_at=_claimed_at(2.0)), events)

    assert crossing.grace_met


# ---- the sweep --------------------------------------------------------------


def _sweep(state_path: Path, *, now: datetime) -> list[Envelope]:
    published: list[Envelope] = []

    async def body() -> None:
        await sweep_once(state_path=state_path, policy=_POLICY, publish=published.append, now=now)

    _run(body)
    return published


def test_sweep_short_of_the_estimate_writes_nothing(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(0.8)))

    assert _sweep(state_path, now=_now()) == []
    assert _event_types(state_path) == []
    assert not notices_path(state_path).exists()


def test_sweep_records_a_crossing_once_per_claim_and_no_pause(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(1.2), status="in_progress"))
    before = state_path.read_bytes()

    first = _sweep(state_path, now=_now())
    again = _sweep(state_path, now=_now() + timedelta(minutes=1))

    assert first[0].payload["event_type"] == ESTIMATE_PASSED_EVENT_TYPE
    assert first[0].payload["extras"]["claim_ref"] == "SES-test"
    assert again == []
    assert PAUSE_EVENT_TYPE not in _event_types(state_path)
    assert list_open_pauses(state_path) == []
    assert state_path.read_bytes() == before


def test_sweep_records_a_new_claim_as_a_new_crossing(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    payload = _state_payload(claimed_at=_claimed_at(1.2))
    _write_state(state_path, payload)
    _sweep(state_path, now=_now())
    payload["waves"][_WAVE_ID]["claim_session_id"] = "SES-second"
    _write_state(state_path, payload)

    second = _sweep(state_path, now=_now())

    assert second[0].payload["extras"]["claim_ref"] == "SES-second"
    assert _event_types(state_path).count(ESTIMATE_PASSED_EVENT_TYPE) == 2


def test_sweep_opens_one_notice_once_the_grace_is_met(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(2.0)))

    first = _sweep(state_path, now=_now())
    again = _sweep(state_path, now=_now() + timedelta(minutes=5))

    assert [e.payload["event_type"] for e in first] == [
        ESTIMATE_PASSED_EVENT_TYPE,
        NOTICE_OPENED_EVENT_TYPE,
    ]
    assert "no progress was observed" in first[1].payload["message"]
    assert again == []
    (notice,) = load_notice_ledger(notices_path(state_path)).notices.values()
    assert (notice.scope_id, notice.axis, notice.basis) == (_WAVE_ID, "wall_seconds", "estimate")


def test_sweep_over_a_missing_state_returns_nothing(tmp_path: Path) -> None:
    async def body() -> None:
        assert await sweep_once(state_path=tmp_path / "absent.json", policy=_POLICY) == []

    _run(body)


def test_run_sweep_loop_rejects_a_non_positive_interval(tmp_path: Path) -> None:
    async def body() -> None:
        await run_sweep_loop(state_path=tmp_path / "state.json", interval_seconds=0)

    with pytest.raises(ValueError, match="interval_seconds must be positive"):
        _run(body)


def test_run_sweep_loop_imports_legacy_pauses_before_sweeping(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(0.5)))
    _append_legacy_pause(state_path)
    stop = asyncio.Event()
    stop.set()

    async def body() -> None:
        await run_sweep_loop(state_path=state_path, stop_event=stop)

    _run(body)

    (notice,) = load_notice_ledger(notices_path(state_path)).notices.values()
    assert notice.provenance == ("urn:legacy:1",)
    assert list_open_pauses(state_path) == []


# ---- legacy pauses: still listed and retracted until imported ----------------


def _append_legacy_pause(state_path: Path, *, wave_id: str = _WAVE_ID) -> None:
    """Append one over-budget pause row as the retired detector wrote it."""
    question = UserQuestion(
        question="over-budget advisory",
        options=[
            UserQuestionOption(label="keep", description="Keep it active."),
            UserQuestionOption(label="defer", description="Leave it for later."),
        ],
    )
    payload = EventPayload(
        timestamp=_now(),
        event_type=PAUSE_EVENT_TYPE,
        event_kind="stale_wave_detected",
        actor="daemon",
        command="stale_wave.sweep",
        args_hash="1" * 16,
        status="needs_user",
        message=question.question,
        extras={
            "pause_urn": "urn:legacy:1",
            "session": "SES-test",
            "user_question": question.model_dump_json(),
            "wave_id": wave_id,
            "advisory_band": "err",
            "elapsed_minutes": 45.0,
            "budget_minutes": 30.0,
        },
    )
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id="EV-000000000001",
            kind=StoreKind.EVENT,
            scope_id="urn:eawf:v1:state:ABC",
            created_at=_now(),
            updated_at=None,
            summary="stale_wave_detected",
            payload=payload.model_dump(mode="json"),
            blob_refs=[],
            artifact_ids=[],
        ),
    )


def test_list_open_pauses_exposes_subject_wave_id(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(0.5)))
    _append_legacy_pause(state_path)

    pauses = list_open_pauses(state_path)

    assert [p.wave_id for p in pauses] == [_WAVE_ID]


def test_retract_wave_pauses_clears_the_open_advisory(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(0.5)))
    _append_legacy_pause(state_path)
    published: list[Envelope] = []

    resolved = retract_wave_pauses(state_path, wave_id=_WAVE_ID, publish=published.append)

    assert resolved == ["urn:legacy:1"]
    assert list_open_pauses(state_path) == []
    assert published[0].payload["extras"]["choice"] == AUTO_RESOLVED_CHOICE


def test_retract_wave_pauses_leaves_other_waves_advisories(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(claimed_at=_claimed_at(0.5)))
    _append_legacy_pause(state_path)

    assert retract_wave_pauses(state_path, wave_id="P28-I02-W99") == []
    assert len(list_open_pauses(state_path)) == 1
