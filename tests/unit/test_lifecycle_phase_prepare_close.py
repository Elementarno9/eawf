"""Unit tests for ``eawf phase prepare-close`` checklist computation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    DecisionStatus,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    Artifact,
    Audit,
    CurrentPointers,
    Decision,
    Project,
    State,
)
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    close_phase,
    open_iter,
    open_phase,
    plan_wave,
)
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


def _add_ship_gate_audit(
    state: State,
    *,
    audit_id: str = "AUD-1",
    phase_id: str = "P03",
    report_artifact_id: str | None = None,
    check_results: list[dict[str, object]] | None = None,
) -> None:
    state.audits = dict(state.audits or {})
    state.audits[audit_id] = Audit(
        id=audit_id,
        scope_id=phase_id,
        kind=AuditKind.SHIP_GATE,
        status=AuditStatus.COMPLETE,
        created_at=datetime.now(UTC),
        verdict=AuditVerdict.PASS,
        report_artifact_id=report_artifact_id,
        check_results=list(
            check_results
            if check_results is not None
            else [{"name": "tests", "passed": True, "details": "focused tests passed"}]
        ),
    )


def test_close_phase_blocks_invalid_close_audit_markdown(tmp_path: Path) -> None:
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
        intent=make_intent(),
    )
    wave = state.waves["P03-I01-W01"]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = datetime.now(UTC)
    iter_row = state.iters["P03-I01"]
    iter_row.status = IterStatus.CLOSED
    iter_row.closed_at = datetime.now(UTC)
    iter_row.audit_id = "ITER-AUD-1"
    state.decisions["D-SINGLE"] = Decision(
        id="D-SINGLE",
        scope_id="P03",
        title="P03 scope collapse: finish as single-wave phase",
        rationale="scope collapse accepted because follow-up work moved to next phase",
        alternatives=["open another wave", "leave phase open"],
        status=DecisionStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
    (tmp_path / "bad-audit.md").write_text(
        "# Bad audit\n\n## Summary\n\nUses [1] without rows.\n\n"
        "## References\n\n## Provenance\n\nsource\n\n## Scrub\n\n- status: clean\n",
        encoding="utf-8",
    )
    state.artifacts["ART-AUD-1"] = Artifact(
        id="ART-AUD-1",
        kind="audit_report",
        uri="repo:bad-audit.md",
        urn="urn:eawf:v1:artifact:P03/ART-AUD-1",
        created_at=datetime.now(UTC),
    )
    _add_ship_gate_audit(
        state,
        audit_id="AUD-1",
        phase_id="P03",
        report_artifact_id="ART-AUD-1",
    )

    with pytest.raises(LifecycleError, match="markdown invalid"):
        close_phase(state, phase_id="P03", audit_id="AUD-1", project_root=tmp_path)

    assert state.phases["P03"].status == PhaseStatus.ACTIVE
