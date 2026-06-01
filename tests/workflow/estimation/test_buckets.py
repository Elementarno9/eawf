"""Bucket-derived estimate selection tests for P28-I02-W14."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.kernel.state.enums import (
    ActualStatus,
    Confidence,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    ActualSummary,
    CurrentPointers,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.workflow.estimation.buckets import (
    BUCKET_EU,
    actual_eu_for_iter,
    actual_eu_for_phase,
    actual_eu_for_wave,
    calibrate_buckets,
    default_estimate_summary,
    resolve_wave_actual,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _empty_state() -> State:
    """Return a minimal valid state for calibration tests."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
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
    effort_bucket: EffortBucket = EffortBucket.M,
    status: WaveStatus = WaveStatus.PENDING,
) -> Wave:
    """Return a wave with the fields bucket estimation reads."""
    iter_id = "-".join(wave_id.split("-")[:2])
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"wave {wave_id}",
        status=status,
        deps=[],
        blocks=[],
        file_scopes=[],
        success_criteria=[],
        effort_bucket=effort_bucket,
        opened_at=_T0,
        closed_at=_T0 if status == WaveStatus.CLOSED else None,
    )


def _actual(*, wave_id: str, elapsed_eu: float, updated_at: datetime = _T0) -> ActualSummary:
    """Return a completed actual row for *wave_id*."""
    return ActualSummary(
        id=f"ACT-{wave_id}",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=updated_at,
    )


def _state_with_samples(
    samples: tuple[float, ...],
    *,
    bucket: EffortBucket = EffortBucket.M,
) -> State:
    """Return state with CLOSED waves whose actuals match *samples*."""
    state = _empty_state()
    actuals: dict[str, ActualSummary] = {}
    for index, elapsed in enumerate(samples, start=1):
        wave_id = f"P01-I01-W{index:02d}"
        state.waves[wave_id] = _wave(
            wave_id=wave_id,
            effort_bucket=bucket,
            status=WaveStatus.CLOSED,
        )
        actuals[wave_id] = _actual(
            wave_id=wave_id,
            elapsed_eu=elapsed,
            updated_at=_T0 - timedelta(minutes=index),
        )
    state.actuals = actuals
    return state


def test_calibrate_buckets_populates_fitted_pessimistic_eu() -> None:
    """Fitted rows carry the p90 pessimistic EU alongside the mean."""
    state = _state_with_samples((1.0, 2.0, 3.0, 4.0, 5.0))

    report = calibrate_buckets(state, now=_T0)
    row = next(row for row in report.buckets if row.bucket == EffortBucket.M)

    assert row.fitted_eu == pytest.approx(3.0)
    assert row.fitted_pessimistic_eu == pytest.approx(5.0)


def test_default_estimate_summary_prefers_config_override() -> None:
    """Explicit config override wins over fitted samples."""
    state = _state_with_samples((1.0, 1.1, 1.2, 1.3, 1.4))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)
    config = {
        "estimation": {
            "buckets": {
                "overrides": {
                    "M": {
                        "expected_eu": 2.75,
                        "pessimistic_eu": 7.5,
                    }
                }
            }
        }
    }

    estimate = default_estimate_summary(wave, now=_T0, state=state, config=config)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(2.75)
    assert estimate.pessimistic_eu == pytest.approx(7.5)
    assert estimate.confidence == Confidence.HIGH
    assert "bucket-config" in estimate.current_store_record_id


def test_default_estimate_summary_uses_fitted_eu_at_n_min() -> None:
    """Five in-window samples trigger fitted expected EU with MEDIUM confidence."""
    state = _state_with_samples((0.8, 1.0, 1.2, 1.4, 1.6))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)

    estimate = default_estimate_summary(wave, now=_T0, state=state)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(1.2)
    assert estimate.pessimistic_eu == pytest.approx(1.6)
    assert estimate.confidence == Confidence.MEDIUM
    assert "bucket-fitted" in estimate.current_store_record_id


def test_default_estimate_summary_high_confidence_at_30_samples() -> None:
    """Thirty samples promote fitted estimates to HIGH confidence."""
    state = _state_with_samples(tuple(1.5 for _ in range(30)))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)

    estimate = default_estimate_summary(wave, now=_T0, state=state)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(1.5)
    assert estimate.confidence == Confidence.HIGH


def test_default_estimate_summary_falls_back_below_n_min() -> None:
    """Fewer than five samples keep the static bucket fallback."""
    state = _state_with_samples((1.5, 1.5, 1.5, 1.5))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)

    estimate = default_estimate_summary(wave, now=_T0, state=state)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(BUCKET_EU[EffortBucket.M])
    assert estimate.confidence == Confidence.LOW
    assert "bucket-fitted" not in estimate.current_store_record_id


def test_bucket_eu_is_canonical_table() -> None:
    """BUCKET_EU stays pinned to the code-canonical XS..XL calibration.

    A silent edit to the centroid table shifts every estimate and the drift
    calibration baseline, so the canonical values are pinned here: XS=0.25,
    S=0.5, M=1.0, L=2.0, XL=3.5 (1 EU ~= 30 min). The keys also stay exactly
    the five closed buckets — no bucket added or dropped without updating this
    pin.
    """
    assert {
        EffortBucket.XS: pytest.approx(0.25),
        EffortBucket.S: pytest.approx(0.5),
        EffortBucket.M: pytest.approx(1.0),
        EffortBucket.L: pytest.approx(2.0),
        EffortBucket.XL: pytest.approx(3.5),
    } == BUCKET_EU
    assert set(BUCKET_EU) == set(EffortBucket)


def _iter(*, iter_id: str, phase_id: str) -> Iter:
    """Return a minimal CLOSED iter under *phase_id*."""
    return Iter(
        id=iter_id,
        phase_id=phase_id,
        title=f"iter {iter_id}",
        status=IterStatus.CLOSED,
        opened_at=_T0,
        closed_at=_T0,
    )


def _phase(*, phase_id: str) -> Phase:
    """Return a minimal CLOSED phase."""
    return Phase(
        id=phase_id,
        scope_id="urn:eawf:v1:state:QR",
        title=f"phase {phase_id}",
        status=PhaseStatus.CLOSED,
        opened_at=_T0,
        closed_at=_T0,
    )


def _state_with_hierarchy() -> State:
    """Return state with one phase, two iters, and bucketed CLOSED waves.

    Layout::

        P01
          I01 -> W01 (elapsed 1.5), W02 (elapsed 2.5)
          I02 -> W01 (elapsed 0.5)

    plus an empty phase ``P02`` and an empty iter ``P01-I03`` so the
    no-data aggregation paths have a target.
    """
    state = _empty_state()
    state.phases = {"P01": _phase(phase_id="P01"), "P02": _phase(phase_id="P02")}
    state.iters = {
        "P01-I01": _iter(iter_id="P01-I01", phase_id="P01"),
        "P01-I02": _iter(iter_id="P01-I02", phase_id="P01"),
        "P01-I03": _iter(iter_id="P01-I03", phase_id="P01"),
    }
    waves = {
        "P01-I01-W01": _wave(wave_id="P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave(wave_id="P01-I01-W02", status=WaveStatus.CLOSED),
        "P01-I02-W01": _wave(wave_id="P01-I02-W01", status=WaveStatus.CLOSED),
    }
    state.waves = waves
    state.actuals = {
        "P01-I01-W01": _actual(wave_id="P01-I01-W01", elapsed_eu=1.5),
        "P01-I01-W02": _actual(wave_id="P01-I01-W02", elapsed_eu=2.5),
        "P01-I02-W01": _actual(wave_id="P01-I02-W01", elapsed_eu=0.5),
    }
    return state


def test_actual_eu_for_wave_reads_elapsed_eu() -> None:
    """The per-wave accessor returns the resolved actual's elapsed EU."""
    state = _state_with_hierarchy()

    assert actual_eu_for_wave(state, "P01-I01-W02") == pytest.approx(2.5)


def test_actual_eu_for_wave_zero_when_no_actual() -> None:
    """A wave with no actual contributes 0.0 (no realized effort yet)."""
    state = _state_with_hierarchy()

    assert actual_eu_for_wave(state, "P01-I03-W01") == pytest.approx(0.0)


def test_actual_eu_for_wave_zero_when_actuals_none() -> None:
    """The accessor treats a None ``state.actuals`` as the empty case."""
    state = _empty_state()
    state.actuals = None

    assert actual_eu_for_wave(state, "P01-I01-W01") == pytest.approx(0.0)


def test_actual_eu_for_wave_resolves_via_scope_id_fallback() -> None:
    """An actual keyed under a non-wave-id dict key resolves by ``scope_id``."""
    state = _empty_state()
    state.waves = {"P01-I01-W01": _wave(wave_id="P01-I01-W01", status=WaveStatus.CLOSED)}
    # Stored under a non-wave-id dict key; only ``scope_id`` points at the wave.
    state.actuals = {"REC-7": _actual(wave_id="P01-I01-W01", elapsed_eu=1.25)}

    assert actual_eu_for_wave(state, "P01-I01-W01") == pytest.approx(1.25)


def test_resolve_wave_actual_prefers_dict_key_over_scope_scan() -> None:
    """The dict-key match wins over a ``scope_id`` collision elsewhere."""
    state = _empty_state()
    direct = _actual(wave_id="P01-I01-W01", elapsed_eu=3.0)
    shadow = ActualSummary(
        id="ACT-shadow",
        scope_id="P01-I01-W01",
        status=ActualStatus.DONE,
        elapsed_eu=9.0,
        current_store_record_id="REC-shadow",
        updated_at=_T0,
    )
    # Both rows claim the wave: one by dict key, one by scope_id only.
    state.actuals = {"P01-I01-W01": direct, "REC-shadow": shadow}

    resolved = resolve_wave_actual(state, "P01-I01-W01")

    assert resolved is direct
    assert actual_eu_for_wave(state, "P01-I01-W01") == pytest.approx(3.0)


def test_resolve_wave_actual_none_when_unresolved() -> None:
    """An unresolved wave id returns ``None`` (existence signal preserved)."""
    state = _state_with_hierarchy()

    assert resolve_wave_actual(state, "P01-I09-W09") is None


def test_actual_eu_for_iter_sums_waves() -> None:
    """The iter accessor sums realized EU across the iter's waves."""
    state = _state_with_hierarchy()

    assert actual_eu_for_iter(state, "P01-I01") == pytest.approx(4.0)
    assert actual_eu_for_iter(state, "P01-I02") == pytest.approx(0.5)


def test_actual_eu_for_iter_zero_for_empty_iter() -> None:
    """An iter with no waves returns 0.0."""
    state = _state_with_hierarchy()

    assert actual_eu_for_iter(state, "P01-I03") == pytest.approx(0.0)
    assert actual_eu_for_iter(state, "P99-I99") == pytest.approx(0.0)


def test_actual_eu_for_phase_sums_across_iters() -> None:
    """The phase accessor sums realized EU across every wave under its iters."""
    state = _state_with_hierarchy()

    # 1.5 + 2.5 (I01) + 0.5 (I02) == 4.5 across P01.
    assert actual_eu_for_phase(state, "P01") == pytest.approx(4.5)


def test_actual_eu_for_phase_zero_for_empty_phase() -> None:
    """A phase with no waves returns 0.0."""
    state = _state_with_hierarchy()

    assert actual_eu_for_phase(state, "P02") == pytest.approx(0.0)
    assert actual_eu_for_phase(state, "P99") == pytest.approx(0.0)


def test_actual_eu_per_bucket_matches_calibration_centroid() -> None:
    """Each bucket's single-wave realized EU reads back its BUCKET_EU centroid.

    Builds one CLOSED wave per bucket whose actual elapsed EU equals the
    canonical centroid, then asserts the per-wave accessor returns exactly
    that centroid — pinning the accessor against every bucket's EU.
    """
    state = _empty_state()
    waves: dict[str, Wave] = {}
    actuals: dict[str, ActualSummary] = {}
    for index, bucket in enumerate(EffortBucket, start=1):
        wave_id = f"P01-I01-W{index:02d}"
        waves[wave_id] = _wave(wave_id=wave_id, effort_bucket=bucket, status=WaveStatus.CLOSED)
        actuals[wave_id] = _actual(wave_id=wave_id, elapsed_eu=BUCKET_EU[bucket])
    state.waves = waves
    state.actuals = actuals

    for index, bucket in enumerate(EffortBucket, start=1):
        wave_id = f"P01-I01-W{index:02d}"
        assert actual_eu_for_wave(state, wave_id) == pytest.approx(BUCKET_EU[bucket])
