"""Unit tests for the phase-wide SHIP_GATE drift-checkpoint audit (P30-I15-W04).

Covers :func:`eawf.workflow.lifecycle.phase.build_ship_gate_drift_audit`, the
reducer that binds the I01 optimistic drift checkpoint over a phase's full
closed-wave window into a real SHIP_GATE :class:`~eawf.kernel.state.models.Audit`
that :func:`eawf.workflow.lifecycle.phase.close_phase` consumes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.phase import (
    _audit_has_real_close_evidence,
    _audit_row_has_result,
    build_ship_gate_drift_audit,
)
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from tests._criteria_helpers import legacy_criteria
from tests.conftest import make_floor_waiver


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


def _intent(*, planned_steps: int) -> IntentBrief:
    """Build an intent whose ``planned_steps`` count is the wave's plan-row count."""
    steps = [f"planned step {i}" for i in range(1, planned_steps + 1)]
    return IntentBrief(
        problem="exercise the phase drift checkpoint",
        desired_outcome="the wave carries a sized plan row",
        priority_rationale="exercises the drift-checkpoint plan-vs-delivered lens",
        planned_steps=steps,
        risks=["none material for the test fixture"],
    )


def _add_closed_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    planned_steps: int,
    delivered_criteria: int,
) -> None:
    """Stage a CLOSED wave whose plan row pairs *planned_steps* vs *delivered_criteria*."""
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id=iter_id,
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        success_criteria=legacy_criteria(
            *(f"criterion {i}" for i in range(1, delivered_criteria + 1))
        ),
        criteria_floor_waiver=make_floor_waiver(),
        intent=_intent(planned_steps=planned_steps),
    )
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = datetime.now(UTC)


def _phase_with_closed_waves(
    *,
    plans: list[tuple[int, int]],
) -> State:
    """Build a phase with one CLOSED wave per (planned_steps, delivered) pair."""
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    for idx, (planned, delivered) in enumerate(plans, start=1):
        _add_closed_wave(
            state,
            wave_id=f"P03-I01-W{idx:02d}",
            iter_id="P03-I01",
            planned_steps=planned,
            delivered_criteria=delivered,
        )
    return state


# --- C1: real audit, non-null verdict, passing check rows, planted drift -> MINOR ---


def test_build_ship_gate_drift_audit_clean_window_is_pass() -> None:
    # Every wave delivered at least as many criteria as the plan called for.
    state = _phase_with_closed_waves(plans=[(2, 2), (3, 4), (1, 1)])

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    assert audit.kind is AuditKind.SHIP_GATE
    assert audit.status is AuditStatus.COMPLETE
    assert audit.scope_id == "P03"
    # Verdict is NOT None on a clean window.
    assert audit.verdict is AuditVerdict.PASS
    # Every check row carries an explicit, passing result.
    assert audit.check_results
    assert all(_audit_row_has_result(row) for row in audit.check_results)
    assert all(row["passed"] is True for row in audit.check_results)


def test_build_ship_gate_drift_audit_is_real_close_evidence() -> None:
    state = _phase_with_closed_waves(plans=[(2, 2), (1, 1)])

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    # The audit passes the close-evidence gate close_phase enforces -- it is
    # NOT a stub/null-verdict audit.
    assert _audit_has_real_close_evidence(state, audit=audit) is True


def test_build_ship_gate_drift_audit_planted_drift_is_minor_not_pass() -> None:
    # Wave W02 delivered 1 criterion against a plan of 3 -> thin -> drift.
    state = _phase_with_closed_waves(plans=[(2, 2), (3, 1), (1, 1)])

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    # Planted drift surfaces as MINOR, never a silent PASS.
    assert audit.verdict is AuditVerdict.MINOR
    # The thin wave's lens row fails; the aggregate cohesion row fails too.
    thin_row = next(r for r in audit.check_results if r["name"] == "wave-drift:P03-I01-W02")
    assert thin_row["passed"] is False
    cohesion_row = next(r for r in audit.check_results if r["name"] == "phase-drift-cohesion")
    assert cohesion_row["passed"] is False
    # The audit still carries real close evidence (non-stub rows).
    assert _audit_has_real_close_evidence(state, audit=audit) is True


# --- C2: spec-derived coverage (one lens per wave + the aggregate cohesion check) ---


def test_build_ship_gate_drift_audit_coverage_is_spec_derived() -> None:
    state = _phase_with_closed_waves(plans=[(2, 2), (3, 4), (1, 1)])

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    names = [row["name"] for row in audit.check_results]
    # One per-wave drift lens per CLOSED wave, in id order ...
    assert names[:3] == [
        "wave-drift:P03-I01-W01",
        "wave-drift:P03-I01-W02",
        "wave-drift:P03-I01-W03",
    ]
    # ... plus exactly one aggregate cohesion lens, so N waves -> N + 1 rows.
    assert names[3] == "phase-drift-cohesion"
    assert len(audit.check_results) == 4


def test_build_ship_gate_drift_audit_coverage_spans_all_phase_iters() -> None:
    # Coverage is phase-wide: closed waves under a second iter contribute too.
    state = _phase_with_closed_waves(plans=[(2, 2)])
    open_iter(state, iter_id="P03-I02", phase_id="P03", title="i2")
    _add_closed_wave(
        state,
        wave_id="P03-I02-W01",
        iter_id="P03-I02",
        planned_steps=1,
        delivered_criteria=1,
    )

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    names = {row["name"] for row in audit.check_results}
    assert "wave-drift:P03-I01-W01" in names
    assert "wave-drift:P03-I02-W01" in names
    # 2 per-wave lenses + 1 cohesion = 3 rows.
    assert len(audit.check_results) == 3


def test_build_ship_gate_drift_audit_wave_without_intent_never_thin() -> None:
    # A wave with no recorded plan (intent=None) reads as not-thin: a missing
    # plan is not evidence of drift (the conservative reading).
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(
        state,
        wave_id="P03-I01-W01",
        iter_id="P03-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        success_criteria=legacy_criteria("only one criterion"),
        criteria_floor_waiver=make_floor_waiver(),
        intent=_intent(planned_steps=1),
    )
    wave = state.waves["P03-I01-W01"]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = datetime.now(UTC)
    # Drop the intent post-plan to simulate a legacy wave with no plan row.
    wave.intent = None

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    assert audit.verdict is AuditVerdict.PASS
    row = next(r for r in audit.check_results if r["name"] == "wave-drift:P03-I01-W01")
    assert row["passed"] is True


# --- error / boundary paths ---


def test_build_ship_gate_drift_audit_unknown_phase_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase 'P99'"):
        build_ship_gate_drift_audit(state, phase_id="P99", audit_id="AUD-DRIFT")


def test_build_ship_gate_drift_audit_no_closed_waves_raises() -> None:
    # A phase with iters but no CLOSED wave has no window to anchor the
    # checkpoint -- an empty-window ship-gate audit would carry no evidence.
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(
        state,
        wave_id="P03-I01-W01",
        iter_id="P03-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        success_criteria=legacy_criteria("c1"),
        criteria_floor_waiver=make_floor_waiver(),
        intent=_intent(planned_steps=1),
    )
    # Wave stays PENDING (not closed).
    with pytest.raises(LifecycleError, match="has no closed waves"):
        build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")


def test_build_ship_gate_drift_audit_honours_created_at_override() -> None:
    state = _phase_with_closed_waves(plans=[(1, 1)])
    stamp = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)

    audit = build_ship_gate_drift_audit(
        state,
        phase_id="P03",
        audit_id="AUD-DRIFT",
        created_at=stamp,
    )

    assert audit.created_at == stamp


def test_build_ship_gate_drift_audit_closed_only_window_ignores_pending() -> None:
    # Only CLOSED waves enter the window; a PENDING sibling must not appear in
    # the spec-derived coverage.
    state = _phase_with_closed_waves(plans=[(2, 2)])
    plan_wave(
        state,
        wave_id="P03-I01-W99",
        iter_id="P03-I01",
        title="pending",
        file_scopes=["y"],
        effort_bucket="M",
        success_criteria=legacy_criteria("c1"),
        criteria_floor_waiver=make_floor_waiver(),
        intent=_intent(planned_steps=3),
    )

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    names = {row["name"] for row in audit.check_results}
    assert "wave-drift:P03-I01-W99" not in names
    # 1 closed-wave lens + 1 cohesion = 2 rows; the thin PENDING wave is ignored.
    assert len(audit.check_results) == 2
    assert audit.verdict is AuditVerdict.PASS


def test_phase_status_stays_active_during_audit_build() -> None:
    # The reducer is pure over state -- it never mutates the phase status.
    state = _phase_with_closed_waves(plans=[(1, 1)])
    assert state.phases["P03"].status is PhaseStatus.ACTIVE

    build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-DRIFT")

    assert state.phases["P03"].status is PhaseStatus.ACTIVE
    # No audit was persisted into state -- the daemon owns insertion.
    assert "AUD-DRIFT" not in (state.audits or {})
    # The closed iter is untouched too.
    assert state.iters["P03-I01"].status is IterStatus.ACTIVE
