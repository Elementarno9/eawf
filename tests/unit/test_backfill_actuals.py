"""Retroactive ActualSummary backfill for historical closed waves (P27-I02-W26).

W25 made wave-close auto-record an :class:`~eawf.kernel.state.models.ActualSummary`
going forward; these tests cover the migration that backfills the actuals
for the waves that closed before that wiring landed.
:func:`~eawf.kernel.migrations.backfill_actuals.backfill_actuals` is a pure,
idempotent transform over a typed :class:`~eawf.kernel.state.models.State`, so the
suite asserts: one actual per eligible closed wave, an idempotent re-run
(count 0, no duplicate / no mutation), the boundary cases (missing
timestamps, open wave), and the end-to-end claim that ``metrics variance``
and ``calibrate buckets`` fit from real samples after the backfill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.estimation.buckets import calibrate_buckets
from eawf.estimation.metrics import (
    compute_estimate_actual_variance,
    compute_eu_variance,
)
from eawf.kernel.migrations.backfill_actuals import backfill_actuals
from eawf.kernel.state.enums import (
    Confidence,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    EstimateSummary,
    Project,
    State,
    Wave,
)


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _wave(
    *,
    wave_id: str,
    status: WaveStatus,
    opened_at: datetime | None,
    closed_at: datetime | None,
    effort_bucket: EffortBucket | None = EffortBucket.M,
) -> Wave:
    """Build a single :class:`Wave` directly (no lifecycle ceremony).

    ``opened_at`` is non-nullable on the model, so a closed-without-opened
    boundary wave is constructed with a placeholder ``opened_at`` and then
    has the field cleared in-memory (mirroring the W25 close-path suite).
    """
    seed_opened = opened_at if opened_at is not None else datetime.now(UTC)
    wave = Wave(
        id=wave_id,
        iter_id="P01-I01",
        title="w",
        status=status,
        file_scopes=["src/"],
        effort_bucket=effort_bucket,
        opened_at=seed_opened,
        closed_at=closed_at,
    )
    if opened_at is None:
        wave.opened_at = None  # type: ignore[assignment]
    return wave


def _state_with_closed_waves(*, now: datetime, count: int = 3) -> State:
    """State with *count* CLOSED waves carrying timestamps but no actuals.

    Each wave's span is ``(index + 1) * 30`` minutes so the derived elapsed
    EU is a distinct positive value, and ``closed_at`` is anchored just
    before *now* so the actuals land inside the calibration window.
    """
    state = _empty_state()
    for index in range(count):
        wave_id = f"P01-I01-W{index + 1:02d}"
        closed_at = now - timedelta(minutes=5)
        opened_at = closed_at - timedelta(minutes=(index + 1) * 30)
        state.waves[wave_id] = _wave(
            wave_id=wave_id,
            status=WaveStatus.CLOSED,
            opened_at=opened_at,
            closed_at=closed_at,
        )
    return state


# ---- happy path: one actual per eligible closed wave ------------------------


def test_backfill_actuals_attaches_one_actual_per_closed_wave() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _state_with_closed_waves(now=now, count=3)
    assert state.actuals is None

    result, added = backfill_actuals(state)

    assert added == 3
    assert result is state
    assert result.actuals is not None
    assert set(result.actuals) == {"P01-I01-W01", "P01-I01-W02", "P01-I01-W03"}
    # Each actual is anchored to its wave's own closed_at, not "now()".
    for wave_id, act in result.actuals.items():
        assert act.scope_id == wave_id
        assert act.id == f"ACT-{wave_id}"
        assert act.elapsed_eu > 0.0
        assert act.updated_at == state.waves[wave_id].closed_at
    # W01 span = 30m -> 1.0 EU; W03 span = 90m -> 3.0 EU.
    assert result.actuals["P01-I01-W01"].elapsed_eu == pytest.approx(1.0)
    assert result.actuals["P01-I01-W03"].elapsed_eu == pytest.approx(3.0)


# ---- idempotence: a re-run adds nothing and alters nothing ------------------


def test_backfill_actuals_idempotent_second_run_adds_zero() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _state_with_closed_waves(now=now, count=3)
    state, first = backfill_actuals(state)
    assert state.actuals is not None
    snapshot = {wave_id: act.model_copy(deep=True) for wave_id, act in state.actuals.items()}

    state, second = backfill_actuals(state)

    assert first == 3
    assert second == 0
    assert state.actuals is not None
    assert len(state.actuals) == 3
    # The existing actuals are byte-for-byte unchanged (no double-write).
    for wave_id, act in state.actuals.items():
        assert act == snapshot[wave_id]


def test_backfill_actuals_does_not_overwrite_existing_actual() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _state_with_closed_waves(now=now, count=2)
    # Pre-seed a hand-authored actual for W01 with a sentinel elapsed_eu so a
    # double-write would be visible.
    state, _ = backfill_actuals(state)
    assert state.actuals is not None
    sentinel = state.actuals["P01-I01-W01"].model_copy(update={"elapsed_eu": 99.0})
    state.actuals["P01-I01-W01"] = sentinel

    state, added = backfill_actuals(state)

    assert added == 0
    assert state.actuals is not None
    # The guarded wave keeps its sentinel value; nothing was clobbered.
    assert state.actuals["P01-I01-W01"].elapsed_eu == pytest.approx(99.0)


# ---- boundary / error paths -------------------------------------------------


def test_backfill_actuals_skips_closed_wave_missing_opened_at() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _empty_state()
    state.waves["P01-I01-W01"] = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        opened_at=None,
        closed_at=now - timedelta(minutes=5),
    )

    state, added = backfill_actuals(state)

    assert added == 0
    assert not (state.actuals or {})


def test_backfill_actuals_skips_closed_wave_missing_closed_at() -> None:
    state = _empty_state()
    # A CLOSED wave with no closed_at (anomalous) derives no actual.
    state.waves["P01-I01-W01"] = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        opened_at=datetime(2026, 5, 23, 11, 0, 0, tzinfo=UTC),
        closed_at=None,
    )

    state, added = backfill_actuals(state)

    assert added == 0
    assert not (state.actuals or {})


def test_backfill_actuals_skips_closed_wave_with_non_positive_span() -> None:
    state = _empty_state()
    opened = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state.waves["P01-I01-W01"] = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        opened_at=opened,
        # closed before opened -> non-positive span -> no actual.
        closed_at=opened - timedelta(minutes=5),
    )

    state, added = backfill_actuals(state)

    assert added == 0
    assert not (state.actuals or {})


def test_backfill_actuals_skips_open_wave() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _empty_state()
    # An IN_PROGRESS wave with a full timestamp pair is still not backfilled
    # — only CLOSED waves contribute.
    state.waves["P01-I01-W01"] = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.IN_PROGRESS,
        opened_at=now - timedelta(minutes=60),
        closed_at=now,
    )

    state, added = backfill_actuals(state)

    assert added == 0
    assert not (state.actuals or {})


def test_backfill_actuals_empty_state_is_noop() -> None:
    state = _empty_state()

    state, added = backfill_actuals(state)

    assert added == 0
    # No closed waves -> the actuals dict is left untouched (stays None).
    assert state.actuals is None


# ---- end-to-end: metrics + calibration fit from backfilled samples ----------


def test_backfill_actuals_feeds_calibrate_buckets() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _state_with_closed_waves(now=now, count=3)
    # Before backfill there are no actuals, so every bucket reports no fit.
    before = calibrate_buckets(state, now=now)
    assert all(row.fitted_eu is None for row in before.buckets)

    state, _ = backfill_actuals(state)

    after = calibrate_buckets(state, now=now)
    m_row = next(row for row in after.buckets if row.bucket == EffortBucket.M)
    assert m_row.sample_count == 3
    assert m_row.fitted_eu is not None


def test_backfill_actuals_feeds_metrics_variance() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    state = _state_with_closed_waves(now=now, count=3)
    # The variance metric also needs estimates; seed one per closed wave so
    # the post-backfill metric has a complete estimate+actual pair to fit.
    estimates: dict[str, EstimateSummary] = {}
    for wave_id in state.waves:
        estimates[wave_id] = EstimateSummary(
            id=f"EST-{wave_id}",
            scope_id=wave_id,
            expected_eu=1.0,
            pessimistic_eu=3.6,
            expected_minutes=30.0,
            pessimistic_minutes=108.0,
            display="1.0 EU",
            reference_class="bucket:M",
            confidence=Confidence.LOW,
            current_store_record_id=f"EST-{wave_id}-seed",
            updated_at=now,
        )
    state.estimates = estimates
    # Before backfill: estimates exist but no actuals -> empty sample.
    assert compute_eu_variance(state).sample_count == 0

    state, _ = backfill_actuals(state)

    eu_var = compute_eu_variance(state)
    m26 = compute_estimate_actual_variance(state)
    assert eu_var.sample_count == 3
    assert m26.sample_count == 3
    assert m26.planned_eu == pytest.approx(3.0)
    assert m26.actual_eu > 0.0
    assert m26.variance_pct is not None
