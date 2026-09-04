"""Tests for the daemon over-budget (banded) wave detector.

The detector is SIZE-RELATIVE: each active wave is banded against its own
pessimistic budget (the effort-bucket EU default, or an explicit estimate
row), so an XS wave flags at a far smaller elapsed than an XL wave. The
0.8x ``warn`` and 1.0x ``err`` boundaries are the SAME constants the TUI
effort gauge reads, each band fires once, and a generous absolute backstop
catches an abandoned wave with no projectable budget.
"""

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
    DEFAULT_ABSOLUTE_BACKSTOP_SECONDS,
    build_stale_wave_envelope,
    plan_stale_waves,
    sweep_once,
)
from eawf.surfaces.tui.widgets.eu_bar import OK_THRESHOLD, WARN_THRESHOLD
from eawf.workflow.estimation.buckets import BUCKET_EU, EU_MINUTES
from eawf.workflow.estimation.thresholds import OK_BAND_CEILING, OVER_BUDGET_CEILING
from eawf.workflow.skills.needs_user import (
    AUTO_RESOLVED_CHOICE,
    list_open_pauses,
    retract_wave_pauses,
)

pytestmark = pytest.mark.unit

_WAVE_ID = "P28-I02-W20"


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _bucket_budget_minutes(bucket: str) -> float:
    """Return the pessimistic budget in minutes for an effort *bucket*."""
    from eawf.kernel.state.enums import EffortBucket

    return BUCKET_EU[EffortBucket(bucket)] * EU_MINUTES


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


# ---- band thresholds shared with the gauge ---------------------------------


def test_stale_detector_reuses_gauge_band_thresholds() -> None:
    # The whole point of the rework: gauge and modal read one constant, so
    # the two cannot drift apart.
    assert OK_THRESHOLD is OK_BAND_CEILING
    assert WARN_THRESHOLD is OVER_BUDGET_CEILING
    assert (OK_BAND_CEILING, OVER_BUDGET_CEILING) == (0.80, 1.00)


# ---- size-relativity (the core behaviour) ----------------------------------


def test_plan_stale_waves_xs_warns_at_smaller_elapsed_than_xl() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    xs_budget = _bucket_budget_minutes("XS")  # 0.25 EU * 30 = 7.5 min
    xl_budget = _bucket_budget_minutes("XL")  # 3.5 EU * 30 = 105 min
    # Elapsed just above the XS warn boundary (0.8 * 7.5 = 6 min) but far
    # below the XL warn boundary (0.8 * 105 = 84 min): only the XS flags.
    claimed = _now() - timedelta(minutes=xs_budget * OK_BAND_CEILING + 0.5)

    xs_state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket="XS"))
    xl_state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket="XL"))

    xs_stale = plan_stale_waves(xs_state, events_path=events, now=_now())
    xl_stale = plan_stale_waves(xl_state, events_path=events, now=_now())

    assert [row.advisory_band for row in xs_stale] == ["warn"]
    assert xl_stale == []
    # Sanity on the projection used: XS budget is far smaller than XL.
    assert xs_budget < xl_budget


def test_plan_stale_waves_warns_above_80pct_of_budget() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    budget = _bucket_budget_minutes("M")  # 1.0 EU * 30 = 30 min
    claimed = _now() - timedelta(minutes=budget * OK_BAND_CEILING + 0.5)
    state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket="M"))

    stale = plan_stale_waves(state, events_path=events, now=_now())

    assert [(row.wave_id, row.advisory_band) for row in stale] == [(_WAVE_ID, "warn")]


def test_plan_stale_waves_quiet_at_or_below_80pct_boundary() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    budget = _bucket_budget_minutes("M")
    # Exactly at the 0.8x boundary is still ``ok`` (inclusive upper bound).
    claimed = _now() - timedelta(minutes=budget * OK_BAND_CEILING)
    state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket="M"))

    assert plan_stale_waves(state, events_path=events, now=_now()) == []


def test_plan_stale_waves_errors_above_full_budget() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    budget = _bucket_budget_minutes("M")
    claimed = _now() - timedelta(minutes=budget * OVER_BUDGET_CEILING + 0.5)
    state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket="M"))

    stale = plan_stale_waves(state, events_path=events, now=_now())

    assert [row.advisory_band for row in stale] == ["err"]


def test_plan_stale_waves_prefers_estimate_over_bucket_default() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    # Bucket M defaults to a 30-min budget, but an explicit 600-min estimate
    # must win: 200 min elapsed is over M's budget yet far inside the
    # estimate, so no advisory fires.
    claimed = _now() - timedelta(minutes=200)
    state = State.model_validate(
        _state_payload(
            claimed_at=claimed,
            effort_bucket="M",
            estimate_pessimistic_minutes=600.0,
        )
    )

    assert plan_stale_waves(state, events_path=events, now=_now()) == []


# ---- claimed_at anchoring --------------------------------------------------


def test_plan_stale_waves_skips_unclaimed_wave() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    # No claimed_at -> no work-start clock -> never stale, even though the
    # wave was opened 12h ago (the opened_at in the fixture).
    state = State.model_validate(_state_payload(claimed_at=None, effort_bucket="XS"))

    assert plan_stale_waves(state, events_path=events, now=_now()) == []


def test_plan_stale_waves_skips_inactive_status() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    claimed = _now() - timedelta(hours=4)
    state = State.model_validate(
        _state_payload(claimed_at=claimed, status="closed", effort_bucket="XS")
    )

    assert plan_stale_waves(state, events_path=events, now=_now()) == []


# ---- absolute backstop (abandoned, no projectable budget) ------------------


def test_plan_stale_waves_backstop_flags_abandoned_wave_without_budget() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    # No effort_bucket and no estimate -> no projectable budget. The wave
    # only flags once it crosses the generous absolute backstop.
    claimed = _now() - timedelta(seconds=DEFAULT_ABSOLUTE_BACKSTOP_SECONDS + 60)
    state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket=None))

    stale = plan_stale_waves(state, events_path=events, now=_now())

    assert [row.advisory_band for row in stale] == ["backstop"]
    assert stale[0].budget_minutes is None


def test_plan_stale_waves_no_budget_quiet_before_backstop() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    claimed = _now() - timedelta(seconds=DEFAULT_ABSOLUTE_BACKSTOP_SECONDS - 60)
    state = State.model_validate(_state_payload(claimed_at=claimed, effort_bucket=None))

    assert plan_stale_waves(state, events_path=events, now=_now()) == []


# ---- error path ------------------------------------------------------------


def test_plan_stale_waves_rejects_non_positive_backstop() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    state = State.model_validate(_state_payload(claimed_at=_now() - timedelta(hours=1)))

    with pytest.raises(ValueError, match="absolute_backstop_seconds must be positive"):
        plan_stale_waves(state, events_path=events, absolute_backstop_seconds=0, now=_now())


# ---- envelope shape --------------------------------------------------------


def test_build_stale_wave_envelope_is_needs_user_pause() -> None:
    events = store_path(Path("/nonexistent") / "state.json", StoreKind.EVENT)
    budget = _bucket_budget_minutes("M")
    claimed = _now() - timedelta(minutes=budget * OVER_BUDGET_CEILING + 0.5)
    state = State.model_validate(_state_payload(claimed_at=claimed, status="in_progress"))
    plan = plan_stale_waves(state, events_path=events, now=_now())[0]

    envelope = build_stale_wave_envelope(plan, now=_now())

    assert envelope.scope_id == state.urn
    assert envelope.payload["event_type"] == "needs_user_pause"
    assert envelope.payload["event_kind"] == "stale_wave_detected"
    assert envelope.payload["status"] == "needs_user"
    assert envelope.payload["extras"]["wave_id"] == _WAVE_ID
    assert envelope.payload["extras"]["advisory_band"] == "err"
    assert envelope.payload["message"].startswith("over-budget advisory:")
    assert "user_question" in envelope.payload["extras"]


# ---- sweep: append, publish, preserve state --------------------------------


def test_sweep_once_appends_publishes_and_preserves_state(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    budget = _bucket_budget_minutes("M")
    claimed = _now() - timedelta(minutes=budget * OVER_BUDGET_CEILING + 0.5)
    payload = _state_payload(claimed_at=claimed, status="in_progress")
    _write_state(state_path, payload)
    before = state_path.read_bytes()
    published: list[Envelope] = []

    async def body() -> None:
        plans = await sweep_once(state_path=state_path, publish=published.append, now=_now())
        assert [plan.wave_id for plan in plans] == [_WAVE_ID]

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


# ---- retract-on-close: the advisory clears when the wave closes ------------


def _sweep_one_stale_pause(state_path: Path) -> None:
    """Drive a single err-band advisory into the store for ``_WAVE_ID``."""
    budget = _bucket_budget_minutes("M")
    claimed = _now() - timedelta(minutes=budget * OVER_BUDGET_CEILING + 0.5)
    _write_state(state_path, _state_payload(claimed_at=claimed, status="in_progress"))

    async def body() -> None:
        await sweep_once(state_path=state_path, now=_now())

    _run(body)


def test_list_open_pauses_exposes_subject_wave_id(tmp_path: Path) -> None:
    # The advisory pause carries its subject wave so a surface can drop it
    # once that wave is terminal, and the close path can retract it by wave.
    state_path = tmp_path / ".ea" / "state.json"
    _sweep_one_stale_pause(state_path)

    pauses = list_open_pauses(state_path)

    assert len(pauses) == 1
    assert pauses[0].wave_id == _WAVE_ID


def test_retract_wave_pauses_clears_the_open_advisory(tmp_path: Path) -> None:
    # The operator's bug: a closed wave kept surfacing its over-budget
    # prompt. Retracting on close pairs the open pause with a resume so it
    # stops surfacing.
    state_path = tmp_path / ".ea" / "state.json"
    _sweep_one_stale_pause(state_path)
    assert len(list_open_pauses(state_path)) == 1
    published: list[Envelope] = []

    resolved = retract_wave_pauses(state_path, wave_id=_WAVE_ID, publish=published.append)

    assert len(resolved) == 1
    assert list_open_pauses(state_path) == []
    # A resume envelope was published so live subscribers drop the pause.
    assert len(published) == 1
    assert published[0].payload["extras"]["choice"] == AUTO_RESOLVED_CHOICE


def test_retract_wave_pauses_leaves_other_waves_advisories(tmp_path: Path) -> None:
    # Retraction is keyed on the subject wave: closing one wave must not
    # clear a sibling wave's open advisory.
    state_path = tmp_path / ".ea" / "state.json"
    _sweep_one_stale_pause(state_path)

    resolved = retract_wave_pauses(state_path, wave_id="P28-I02-W99")

    assert resolved == []
    assert len(list_open_pauses(state_path)) == 1


# ---- escalating one-shot per band ------------------------------------------


def test_sweep_once_warn_fires_once_not_repeatedly(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    budget = _bucket_budget_minutes("M")
    claimed = _now() - timedelta(minutes=budget * OK_BAND_CEILING + 0.5)
    _write_state(state_path, _state_payload(claimed_at=claimed, status="in_progress"))

    async def body() -> None:
        first = await sweep_once(state_path=state_path, now=_now())
        assert [row.advisory_band for row in first] == ["warn"]
        # A later tick still inside the warn band must not re-raise warn.
        later = await sweep_once(state_path=state_path, now=_now() + timedelta(minutes=1))
        assert later == []

    _run(body)

    rows = store_path(state_path, StoreKind.EVENT).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1


def test_sweep_once_error_fires_once(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    budget = _bucket_budget_minutes("M")
    claimed = _now() - timedelta(minutes=budget * OVER_BUDGET_CEILING + 0.5)
    _write_state(state_path, _state_payload(claimed_at=claimed, status="in_progress"))

    async def body() -> None:
        first = await sweep_once(state_path=state_path, now=_now())
        assert [row.advisory_band for row in first] == ["err"]
        later = await sweep_once(state_path=state_path, now=_now() + timedelta(minutes=5))
        assert later == []

    _run(body)

    rows = store_path(state_path, StoreKind.EVENT).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1


def test_sweep_once_warn_then_error_escalates_each_once(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    budget = _bucket_budget_minutes("M")  # 30 min
    warn_claimed = _now() - timedelta(minutes=budget * OK_BAND_CEILING + 0.5)
    _write_state(state_path, _state_payload(claimed_at=warn_claimed, status="in_progress"))

    async def body() -> None:
        warn = await sweep_once(state_path=state_path, now=_now())
        assert [row.advisory_band for row in warn] == ["warn"]
        # Push elapsed past the 1.0x boundary: err escalates once.
        err_now = warn_claimed + timedelta(minutes=budget * OVER_BUDGET_CEILING + 1)
        err = await sweep_once(state_path=state_path, now=err_now)
        assert [row.advisory_band for row in err] == ["err"]
        # No third advisory once both bands have fired.
        again = await sweep_once(state_path=state_path, now=err_now + timedelta(minutes=10))
        assert again == []

    _run(body)

    rows = store_path(state_path, StoreKind.EVENT).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 2
    bands = [orjson.loads(r)["payload"]["extras"]["advisory_band"] for r in rows]
    assert bands == ["warn", "err"]
