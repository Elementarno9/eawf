"""End-to-end cadence-integrity tests for ``close_phase`` against stub audits (P30-I15-W05).

The phase-wide drift checkpoint (P30-I15-W04) builds a real SHIP_GATE audit, and
:func:`eawf.workflow.lifecycle.phase.close_phase` consumes it through two
rejection paths the close-readiness gate enforces:

- a ``verdict=None`` audit is rejected at
  :data:`~eawf.workflow.lifecycle.phase._PHASE_CLOSE_ALLOWED_AUDIT_VERDICTS`
  (the ``verdict must be pass or minor`` blocker); and
- a ``verdict=pass`` audit with empty ``check_results`` and no resolving
  ``report_artifact_id`` is rejected at
  :func:`~eawf.workflow.lifecycle.phase._audit_has_real_close_evidence`
  (the ``must include real audit evidence`` blocker).

These tests drive the binding end-to-end *through* ``close_phase`` (not only the
readiness projection): a stub audit cannot pass the phase-close gate, while the
real W04 SHIP_GATE drift audit does.
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
from eawf.kernel.state.models import Audit, CurrentPointers, Project, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.phase import build_ship_gate_drift_audit, close_phase
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from tests._criteria_helpers import legacy_criteria


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
    steps = [f"planned step {i}" for i in range(1, planned_steps + 1)]
    return IntentBrief(
        problem="exercise the phase-close gate",
        desired_outcome="the wave carries a sized plan row",
        priority_rationale="exercises the close-readiness drift lens",
        planned_steps=steps,
        risks=["none material for the test fixture"],
    )


def _closable_phase(*, closed_iter_audit_id: str = "AUD-ITER") -> State:
    """Build a structurally closable phase with two CLOSED waves under a CLOSED iter.

    Two closed waves dodge the single-wave-without-decision gate, the iter is
    CLOSED with its ``audit_id`` set so the iter-audit gate clears, and no wave
    is left non-terminal -- so the only deciding close-readiness criterion is
    the close-audit gate the caller supplies.
    """
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    for idx, (planned, delivered) in enumerate([(2, 2), (1, 1)], start=1):
        wave_id = f"P03-I01-W{idx:02d}"
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P03-I01",
            title="w",
            file_scopes=[f"f{idx}"],
            effort_bucket="M",
            success_criteria=legacy_criteria(
                *(f"criterion {i}" for i in range(1, delivered + 1))
            ),
            intent=_intent(planned_steps=planned),
        )
        wave = state.waves[wave_id]
        wave.status = WaveStatus.CLOSED
        wave.closed_at = datetime.now(UTC)
    it = state.iters["P03-I01"]
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    it.audit_id = closed_iter_audit_id
    return state


def _register_audit(
    state: State,
    *,
    audit_id: str,
    phase_id: str = "P03",
    verdict: AuditVerdict | None,
    check_results: list[dict[str, object]],
) -> None:
    state.audits = dict(state.audits or {})
    state.audits[audit_id] = Audit(
        id=audit_id,
        scope_id=phase_id,
        kind=AuditKind.SHIP_GATE,
        status=AuditStatus.COMPLETE,
        created_at=datetime.now(UTC),
        verdict=verdict,
        check_results=list(check_results),
    )


# --- (a) verdict=None rejected at the verdict-allowlist gate --------------------


def test_close_phase_rejects_null_verdict_audit() -> None:
    state = _closable_phase()
    _register_audit(
        state,
        audit_id="AUD-NULL",
        verdict=None,
        check_results=[{"name": "tests", "passed": True, "details": "ran"}],
    )

    with pytest.raises(LifecycleError, match="verdict must be pass or minor"):
        close_phase(state, phase_id="P03", audit_id="AUD-NULL")

    # The phase is NOT closed -- the gate refused the stub verdict.
    assert state.phases["P03"].status is PhaseStatus.ACTIVE
    assert state.current.phase_id == "P03"


def test_close_phase_rejects_null_verdict_even_with_no_checks() -> None:
    # The verdict gate fires before the evidence gate, so a null verdict is
    # rejected on the verdict message even when checks are also empty.
    state = _closable_phase()
    _register_audit(state, audit_id="AUD-NULL2", verdict=None, check_results=[])

    with pytest.raises(LifecycleError, match="verdict must be pass or minor"):
        close_phase(state, phase_id="P03", audit_id="AUD-NULL2")


# --- (b) verdict=pass with no real evidence rejected at the evidence gate -------


def test_close_phase_rejects_empty_check_pass_audit() -> None:
    state = _closable_phase()
    _register_audit(state, audit_id="AUD-EMPTY", verdict=AuditVerdict.PASS, check_results=[])

    with pytest.raises(LifecycleError, match="must include real audit evidence"):
        close_phase(state, phase_id="P03", audit_id="AUD-EMPTY")

    assert state.phases["P03"].status is PhaseStatus.ACTIVE


def test_close_phase_rejects_legacy_stub_check_pass_audit() -> None:
    # The legacy Phase-2 ``stub`` row is not real evidence either.
    state = _closable_phase()
    _register_audit(
        state,
        audit_id="AUD-STUB",
        verdict=AuditVerdict.PASS,
        check_results=[{"name": "stub", "passed": True, "details": "Phase 2 stub"}],
    )

    with pytest.raises(LifecycleError, match="must include real audit evidence"):
        close_phase(state, phase_id="P03", audit_id="AUD-STUB")

    assert state.phases["P03"].status is PhaseStatus.ACTIVE


# --- (c) the real W04 SHIP_GATE drift audit passes the gate ---------------------


def test_close_phase_accepts_real_ship_gate_drift_audit() -> None:
    state = _closable_phase()
    # Build the real W04 SHIP_GATE drift audit over the phase's closed waves and
    # register it as the close audit close_phase consumes.
    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-SHIP")
    assert audit.verdict is AuditVerdict.PASS
    state.audits = dict(state.audits or {})
    state.audits["AUD-SHIP"] = audit

    phase = close_phase(state, phase_id="P03", audit_id="AUD-SHIP")

    assert phase.status is PhaseStatus.CLOSED
    assert phase.audit_id == "AUD-SHIP"
    assert phase.closed_at is not None
    # current.phase_id cleared on close of the active phase.
    assert state.current.phase_id is None


def test_close_phase_accepts_minor_verdict_ship_gate_audit() -> None:
    # A planted-drift SHIP_GATE audit reads MINOR -- still inside the allowed
    # verdict set and carrying real check rows, so the gate accepts it.
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    for idx, (planned, delivered) in enumerate([(2, 2), (3, 1)], start=1):
        wave_id = f"P03-I01-W{idx:02d}"
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P03-I01",
            title="w",
            file_scopes=[f"f{idx}"],
            effort_bucket="M",
            success_criteria=legacy_criteria(
                *(f"criterion {i}" for i in range(1, delivered + 1))
            ),
            intent=_intent(planned_steps=planned),
        )
        wave = state.waves[wave_id]
        wave.status = WaveStatus.CLOSED
        wave.closed_at = datetime.now(UTC)
    it = state.iters["P03-I01"]
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    it.audit_id = "AUD-ITER"

    audit = build_ship_gate_drift_audit(state, phase_id="P03", audit_id="AUD-SHIP")
    assert audit.verdict is AuditVerdict.MINOR
    state.audits = dict(state.audits or {})
    state.audits["AUD-SHIP"] = audit

    phase = close_phase(state, phase_id="P03", audit_id="AUD-SHIP")

    assert phase.status is PhaseStatus.CLOSED
    assert phase.audit_id == "AUD-SHIP"
