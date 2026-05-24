"""Unit tests for the D17 iter-bump hint logic on ``eawf iter open``."""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    IterStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import (
    Audit,
    CurrentPointers,
    Project,
    State,
)
from eawf.surfaces.cli.commands.lifecycle import _compute_iter_bump_hints
from eawf.workflow.lifecycle.transitions import (
    close_iter,
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


def _add_audit(state: State, *, audit_id: str, verdict: AuditVerdict) -> None:
    if state.audits is None:
        state.audits = {}
    state.audits[audit_id] = Audit(
        id=audit_id,
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        status=AuditStatus.COMPLETE,
        report_artifact_id=None,
        check_results=[],
        integrity_results=[],
        created_at=datetime.now(UTC),
        verdict=verdict,
    )


def test_no_hints_on_fresh_phase() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    assert _compute_iter_bump_hints(state, phase_id="P03") == []


def test_previous_iter_audit_failed_hint() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    _add_audit(state, audit_id="A99-P03", verdict=AuditVerdict.MAJOR)
    close_iter(state, iter_id="P03-I01", audit_id="A99-P03")
    hints = _compute_iter_bump_hints(state, phase_id="P03")
    assert "previous_iter_audit_failed" in hints


def test_audit_pass_does_not_trigger() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    _add_audit(state, audit_id="A99-P03", verdict=AuditVerdict.PASS)
    close_iter(state, iter_id="P03-I01", audit_id="A99-P03")
    hints = _compute_iter_bump_hints(state, phase_id="P03")
    assert "previous_iter_audit_failed" not in hints


def test_wave_many_blockers_hint() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(state, wave_id="P03-I01-W01", iter_id="P03-I01", title="dep1", file_scopes=["x"])
    plan_wave(state, wave_id="P03-I01-W02", iter_id="P03-I01", title="dep2", file_scopes=["x"])
    plan_wave(state, wave_id="P03-I01-W03", iter_id="P03-I01", title="dep3", file_scopes=["x"])
    plan_wave(state, wave_id="P03-I01-W04", iter_id="P03-I01", title="dep4", file_scopes=["x"])
    plan_wave(
        state,
        wave_id="P03-I01-W05",
        iter_id="P03-I01",
        title="dependent",
        file_scopes=["y"],
        deps=["P03-I01-W01", "P03-I01-W02", "P03-I01-W03", "P03-I01-W04"],
    )
    hints = _compute_iter_bump_hints(state, phase_id="P03")
    assert "wave_with_many_blockers" in hints


def test_phase_scope_expansion_hint() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    for n in range(1, 8):
        plan_wave(
            state,
            wave_id=f"P03-I01-W{n:02d}",
            iter_id="P03-I01",
            title=f"w{n}",
            file_scopes=["x"],
        )
    _add_audit(state, audit_id="A77-P03", verdict=AuditVerdict.PASS)
    # Close iter requires no open waves; force-mark via direct mutation.
    state.iters["P03-I01"].status = IterStatus.CLOSED
    state.iters["P03-I01"].closed_at = datetime.now(UTC)
    state.iters["P03-I01"].audit_id = "A77-P03"
    hints = _compute_iter_bump_hints(state, phase_id="P03")
    assert "phase_scope_expanded" in hints


def test_unknown_phase_returns_empty() -> None:
    state = _empty_state()
    assert _compute_iter_bump_hints(state, phase_id="P99") == []
