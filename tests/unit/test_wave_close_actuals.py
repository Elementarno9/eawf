"""Default estimate on claim + token-only auto-actual on close.

Claiming a wave seeds a default :class:`~eawf.kernel.state.models.EstimateSummary`
from its effort bucket (the estimate side of the learning loop, unchanged
since P27-I05-W28).

Closing a wave upserts an :class:`ActualSummary` carrying the
close-time ``actual_tokens`` tally from :attr:`Wave.tokens_consumed`
plus ``actual_cost_usd=0.0`` (P28-I02-W03: per-model rate table not
yet wired). The auto-created actual leaves ``elapsed_eu=0.0`` — the
open->close wall-clock span is not agent effort (it counts overnight
/ cross-session / other-wave idle, inflating consumed EU by ~10x per
the P27-I05-W28 EU-actual research). Measured elapsed-EU still comes
from manual ``eawf actual start/stop`` segments only, so
``compute_estimate_actual_variance`` continues to read the honest
zero-EU state until a measured actual exists — even though
``state.actuals`` is now populated for the cost / token side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from eawf.kernel.config.schema import EuBasis
from eawf.kernel.state.enums import (
    Confidence,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Project,
    RuntimeBaseline,
    RuntimeLatest,
    State,
)
from eawf.observability.telemetry.join import DEFAULT_TOKENS_PER_EU
from eawf.workflow.estimation.buckets import default_estimate_summary
from eawf.workflow.estimation.metrics import compute_estimate_actual_variance
from eawf.workflow.lifecycle import wave as wave_lifecycle
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.transitions import (
    claim_wave,
    close_wave,
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.workflow.lifecycle.wave import compute_runtime_delta
from tests.conftest import make_intent


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


def _seed_wave(
    state: State,
    *,
    wave_id: str = "P01-I01-W01",
    effort_bucket: EffortBucket | None = EffortBucket.M,
) -> None:
    """Plan a phase -> iter -> single wave for estimate/actual tests."""
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=effort_bucket or EffortBucket.M,
        intent=make_intent(),
    )
    if effort_bucket is None:
        state.waves[wave_id].effort_bucket = None


# ---- default_estimate_summary (pure helper) ---------------------------------


def test_default_estimate_summary_from_bucket_fields() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    wave = state.waves["P01-I01-W01"]
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)

    est = default_estimate_summary(wave, now=now)

    assert est is not None
    assert est.scope_id == "P01-I01-W01"
    assert est.id == "EST-P01-I01-W01"
    # M bucket centroid is 1.0 EU; pessimistic applies the 3.6 ratio.
    assert est.expected_eu == pytest.approx(1.0)
    assert est.pessimistic_eu == pytest.approx(3.6)
    assert est.expected_minutes == pytest.approx(30.0)
    assert est.confidence == Confidence.LOW
    assert est.reference_class == "bucket:M"
    assert est.updated_at == now


def test_default_estimate_summary_no_bucket_returns_none() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=None)
    wave = state.waves["P01-I01-W01"]

    assert default_estimate_summary(wave, now=datetime.now(UTC)) is None


# ---- claim_wave seeds a default estimate ------------------------------------


def test_claim_wave_seeds_default_estimate_from_bucket() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.L)

    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert state.estimates is not None
    est = state.estimates["P01-I01-W01"]
    # L bucket centroid is 2.0 EU.
    assert est.expected_eu == pytest.approx(2.0)
    assert est.scope_id == "P01-I01-W01"


def test_claim_wave_no_bucket_rejects_before_estimate() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=None)

    with pytest.raises(LifecycleError, match="effort_bucket"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    # Rejected claim -> no estimate seeded; the dict stays None/empty.
    assert not (state.estimates or {})


def test_claim_wave_captures_runtime_baseline_from_real_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim reads the session-keyed sidecar and stamps a real baseline.

    Exercises the production read path end-to-end (no monkeypatch of the
    capture helper): a sidecar written for the claim session is read back
    and converted into the stamped baseline.
    """
    from eawf.runtime.runtime_counter_sidecar import (
        RuntimeCounterSidecar,
        sidecar_path_for_statusline_cache,
    )
    from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
    from eawf.runtime.runtimes.claude.statusline import cache_path_for

    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path))
    session_id = "SES-real"
    sidecar = RuntimeCounterSidecar(sidecar_path_for_statusline_cache(cache_path_for(session_id)))
    sidecar.write(
        RuntimeCounters(
            api_duration_ms=100,
            total_duration_ms=125,
            cost_usd=Decimal("0.25"),
            input_tokens=10,
            output_tokens=20,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=7,
        )
    )

    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    wave = claim_wave(state, wave_id="P01-I01-W01", session_id=session_id)

    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.api_duration_ms == 100
    assert wave.runtime_baseline.total_duration_ms == 125
    assert wave.runtime_baseline.cost_usd == pytest.approx(0.25)
    assert wave.runtime_baseline.input_tokens == 10
    assert wave.runtime_baseline.cache_read_input_tokens == 7
    assert wave.runtime_baseline.captured_at is not None


def test_claim_wave_runtime_baseline_none_when_sidecar_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sidecar for the claim session stamps no baseline (honest miss)."""
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path))
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)

    wave = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-absent")

    assert wave.runtime_baseline is None


def test_claim_wave_preserves_runtime_baseline_on_idempotent_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-session re-claim does not re-snapshot runtime counters."""
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    first_baseline = RuntimeBaseline(
        api_duration_ms=100,
        total_duration_ms=125,
        cost_usd=0.25,
        input_tokens=10,
        output_tokens=20,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=7,
        captured_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )
    second_baseline = RuntimeBaseline(
        api_duration_ms=999,
        total_duration_ms=999,
        cost_usd=9.99,
        input_tokens=999,
        output_tokens=999,
        cache_creation_input_tokens=999,
        cache_read_input_tokens=999,
        captured_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
    )
    captures = [first_baseline, second_baseline]
    calls = 0

    def capture(session_id: str) -> RuntimeBaseline:
        nonlocal calls
        calls += 1
        return captures[calls - 1]

    monkeypatch.setattr(wave_lifecycle, "_capture_runtime_baseline", capture)

    first = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    original_claimed_at = first.claimed_at
    again = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert calls == 1
    assert again.claimed_at == original_claimed_at
    assert again.runtime_baseline == first_baseline


# ---- close_wave upserts ActualSummary (P28-I02-W03 token-only auto-actual) ---


def test_close_wave_upserts_actual_with_token_tally() -> None:
    """close_wave creates ActualSummary carrying Wave.tokens_consumed."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].tokens_consumed = 4242
    # A positive open->close span historically fabricated a wall-clock
    # actual; the token-only auto-actual leaves elapsed_eu at 0.0.
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=60)

    wave = close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert wave.status == WaveStatus.CLOSED
    assert state.actuals is not None
    actual = state.actuals["P01-I01-W01"]
    assert actual.scope_id == "P01-I01-W01"
    assert actual.actual_tokens == 4242
    # Cost stays at 0.0 until the per-model rate table lands.
    assert actual.actual_cost_usd == pytest.approx(0.0)
    # Token-only auto-actual leaves elapsed_eu at 0.0 (W28 invariant).
    assert actual.elapsed_eu == pytest.approx(0.0)


def test_close_wave_auto_actual_accepts_telemetry_attention_eu() -> None:
    """A telemetry rollup can populate attention/runtime EU on auto-create."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    close_wave(
        state,
        wave_id="P01-I01-W01",
        outcome="ok",
        actual_attention_eu=1.25,
        actual_agent_runtime_eu=1.25,
    )

    assert state.actuals is not None
    actual = state.actuals["P01-I01-W01"]
    assert actual.attention_eu == pytest.approx(1.25)
    assert actual.agent_runtime_eu == pytest.approx(1.25)
    assert actual.elapsed_eu == pytest.approx(0.0)


def test_close_wave_auto_actual_records_elapsed_eu() -> None:
    """A telemetry-derived elapsed EU lands on the auto-created actual."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    close_wave(
        state,
        wave_id="P01-I01-W01",
        outcome="ok",
        actual_elapsed_eu=1.5,
    )

    assert state.actuals is not None
    actual = state.actuals["P01-I01-W01"]
    assert actual.elapsed_eu == pytest.approx(1.5)


def test_close_wave_negative_elapsed_eu_rejects_without_mutation() -> None:
    """A negative elapsed EU is rejected before the wave closes."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    with pytest.raises(LifecycleError, match="actual_elapsed_eu must be non-negative"):
        close_wave(
            state,
            wave_id="P01-I01-W01",
            outcome="ok",
            actual_elapsed_eu=-1.0,
        )

    wave = state.waves["P01-I01-W01"]
    assert wave.status == WaveStatus.CLAIMED
    assert state.actuals is None


def test_close_wave_no_elapsed_eu_leaves_zero() -> None:
    """Omitting elapsed EU keeps the honest zero-EU auto-actual."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert state.actuals is not None
    assert state.actuals["P01-I01-W01"].elapsed_eu == pytest.approx(0.0)


def test_compute_runtime_delta_absent_baseline_returns_none() -> None:
    latest = RuntimeLatest(
        api_duration_ms=17000,
        cost_usd=0.42,
        input_tokens=100,
        output_tokens=50,
        captured_at=datetime.now(UTC),
    )

    assert compute_runtime_delta(None, latest, eu_minutes=30.0) is None


def test_compute_runtime_delta_equal_counters_yields_zero_eu() -> None:
    captured_at = datetime.now(UTC)
    baseline = RuntimeBaseline(
        api_duration_ms=5000,
        cost_usd=0.25,
        input_tokens=10,
        output_tokens=20,
        captured_at=captured_at,
    )
    latest = RuntimeLatest(
        api_duration_ms=5000,
        cost_usd=0.25,
        input_tokens=10,
        output_tokens=20,
        captured_at=captured_at,
    )

    delta = compute_runtime_delta(baseline, latest, eu_minutes=30.0)

    assert delta is not None
    assert delta.elapsed_eu == pytest.approx(0.0)
    assert delta.agent_runtime_eu == pytest.approx(0.0)
    assert delta.actual_tokens == 0
    assert delta.actual_cost_usd == pytest.approx(0.0)


def test_eu_basis_api_duration_default() -> None:
    captured_at = datetime.now(UTC)
    baseline = RuntimeBaseline(
        api_duration_ms=5000,
        total_duration_ms=8000,
        input_tokens=100,
        captured_at=captured_at,
    )
    latest = RuntimeLatest(
        api_duration_ms=17000,
        total_duration_ms=48000,
        input_tokens=500,
        captured_at=captured_at,
    )

    delta = compute_runtime_delta(baseline, latest, eu_minutes=30.0)

    assert delta is not None
    assert delta.elapsed_eu == pytest.approx(12000 / (30 * 60_000))
    assert delta.agent_runtime_eu == pytest.approx(delta.elapsed_eu)


def test_eu_basis_tokens_uses_token_delta() -> None:
    captured_at = datetime.now(UTC)
    baseline = RuntimeBaseline(
        api_duration_ms=5000,
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=5,
        cache_read_input_tokens=15,
        captured_at=captured_at,
    )
    latest = RuntimeLatest(
        api_duration_ms=17000,
        input_tokens=600,
        output_tokens=220,
        cache_creation_input_tokens=55,
        cache_read_input_tokens=65,
        captured_at=captured_at,
    )

    delta = compute_runtime_delta(
        baseline,
        latest,
        eu_minutes=30.0,
        eu_basis=EuBasis.TOKENS,
    )

    assert delta is not None
    assert delta.actual_tokens == 800
    assert delta.elapsed_eu == pytest.approx(800 / DEFAULT_TOKENS_PER_EU)
    assert delta.agent_runtime_eu == pytest.approx(delta.elapsed_eu)


def test_compute_runtime_delta_latest_below_baseline_raises() -> None:
    captured_at = datetime.now(UTC)
    baseline = RuntimeBaseline(api_duration_ms=5000, captured_at=captured_at)
    latest = RuntimeLatest(api_duration_ms=4999, captured_at=captured_at)

    with pytest.raises(LifecycleError, match="api_duration_ms"):
        compute_runtime_delta(baseline, latest, eu_minutes=30.0)


def test_close_wave_tokens_consumed_param_sets_final_tally() -> None:
    """The close call may set the final token tally before actual upsert."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].tokens_consumed = 100

    wave = close_wave(
        state,
        wave_id="P01-I01-W01",
        outcome="ok",
        tokens_consumed=4242,
    )

    assert wave.tokens_consumed == 4242
    assert state.actuals is not None
    assert state.actuals["P01-I01-W01"].actual_tokens == 4242


def test_close_wave_negative_tokens_consumed_rejects_without_mutation() -> None:
    """A negative final token tally is rejected before closing the wave."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].tokens_consumed = 100

    with pytest.raises(LifecycleError, match="tokens_consumed must be non-negative"):
        close_wave(
            state,
            wave_id="P01-I01-W01",
            outcome="ok",
            tokens_consumed=-1,
        )

    wave = state.waves["P01-I01-W01"]
    assert wave.status == WaveStatus.CLAIMED
    assert wave.tokens_consumed == 100
    assert state.actuals is None


def test_close_wave_upserts_actual_zero_tokens_when_unaccrued() -> None:
    """A wave that never accrued tokens still upserts (zero token tally)."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert state.actuals is not None
    actual = state.actuals["P01-I01-W01"]
    assert actual.actual_tokens == 0
    assert actual.actual_cost_usd == pytest.approx(0.0)


def test_close_wave_refreshes_existing_actual_tokens() -> None:
    """A pre-existing ActualSummary keeps elapsed_eu; token tally refreshed."""
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # Pre-seed a measured actual (as if `eawf actual stop` had run).
    from eawf.kernel.state.enums import ActualStatus
    from eawf.kernel.state.models import ActualSummary

    seeded_at = datetime(2026, 5, 1, tzinfo=UTC)
    state.actuals = {
        "P01-I01-W01": ActualSummary(
            id="ACT-P01-I01-W01",
            scope_id="P01-I01-W01",
            status=ActualStatus.ACTIVE,
            elapsed_eu=1.25,
            attention_eu=2.0,
            agent_runtime_eu=2.0,
            actual_tokens=100,
            current_store_record_id="REC-P01-I01-W01",
            updated_at=seeded_at,
        )
    }
    state.waves["P01-I01-W01"].tokens_consumed = 9999

    close_wave(
        state,
        wave_id="P01-I01-W01",
        outcome="ok",
        actual_attention_eu=9.0,
        actual_agent_runtime_eu=9.0,
    )

    actual = state.actuals["P01-I01-W01"]
    # Token tally refreshed, status flipped to DONE, but the measured
    # elapsed/attention EU values are operator-authored and preserved.
    assert actual.actual_tokens == 9999
    assert actual.status == ActualStatus.DONE
    assert actual.elapsed_eu == pytest.approx(1.25)
    assert actual.attention_eu == pytest.approx(2.0)
    assert actual.agent_runtime_eu == pytest.approx(2.0)
    assert actual.updated_at > seeded_at


def test_close_wave_missing_opened_at_does_not_crash() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].opened_at = None

    wave = close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert wave.status == WaveStatus.CLOSED
    # The token-only upsert still runs even when opened_at is missing.
    assert state.actuals is not None
    assert "P01-I01-W01" in state.actuals


def test_close_wave_idempotent_double_close_is_safe() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=30)
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    # A second close on an already-CLOSED wave is rejected by the status guard.
    with pytest.raises(Exception, match="not claimed"):
        close_wave(state, wave_id="P01-I01-W01", outcome="ok")


# ---- variance metric still empty without measured elapsed-EU (W28 invariant) -


def test_metrics_variance_empty_without_measured_elapsed_eu() -> None:
    """compute_estimate_actual_variance ignores token-only auto-actuals.

    The upserted ActualSummary leaves ``elapsed_eu=0.0``; the variance
    metric sums ``actual.elapsed_eu`` so the aggregate stays at zero,
    matching the W28 invariant that wall-clock spans never feed M26.
    """
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=45)
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    metric = compute_estimate_actual_variance(state)

    # One sample (the token-only auto-actual), but zero actual_eu means
    # the aggregate variance is -100% (close came in under the M-bucket
    # planned estimate of 1.0 EU because no measured elapsed_eu landed).
    assert metric.sample_count == 1
    assert metric.actual_eu == pytest.approx(0.0)
    assert metric.planned_eu == pytest.approx(1.0)
    assert metric.variance_pct == pytest.approx(-100.0)
