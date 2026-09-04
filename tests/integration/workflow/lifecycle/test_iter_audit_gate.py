"""Strict iter-close audit-link acceptance tests."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    IterStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import Audit, CurrentPointers, Project, State
from eawf.workflow.lifecycle._audit_acceptance import AUDIT_MINOR_BACKLOG_TRIAGE
from eawf.workflow.lifecycle._errors import LifecycleGuardError
from eawf.workflow.lifecycle.legacy_audit import (
    acknowledge_invalid_iter_audits,
    load_legacy_audit_dispositions,
)
from eawf.workflow.lifecycle.transitions import close_iter, open_iter, open_phase

_ITER_ID = "P01-I01"
_AUDIT_ID = "AUD-I01"
_NOW = datetime(2020, 1, 1, tzinfo=UTC)


def _state() -> State:
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _NOW.isoformat(),
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
    open_phase(state, phase_id="P01", title="Phase")
    open_iter(state, iter_id=_ITER_ID, phase_id="P01", title="Iter")
    return state


def _accepted_audit(*, verdict: AuditVerdict = AuditVerdict.PASS) -> Audit:
    return Audit(
        id=_AUDIT_ID,
        scope_id=_ITER_ID,
        kind=AuditKind.EVALUATION,
        status=AuditStatus.COMPLETE,
        created_at=_NOW,
        verdict=verdict,
        check_results=[
            {
                "name": "acceptance",
                "passed": True,
                "details": "targeted verification passed",
            }
        ],
    )


def _missing(_audit: Audit) -> None:
    return None


def _wrong_scope(audit: Audit) -> None:
    audit.scope_id = "P01"


def _wrong_kind(audit: Audit) -> None:
    audit.kind = AuditKind.SHIP_GATE


def _pending(audit: Audit) -> None:
    audit.status = AuditStatus.PENDING


def _failed(audit: Audit) -> None:
    audit.status = AuditStatus.FAILED


def _major(audit: Audit) -> None:
    audit.verdict = AuditVerdict.MAJOR


def _future(audit: Audit) -> None:
    audit.created_at = datetime(2099, 1, 1, tzinfo=UTC)


def _stub_only(audit: Audit) -> None:
    audit.check_results = [{"name": "stub", "passed": True, "details": "Phase 2 stub"}]


def _empty(audit: Audit) -> None:
    audit.check_results = []


def _failed_check_only(audit: Audit) -> None:
    audit.check_results = [
        {"name": "acceptance", "passed": False, "details": "verification failed"}
    ]


@pytest.mark.parametrize(
    ("mutate_audit", "expected_code"),
    [
        (_missing, "audit_not_found"),
        (_wrong_scope, "audit_scope_mismatch"),
        (_wrong_kind, "audit_kind_invalid"),
        (_pending, "audit_not_complete"),
        (_failed, "audit_not_complete"),
        (_major, "audit_verdict_rejected"),
        (_future, "audit_not_complete"),
        (_stub_only, "audit_evidence_missing"),
        (_empty, "audit_evidence_missing"),
        (_failed_check_only, "audit_evidence_missing"),
    ],
    ids=[
        "unknown",
        "wrong-scope",
        "wrong-kind",
        "pending",
        "failed",
        "major",
        "future",
        "stub-only",
        "empty",
        "failed-check-only",
    ],
)
def test_close_iter_strict_invalid_audit_rejects_without_state_mutation(
    mutate_audit: Callable[[Audit], None],
    expected_code: str,
) -> None:
    state = _state()
    audit = _accepted_audit()
    mutate_audit(audit)
    if mutate_audit is not _missing:
        state.audits = {_AUDIT_ID: audit}
    before = state.model_dump(mode="json")

    with pytest.raises(LifecycleGuardError) as exc_info:
        close_iter(
            state,
            iter_id=_ITER_ID,
            audit_id=_AUDIT_ID,
            require_audit_accepted=True,
        )

    assert exc_info.value.code == expected_code
    assert state.model_dump(mode="json") == before


def test_close_iter_strict_false_preserves_unchecked_audit_compatibility() -> None:
    state = _state()

    closed = close_iter(state, iter_id=_ITER_ID, audit_id="AUD-PHANTOM")

    assert closed.status is IterStatus.CLOSED
    assert closed.audit_id == "AUD-PHANTOM"


def test_legacy_missing_audit_gets_unverified_idempotent_disposition(
    tmp_path: Path,
) -> None:
    state = _state()
    state.iters[_ITER_ID].status = IterStatus.CLOSED
    state.iters[_ITER_ID].closed_at = _NOW
    state.iters[_ITER_ID].audit_id = None
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    assert acknowledge_invalid_iter_audits(
        state_path,
        reason="legacy close predates strict audit links",
    ) == (1, 0)
    assert acknowledge_invalid_iter_audits(
        state_path,
        reason="legacy close predates strict audit links",
    ) == (0, 1)
    rows = load_legacy_audit_dispositions(state_path)
    assert len(rows) == 1
    assert rows[0].iter_id == _ITER_ID
    assert rows[0].audit_id is None
    assert rows[0].disposition == "acknowledged_legacy_unverified"


def test_close_iter_strict_pass_stores_exact_audit_id() -> None:
    state = _state()
    state.audits = {_AUDIT_ID: _accepted_audit()}
    warnings: list[str] = []

    closed = close_iter(
        state,
        iter_id=_ITER_ID,
        audit_id=_AUDIT_ID,
        require_audit_accepted=True,
        warnings_out=warnings,
    )

    assert closed.status is IterStatus.CLOSED
    assert closed.audit_id == _AUDIT_ID
    assert warnings == []


def test_close_iter_strict_passing_check_survives_missing_report_artifact() -> None:
    state = _state()
    audit = _accepted_audit()
    audit.report_artifact_id = "ART-MISSING"
    state.audits = {_AUDIT_ID: audit}

    closed = close_iter(
        state,
        iter_id=_ITER_ID,
        audit_id=_AUDIT_ID,
        require_audit_accepted=True,
    )

    assert closed.status is IterStatus.CLOSED
    assert closed.audit_id == _AUDIT_ID


def test_close_iter_strict_minor_returns_backlog_triage_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state()
    state.audits = {_AUDIT_ID: _accepted_audit(verdict=AuditVerdict.MINOR)}
    warnings: list[str] = []

    with caplog.at_level(logging.WARNING):
        closed = close_iter(
            state,
            iter_id=_ITER_ID,
            audit_id=_AUDIT_ID,
            require_audit_accepted=True,
            warnings_out=warnings,
        )

    assert closed.status is IterStatus.CLOSED
    assert warnings == [AUDIT_MINOR_BACKLOG_TRIAGE]
    assert AUDIT_MINOR_BACKLOG_TRIAGE in caplog.text
