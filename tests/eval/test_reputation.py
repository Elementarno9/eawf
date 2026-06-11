"""Verdict-to-outcome projection tests (P29-I05-W01).

The outcome loop closes the gap from a per-wave verdict (an AUDITOR report at
``base_id=wave_id``) to its realized, state-observable outcome, so a later
reputation/Brier scorer has data to score. These tests pin:

- the honest-empty path -- an empty report store yields ``[]`` (today's real
  result, not a bug);
- a clean closed iter -> ``held is True`` / ``outcome_source == "clean"``;
- a later reactive iter under the same phase -> ``held is False`` /
  ``outcome_source == "reactive"``;
- a reopened phase -> ``held is False`` / ``outcome_source == "reopen"``;
- an in-flight wave -> ``held is None`` (not yet observable);
- the confidence -> float mapping (high/med/low -> 0.9/0.7/0.55); and
- the model's ``extra="forbid"`` + ``confidence`` bound error paths.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    IterStatus,
    IterTrigger,
    PhaseStatus,
    WaveStatus,
)
from eawf.kernel.state.models import Iter, Phase, State, Wave
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.observability.eval import (
    DEFAULT_TIER_THRESHOLDS,
    FleetVerdictRow,
    ReliabilityStatus,
    ReputationConfig,
    ReputationTier,
    RoleReliability,
    VerdictOutcome,
    build_verdict_outcomes,
    compute_role_reliability,
    confidence_to_float,
    fleet_verdict_rollup,
    map_reliability_to_tier,
)
from eawf.observability.eval.reputation import _CONFIDENCE_TO_FLOAT
from eawf.workflow.estimation.trust_scorecard import TrustTier

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _empty_state() -> State:
    """Return a minimal but valid State with no phases/iters/waves."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _phase(*, phase_id: str, status: PhaseStatus, audit_id: str | None = None) -> Phase:
    """Return a Phase carrying the fields the outcome loop reads."""
    return Phase(
        id=phase_id,
        scope_id="QR",
        title=f"phase {phase_id}",
        status=status,
        opened_at=_T0,
        closed_at=None if status is not PhaseStatus.CLOSED else _T0,
        audit_id=audit_id,
    )


def _iter(*, iter_id: str, status: IterStatus, trigger: IterTrigger) -> Iter:
    """Return an Iter carrying *status* + *trigger*."""
    phase_id = iter_id.split("-")[0]
    return Iter(
        id=iter_id,
        phase_id=phase_id,
        title=f"iter {iter_id}",
        status=status,
        trigger=trigger,
        opened_at=_T0,
    )


def _wave(*, wave_id: str, status: WaveStatus) -> Wave:
    """Return a Wave under the iter parsed from *wave_id*."""
    iter_id = "-".join(wave_id.split("-")[:2])
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"wave {wave_id}",
        status=status,
        opened_at=_T0,
        closed_at=_T0 if status is WaveStatus.CLOSED else None,
    )


def _write_auditor_verdict(
    state_path: Path,
    *,
    base_id: str,
    index: int = 0,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    confidence: Confidence = Confidence.HIGH,
    runtime: str = "claude",
) -> None:
    """Append one AUDITOR verdict envelope at ``base_id`` to the on-disk store."""
    role = AgentSessionRole.AUDITOR
    report_id = report_record_id(role=role, base_id=base_id, attempt=1)
    moment = _T0 + timedelta(minutes=index)
    body = AuditorReportBody(
        verdict=verdict,
        confidence=confidence,
        summary="recorded auditor verdict",
        target_id=base_id,
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{index:02d}",
        scope_id=f"{base_id}::audit",
        base_id=base_id,
        attempt=1,
        runtime=runtime,
        generated_at=moment,
        summary="recorded auditor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=moment,
        updated_at=None,
        summary="recorded auditor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


def _state_with_clean_closed_wave(*, wave_id: str = "P01-I01-W01") -> State:
    """Build a state where *wave_id* sits in a clean closed phase/iter."""
    state = _empty_state()
    phase_id = wave_id.split("-")[0]
    iter_id = "-".join(wave_id.split("-")[:2])
    state.phases[phase_id] = _phase(phase_id=phase_id, status=PhaseStatus.CLOSED, audit_id="AUD-1")
    state.iters[iter_id] = _iter(
        iter_id=iter_id, status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)
    return state


# --- build_verdict_outcomes: honest-empty path ----------------------------


def test_build_verdict_outcomes_empty_store_returns_empty(tmp_path: Path) -> None:
    """An empty per-wave report store yields ``[]`` -- today's real result.

    This is the load-bearing honest-negative criterion: zero AUDITOR verdict
    rows exist on disk today, so the projection has nothing to project and must
    return an empty list rather than fabricate an outcome.
    """
    state_path = tmp_path / "state.json"
    state = _state_with_clean_closed_wave()

    assert build_verdict_outcomes(state, state_path) == []


def test_build_verdict_outcomes_skips_verdict_for_unknown_wave(tmp_path: Path) -> None:
    """A verdict whose base_id names no wave in state is skipped."""
    state_path = tmp_path / "state.json"
    _write_auditor_verdict(state_path, base_id="P99-I99-W99")
    state = _state_with_clean_closed_wave()

    assert build_verdict_outcomes(state, state_path) == []


# --- build_verdict_outcomes: clean held outcome ---------------------------


def test_build_verdict_outcomes_clean_closed_iter_holds(tmp_path: Path) -> None:
    """A pass verdict on a wave in a clean closed iter holds clean."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id, verdict=AgentReportVerdict.PASS)
    state = _state_with_clean_closed_wave(wave_id=wave_id)

    outcomes = build_verdict_outcomes(state, state_path)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.base_id == wave_id
    assert outcome.agent_role is AgentSessionRole.AUDITOR
    assert outcome.verdict is AgentReportVerdict.PASS
    assert outcome.held is True
    assert outcome.outcome_source == "clean"


def test_build_verdict_outcomes_carries_runtime_and_confidence(tmp_path: Path) -> None:
    """The projection copies the report runtime and maps its confidence bucket."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(
        state_path, base_id=wave_id, confidence=Confidence.MEDIUM, runtime="codex"
    )
    state = _state_with_clean_closed_wave(wave_id=wave_id)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.runtime == "codex"
    assert outcome.confidence == pytest.approx(0.7)


# --- build_verdict_outcomes: reactive refutation --------------------------


def test_build_verdict_outcomes_later_reactive_iter_refutes(tmp_path: Path) -> None:
    """A later reactive iter under the same phase refutes the verdict."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _state_with_clean_closed_wave(wave_id=wave_id)
    # A later repair iter covers the wave's scope.
    state.iters["P01-I02"] = _iter(
        iter_id="P01-I02", status=IterStatus.ACTIVE, trigger=IterTrigger.REACTIVE
    )

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is False
    assert outcome.outcome_source == "reactive"


def test_build_verdict_outcomes_earlier_reactive_iter_does_not_refute(tmp_path: Path) -> None:
    """A reactive iter that is NOT strictly later does not refute the wave."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I02-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.CLOSED, audit_id="AUD-1")
    # An earlier reactive iter (I01) precedes the wave's iter (I02): no refutation.
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.CLOSED, trigger=IterTrigger.REACTIVE
    )
    state.iters["P01-I02"] = _iter(
        iter_id="P01-I02", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is True
    assert outcome.outcome_source == "clean"


# --- build_verdict_outcomes: reopen refutation ----------------------------


def test_build_verdict_outcomes_reopened_phase_refutes(tmp_path: Path) -> None:
    """A reopened phase (ACTIVE + preserved audit_id) refutes the verdict."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    # reopen_phase flips CLOSED -> ACTIVE but preserves audit_id.
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id="AUD-1")
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is False
    assert outcome.outcome_source == "reopen"


def test_build_verdict_outcomes_never_closed_active_phase_is_not_reopen(tmp_path: Path) -> None:
    """An ACTIVE phase with no audit_id is in-flight, not a reopen."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    # Never-closed ACTIVE phase: audit_id is None.
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.IN_PROGRESS)

    outcome = build_verdict_outcomes(state, state_path)[0]

    # In-flight, not refuted: not yet observable.
    assert outcome.held is None
    assert outcome.outcome_source is None


# --- build_verdict_outcomes: not-yet-observable ---------------------------


def test_build_verdict_outcomes_in_flight_wave_is_unobservable(tmp_path: Path) -> None:
    """An open wave in an open iter has no settled outcome -- held is None."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.IN_PROGRESS)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is None
    assert outcome.outcome_source is None


def test_build_verdict_outcomes_closed_wave_open_iter_is_unobservable(tmp_path: Path) -> None:
    """A closed wave whose iter is still open is not yet settled."""
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _write_auditor_verdict(state_path, base_id=wave_id)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.ACTIVE, audit_id=None)
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.ACTIVE, trigger=IterTrigger.PROACTIVE
    )
    state.waves[wave_id] = _wave(wave_id=wave_id, status=WaveStatus.CLOSED)

    outcome = build_verdict_outcomes(state, state_path)[0]

    assert outcome.held is None
    assert outcome.outcome_source is None


# --- build_verdict_outcomes: iter_id filter -------------------------------


def test_build_verdict_outcomes_iter_filter_restricts_cohort(tmp_path: Path) -> None:
    """The iter_id filter keeps only verdicts whose wave is under that iter."""
    state_path = tmp_path / "state.json"
    keep_wave = "P01-I01-W01"
    drop_wave = "P01-I02-W01"
    _write_auditor_verdict(state_path, base_id=keep_wave, index=0)
    _write_auditor_verdict(state_path, base_id=drop_wave, index=1)
    state = _empty_state()
    state.phases["P01"] = _phase(phase_id="P01", status=PhaseStatus.CLOSED, audit_id="AUD-1")
    state.iters["P01-I01"] = _iter(
        iter_id="P01-I01", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.iters["P01-I02"] = _iter(
        iter_id="P01-I02", status=IterStatus.CLOSED, trigger=IterTrigger.PROACTIVE
    )
    state.waves[keep_wave] = _wave(wave_id=keep_wave, status=WaveStatus.CLOSED)
    state.waves[drop_wave] = _wave(wave_id=drop_wave, status=WaveStatus.CLOSED)

    outcomes = build_verdict_outcomes(state, state_path, iter_id="P01-I01")

    assert [outcome.base_id for outcome in outcomes] == [keep_wave]


# --- confidence_to_float mapping ------------------------------------------


def test_confidence_to_float_maps_high_med_low() -> None:
    """The ratified mapping is high/med/low -> 0.9/0.7/0.55."""
    assert confidence_to_float(Confidence.HIGH) == pytest.approx(0.9)
    assert confidence_to_float(Confidence.MEDIUM) == pytest.approx(0.7)
    assert confidence_to_float(Confidence.LOW) == pytest.approx(0.55)


def test_confidence_to_float_covers_every_confidence_value() -> None:
    """Every Confidence member has a mapping (no bucket falls through)."""
    assert set(_CONFIDENCE_TO_FLOAT) == set(Confidence)


# --- VerdictOutcome model error paths -------------------------------------


def test_verdict_outcome_rejects_out_of_range_confidence() -> None:
    """A confidence above 1.0 fails the [0.0, 1.0] bound."""
    with pytest.raises(ValidationError):
        VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude",
            verdict=AgentReportVerdict.PASS,
            confidence=1.5,
        )


def test_verdict_outcome_rejects_extra_field() -> None:
    """An unexpected field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude",
            verdict=AgentReportVerdict.PASS,
            confidence=0.9,
            unexpected="boom",
        )


# --- reliability scoring layer (P29-I05-W02) ------------------------------


def _outcome(
    *,
    held: bool | None,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    confidence: float = 0.9,
    agent_role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> VerdictOutcome:
    """Build one observed (or in-flight) VerdictOutcome for scorer tests."""
    return VerdictOutcome(
        base_id="P01-I01-W01",
        agent_role=agent_role,
        runtime=runtime,
        verdict=verdict,
        confidence=confidence,
        held=held,
        outcome_source=None if held is None else ("clean" if held else "reactive"),
    )


def _held_group(
    *,
    n: int,
    held_count: int,
    confidence: float = 0.9,
    agent_role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> list[VerdictOutcome]:
    """Build *n* PASS outcomes, *held_count* of which held, the rest refuted."""
    rows: list[VerdictOutcome] = []
    for i in range(n):
        rows.append(
            _outcome(
                held=i < held_count,
                verdict=AgentReportVerdict.PASS,
                confidence=confidence,
                agent_role=agent_role,
                runtime=runtime,
            )
        )
    return rows


# --- compute_role_reliability: honest-empty + refuse-to-score -------------


def test_compute_role_reliability_empty_outcomes_returns_empty() -> None:
    """Empty outcomes yield ``[]`` -- the honest-empty path (today's reality).

    The verdict-outcome substrate is empty today, so the scorer has nothing to
    score and must return an empty list rather than fabricate a reliability.
    """
    assert compute_role_reliability([], ReputationConfig(), {}) == []


def test_compute_role_reliability_only_unobservable_rows_returns_empty() -> None:
    """A group of only in-flight (``held is None``) rows scores nothing."""
    outcomes = [_outcome(held=None), _outcome(held=None)]

    assert compute_role_reliability(outcomes, ReputationConfig(), {}) == []


def test_compute_role_reliability_below_min_n_is_insufficient() -> None:
    """A group with ``n < min_n`` refuses to score -- the refuse-to-score path.

    This is the real today state once a handful of verdicts accrue but the
    cohort is still under the honesty floor: status INSUFFICIENT with every
    numeric field ``None``, never a fabricated or zero held-rate.
    """
    config = ReputationConfig(min_n=20)
    outcomes = _held_group(n=5, held_count=5)

    result = compute_role_reliability(outcomes, config, {})

    assert len(result) == 1
    reliability = result[0]
    assert reliability.n == 5
    assert reliability.status is ReliabilityStatus.INSUFFICIENT
    assert reliability.brier is None
    assert reliability.reliability is None
    assert reliability.resolution is None
    assert reliability.posterior_lower_bound is None
    assert reliability.routing_score is None


def test_compute_role_reliability_unobservable_rows_do_not_count_toward_min_n() -> None:
    """In-flight rows are dropped before the min-N gate is applied."""
    config = ReputationConfig(min_n=20)
    # 5 observed + 30 in-flight: only the 5 observed count, so still under N.
    outcomes = _held_group(n=5, held_count=5) + [_outcome(held=None) for _ in range(30)]

    result = compute_role_reliability(outcomes, config, {})

    assert len(result) == 1
    assert result[0].n == 5
    assert result[0].status is ReliabilityStatus.INSUFFICIENT


# --- compute_role_reliability: scored path --------------------------------


def test_compute_role_reliability_above_min_n_narrow_ci_is_scored() -> None:
    """A large, near-unanimous group clears both gates and scores real values."""
    config = ReputationConfig(min_n=10, ci_width_gate=0.3)
    outcomes = _held_group(n=40, held_count=40, confidence=0.9)

    result = compute_role_reliability(outcomes, config, {})

    assert len(result) == 1
    reliability = result[0]
    assert reliability.status is ReliabilityStatus.SCORED
    assert reliability.n == 40
    assert reliability.brier is not None
    assert 0.0 <= reliability.brier <= 1.0
    assert reliability.posterior_lower_bound is not None
    assert 0.0 <= reliability.posterior_lower_bound <= 1.0
    assert reliability.routing_score is not None
    assert 0.0 <= reliability.routing_score <= 1.0
    assert reliability.reliability is not None
    assert reliability.resolution is not None


def test_compute_role_reliability_routing_score_above_display_lb() -> None:
    """The optimistic routing upper bound is >= the conservative display LB."""
    config = ReputationConfig(min_n=10, ci_width_gate=0.5)
    outcomes = _held_group(n=40, held_count=36, confidence=0.9)

    reliability = compute_role_reliability(outcomes, config, {})[0]

    assert reliability.status is ReliabilityStatus.SCORED
    assert reliability.posterior_lower_bound is not None
    assert reliability.routing_score is not None
    assert reliability.routing_score >= reliability.posterior_lower_bound


def test_compute_role_reliability_groups_by_role_and_runtime() -> None:
    """Distinct (role, runtime) pairs are scored as separate groups, sorted."""
    config = ReputationConfig(min_n=10, ci_width_gate=0.5)
    outcomes = (
        _held_group(n=20, held_count=20, agent_role=AgentSessionRole.EXECUTOR, runtime="claude")
        + _held_group(n=20, held_count=20, agent_role=AgentSessionRole.EXECUTOR, runtime="codex")
        + _held_group(n=20, held_count=20, agent_role=AgentSessionRole.AUDITOR, runtime="claude")
    )

    result = compute_role_reliability(outcomes, config, {})

    keys = [(r.agent_role, r.runtime) for r in result]
    assert keys == [
        (AgentSessionRole.AUDITOR, "claude"),
        (AgentSessionRole.EXECUTOR, "claude"),
        (AgentSessionRole.EXECUTOR, "codex"),
    ]


# --- compute_role_reliability: Brier ordering -----------------------------


def test_compute_role_reliability_calibrated_held_beats_confident_wrong() -> None:
    """A confident-and-right group has a far lower Brier than confident-wrong.

    A PASS verdict at confidence 0.9 forecasts p(hold)=0.9; when every wave
    actually held the Brier is small, and when every wave was refuted the Brier
    is large. The scorer must rank the calibrated group below the wrong one.
    """
    config = ReputationConfig(min_n=10, ci_width_gate=1.0)
    calibrated = _held_group(n=30, held_count=30, confidence=0.9)
    confident_wrong = _held_group(n=30, held_count=0, confidence=0.9)

    good = compute_role_reliability(calibrated, config, {})[0]
    bad = compute_role_reliability(confident_wrong, config, {})[0]

    assert good.brier is not None
    assert bad.brier is not None
    # p(hold)=0.9 vs outcome 1.0 -> (0.1)^2 = 0.01; vs outcome 0.0 -> 0.81.
    assert good.brier == pytest.approx(0.01)
    assert bad.brier == pytest.approx(0.81)
    assert good.brier < bad.brier


# --- compute_role_reliability: CI-width gate ------------------------------


def test_compute_role_reliability_wide_ci_is_insufficient_despite_count() -> None:
    """A group past the count floor but with a wide posterior CI refuses.

    A 50/50 split at the count floor keeps the posterior near 0.5 where its
    credible interval is widest, so the held-rate is not yet pinned down and
    the display LB would be noise -- status INSUFFICIENT even though n >= min_n.
    """
    config = ReputationConfig(min_n=20, ci_width_gate=0.3)
    # n == min_n but a 50/50 split -> widest posterior CI (> 0.3).
    outcomes = _held_group(n=20, held_count=10, confidence=0.9)

    reliability = compute_role_reliability(outcomes, config, {})[0]

    assert reliability.n == 20
    assert reliability.status is ReliabilityStatus.INSUFFICIENT
    assert reliability.posterior_lower_bound is None


def test_compute_role_reliability_sibling_prior_shrinks_estimate() -> None:
    """A lower sibling prior shrinks the held-rate LB down vs a higher prior.

    Empirical-Bayes folds the sibling prior in as synthetic trials, so the same
    observed group scored against a low prior yields a lower display LB than
    against a high prior -- the shrink is toward the prior.
    """
    config = ReputationConfig(min_n=10, ci_width_gate=1.0)
    outcomes = _held_group(n=20, held_count=18, confidence=0.9)

    low = compute_role_reliability(outcomes, config, {AgentSessionRole.EXECUTOR: 0.1})[0]
    high = compute_role_reliability(outcomes, config, {AgentSessionRole.EXECUTOR: 0.9})[0]

    assert low.posterior_lower_bound is not None
    assert high.posterior_lower_bound is not None
    assert low.posterior_lower_bound < high.posterior_lower_bound


# --- ReputationConfig validation ------------------------------------------


def test_reputation_config_rejects_min_n_below_one() -> None:
    """``min_n=0`` fails the ``ge=1`` bound -- a zero floor defeats the gate."""
    with pytest.raises(ValidationError):
        ReputationConfig(min_n=0)


def test_reputation_config_rejects_extra_field() -> None:
    """An unexpected config key fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        ReputationConfig(unexpected="boom")


def test_reputation_config_rejects_loss_weight_out_of_range() -> None:
    """``loss_weight`` above its 5.0 ceiling fails the bound."""
    with pytest.raises(ValidationError):
        ReputationConfig(loss_weight=6.0)


def test_reputation_config_defaults() -> None:
    """The trust.* leaf defaults match the ratified reputation-engine design."""
    config = ReputationConfig()

    assert config.min_n == 20
    assert config.ci_width_gate == pytest.approx(0.3)
    assert config.tier_thresholds == {}
    assert config.loss_weight == pytest.approx(3.0)


# --- RoleReliability model error paths ------------------------------------


def test_role_reliability_rejects_out_of_range_lower_bound() -> None:
    """A posterior_lower_bound above 1.0 fails the [0.0, 1.0] bound."""
    with pytest.raises(ValidationError):
        RoleReliability(
            agent_role=AgentSessionRole.EXECUTOR,
            runtime="claude",
            n=20,
            status=ReliabilityStatus.SCORED,
            posterior_lower_bound=1.5,
        )


def test_role_reliability_rejects_extra_field() -> None:
    """An unexpected field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        RoleReliability(
            agent_role=AgentSessionRole.EXECUTOR,
            runtime="claude",
            n=20,
            status=ReliabilityStatus.SCORED,
            unexpected="boom",
        )


# --- trust-tier ladder (P29-I05-W03) --------------------------------------


def test_map_reliability_to_tier_high_lb_maps_to_a() -> None:
    """A display LB of 0.9 clears the default A floor (0.85) -> tier A."""
    assert map_reliability_to_tier(0.9, ReputationConfig()) is ReputationTier.A


def test_map_reliability_to_tier_mid_lb_maps_to_b() -> None:
    """A display LB of 0.7 clears B (0.6) but not A (0.85) -> tier B."""
    assert map_reliability_to_tier(0.7, ReputationConfig()) is ReputationTier.B


def test_map_reliability_to_tier_low_lb_maps_to_c() -> None:
    """A display LB of 0.3 clears only the C floor (0.0) -> tier C."""
    assert map_reliability_to_tier(0.3, ReputationConfig()) is ReputationTier.C


def test_map_reliability_to_tier_zero_lb_maps_to_c() -> None:
    """A display LB of 0.0 sits on the C floor -> tier C (never below C)."""
    assert map_reliability_to_tier(0.0, ReputationConfig()) is ReputationTier.C


def test_map_reliability_to_tier_b_floor_is_inclusive() -> None:
    """An LB exactly on the B floor maps to B (the >= convention)."""
    b_floor = DEFAULT_TIER_THRESHOLDS["B"]

    assert map_reliability_to_tier(b_floor, ReputationConfig()) is ReputationTier.B


def test_map_reliability_to_tier_a_floor_is_inclusive() -> None:
    """An LB exactly on the A floor maps to A (the >= convention)."""
    a_floor = DEFAULT_TIER_THRESHOLDS["A"]

    assert map_reliability_to_tier(a_floor, ReputationConfig()) is ReputationTier.A


def test_map_reliability_to_tier_just_below_a_floor_maps_to_b() -> None:
    """An LB a hair below the A floor falls back to B, not A."""
    a_floor = DEFAULT_TIER_THRESHOLDS["A"]

    assert map_reliability_to_tier(a_floor - 1e-9, ReputationConfig()) is ReputationTier.B


def test_map_reliability_to_tier_custom_thresholds_override_defaults() -> None:
    """A non-empty config.tier_thresholds overrides DEFAULT_TIER_THRESHOLDS.

    With a stricter A floor of 0.95, an LB of 0.9 that would be A under the
    defaults drops to B -- proving the override is honoured, not the default.
    """
    config = ReputationConfig(tier_thresholds={"C": 0.0, "B": 0.5, "A": 0.95})

    assert map_reliability_to_tier(0.9, config) is ReputationTier.B
    assert map_reliability_to_tier(0.96, config) is ReputationTier.A
    # 0.4 is below the custom B floor (0.5) -> C.
    assert map_reliability_to_tier(0.4, config) is ReputationTier.C


def test_compute_role_reliability_scored_row_carries_tier() -> None:
    """A SCORED RoleReliability carries a non-None tier off its display LB.

    A large, unanimous held group clears both gates and earns a display LB,
    so the ladder assigns it a tier -- the wired-in path the moment a group
    scores.
    """
    config = ReputationConfig(min_n=10, ci_width_gate=0.3)
    outcomes = _held_group(n=40, held_count=40, confidence=0.9)

    reliability = compute_role_reliability(outcomes, config, {})[0]

    assert reliability.status is ReliabilityStatus.SCORED
    assert reliability.tier is not None
    assert reliability.posterior_lower_bound is not None
    # The tier matches a direct map of the row's own display LB.
    assert reliability.tier is map_reliability_to_tier(reliability.posterior_lower_bound, config)


def test_compute_role_reliability_insufficient_row_has_no_tier() -> None:
    """An INSUFFICIENT RoleReliability has tier None -- today's real result.

    No verdict-outcome row is SCORED today (the substrate is empty), so every
    role is INSUFFICIENT and its tier stays None: the honest-empty surface, not
    a fabricated C.
    """
    config = ReputationConfig(min_n=20)
    outcomes = _held_group(n=5, held_count=5)

    reliability = compute_role_reliability(outcomes, config, {})[0]

    assert reliability.status is ReliabilityStatus.INSUFFICIENT
    assert reliability.tier is None


def test_reputation_tier_values_are_c_b_a() -> None:
    """ReputationTier exposes exactly the C / B / A value space."""
    assert {tier.value for tier in ReputationTier} == {"C", "B", "A"}


def test_reputation_tier_distinct_from_output_quality_trust_tier() -> None:
    """The reputation ladder shares no value with the output-quality TrustTier.

    A regression guard against conflating the agent-reputation ladder
    (ReputationTier: does this agent earn autonomy) with the output-quality
    tier (TrustTier: was this artifact verified). The two are different
    concepts and must not share a value space.
    """
    reputation_values = {tier.value for tier in ReputationTier}
    output_quality_values = set(typing.get_args(TrustTier))

    assert reputation_values.isdisjoint(output_quality_values)


# --- fleet_verdict_rollup: per-wave latest verdict (P30-I07-W09) -----------


def _append_auditor_verdict(
    state_path: Path,
    *,
    base_id: str,
    attempt: int = 1,
    verdict: AgentReportVerdict,
    runtime: str = "claude",
) -> None:
    """Append one AUDITOR verdict envelope at ``base_id``/``attempt`` to disk.

    An attempt-aware sibling of the module-level :func:`_write_auditor_verdict`
    so a single wave can carry two verdict attempts (the latest-wins boundary)
    without colliding on the record id.
    """
    role = AgentSessionRole.AUDITOR
    report_id = report_record_id(role=role, base_id=base_id, attempt=attempt)
    moment = _T0 + timedelta(minutes=attempt)
    body = AuditorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="recorded auditor verdict",
        target_id=base_id,
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{attempt:02d}",
        scope_id=f"{base_id}::audit",
        base_id=base_id,
        attempt=attempt,
        runtime=runtime,
        generated_at=moment,
        summary="recorded auditor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=moment,
        updated_at=None,
        summary="recorded auditor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


def test_fleet_verdict_rollup_empty_store_returns_empty(tmp_path: Path) -> None:
    """An empty per-wave report store yields ``[]`` -- the honest-empty path.

    The load-bearing honesty criterion: zero AUDITOR verdict rows on disk means
    the rollup has nothing to surface and must return an empty list rather than
    fabricate a verdict row. The pane paints its honest-empty line off this.
    """
    state_path = tmp_path / "state.json"

    assert fleet_verdict_rollup(state_path) == []


def test_fleet_verdict_rollup_lists_each_wave_latest_verdict(tmp_path: Path) -> None:
    """Two waves with one verdict each surface both, ordered by wave id.

    The success-criterion data half: a pass on one wave and a fail on another
    both land in the rollup, each carrying its own verdict + runtime.
    """
    state_path = tmp_path / "state.json"
    _append_auditor_verdict(
        state_path, base_id="P01-I01-W01", verdict=AgentReportVerdict.PASS, runtime="claude"
    )
    _append_auditor_verdict(
        state_path, base_id="P01-I01-W02", verdict=AgentReportVerdict.FAIL, runtime="codex"
    )

    rollup = fleet_verdict_rollup(state_path)

    assert rollup == [
        FleetVerdictRow(wave_id="P01-I01-W01", verdict=AgentReportVerdict.PASS, runtime="claude"),
        FleetVerdictRow(wave_id="P01-I01-W02", verdict=AgentReportVerdict.FAIL, runtime="codex"),
    ]


def test_fleet_verdict_rollup_keeps_latest_verdict_per_wave(tmp_path: Path) -> None:
    """Two verdict attempts on one wave -> the latest verdict wins.

    Boundary: an earlier FAIL must not shadow a later PASS, so a re-audited wave
    surfaces its current verdict and not a stale earlier one.
    """
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _append_auditor_verdict(state_path, base_id=wave_id, attempt=1, verdict=AgentReportVerdict.FAIL)
    _append_auditor_verdict(state_path, base_id=wave_id, attempt=2, verdict=AgentReportVerdict.PASS)

    rollup = fleet_verdict_rollup(state_path)

    assert len(rollup) == 1
    assert rollup[0].wave_id == wave_id
    assert rollup[0].verdict is AgentReportVerdict.PASS


def test_fleet_verdict_rollup_orders_by_wave_id(tmp_path: Path) -> None:
    """Rows come back wave-id-sorted regardless of write order, for a stable pane."""
    state_path = tmp_path / "state.json"
    _append_auditor_verdict(state_path, base_id="P01-I01-W03", verdict=AgentReportVerdict.PASS)
    _append_auditor_verdict(state_path, base_id="P01-I01-W01", verdict=AgentReportVerdict.FAIL)

    rollup = fleet_verdict_rollup(state_path)

    assert [row.wave_id for row in rollup] == ["P01-I01-W01", "P01-I01-W03"]


def test_fleet_verdict_row_forbids_extra_field() -> None:
    """A drifted FleetVerdictRow field raises at construction (extra=forbid)."""
    with pytest.raises(ValidationError):
        FleetVerdictRow(
            wave_id="P01-I01-W01",
            verdict=AgentReportVerdict.PASS,
            runtime="claude",
            drift="x",  # type: ignore[call-arg]
        )
