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

import pytest

from eawf.kernel.state.enums import (
    Confidence,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.estimation.buckets import default_estimate_summary
from eawf.workflow.estimation.metrics import compute_estimate_actual_variance
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.transitions import (
    claim_wave,
    close_wave,
    open_iter,
    open_phase,
    plan_wave,
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
