"""Unit tests for :mod:`eawf.evidence.audit`.

Covers add (with/without report), run (fixture and stub paths), integrity,
show, and list. The audit-evidence guard is exercised in
``test_evidence_outcome.py`` and ``test_evidence_hypothesis.py``; here we
focus on the audit module's own contract.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.evidence import _io, audit
from eawf.state.enums import AuditKind, AuditStatus, AuditVerdict, StoreKind
from eawf.state.models import Artifact, State

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def _seed_artifact(state: State, artifact_id: str = "ART-001", scope: str = "QR") -> None:
    """Insert a minimum-valid :class:`Artifact` into ``state.artifacts``.

    Required because :func:`audit.add_audit` rejects a ``report_artifact_id``
    that is absent from ``state.artifacts`` (orphan-ref guard).
    """
    artifacts = dict(state.artifacts)
    artifacts[artifact_id] = Artifact(
        id=artifact_id,
        kind="audit_report",
        uri=f"repo:.ea/artifacts/{artifact_id}.md",
        urn=f"urn:eawf:v1:artifact:{scope}/{artifact_id}",
        created_at=datetime.now(UTC),
    )
    state.artifacts = artifacts


def test_add_audit_with_report_lands_complete(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    record, event = audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.PASS,
    )
    assert state.audits is not None
    a = state.audits["AUD-001"]
    assert a.status == AuditStatus.COMPLETE
    assert a.verdict == AuditVerdict.PASS
    assert a.report_artifact_id == "ART-001"
    assert record.payload["audit_kind"] == "evaluation"
    assert event.payload["event_type"] == "audit.add"


def test_add_audit_without_report_lands_pending(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    audit.add_audit(
        state,
        audit_id="AUD-002",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id=None,
    )
    assert state.audits["AUD-002"].status == AuditStatus.PENDING


def test_add_audit_verdict_without_report_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="no report"):
        audit.add_audit(
            state,
            audit_id="AUD-003",
            scope_id="QR",
            kind=AuditKind.EVALUATION,
            report_artifact_id=None,
            verdict=AuditVerdict.PASS,
        )


def test_add_audit_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
    )
    with pytest.raises(cli_errors.UserError, match="already exists"):
        audit.add_audit(
            state,
            audit_id="AUD-001",
            scope_id="QR",
            kind=AuditKind.EVALUATION,
        )


def test_run_audit_stub_path(tmp_path: Path) -> None:
    """Without a fixture path, run_audit lands a single passing stub result."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    record, event = audit.run_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        fixture_path=None,
    )
    a = state.audits["AUD-001"]
    assert a.status == AuditStatus.COMPLETE
    assert a.verdict == AuditVerdict.PASS
    assert a.check_results == [{"name": "stub", "passed": True, "details": "Phase 2 stub"}]
    assert record.payload["check_results"][0]["passed"] is True
    assert event.payload["event_type"] == "audit.run"


def test_run_audit_fixture_path(tmp_path: Path) -> None:
    """A fixture file populates check_results; verdict reflects all-passed."""
    fixture = tmp_path / "checks.json"
    fixture.write_text(
        json.dumps(
            [
                {"name": "lint", "passed": True, "details": "ruff clean"},
                {"name": "tests", "passed": True, "details": "120 ok"},
            ]
        )
    )
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    record, _ = audit.run_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.SHIP_GATE,
        fixture_path=fixture,
    )
    a = state.audits["AUD-001"]
    assert a.kind == AuditKind.SHIP_GATE
    assert a.verdict == AuditVerdict.PASS
    assert {r["name"] for r in record.payload["check_results"]} == {"lint", "tests"}


def test_run_audit_with_failed_check_yields_major_verdict(tmp_path: Path) -> None:
    fixture = tmp_path / "checks.json"
    fixture.write_text(json.dumps([{"name": "lint", "passed": False}]))
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    audit.run_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        fixture_path=fixture,
    )
    assert state.audits["AUD-001"].verdict == AuditVerdict.MAJOR


def test_add_integrity_appends_result(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
    )
    _, event = audit.add_integrity(
        state,
        audit_id="AUD-001",
        check="leakage",
        passed=True,
        details=None,
    )
    a = state.audits["AUD-001"]
    assert len(a.integrity_results) == 1
    assert a.integrity_results[0]["check"] == "leakage"
    assert event.payload["event_type"] == "audit.integrity"


def test_add_integrity_unknown_audit_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="AUD-999"):
        audit.add_integrity(
            state,
            audit_id="AUD-999",
            check="leakage",
            passed=True,
        )


def test_show_audit_unknown_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError):
        audit.show_audit(state, "AUD-DOES-NOT-EXIST")


def test_list_audits_filters(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
    )
    audit.add_audit(
        state,
        audit_id="AUD-002",
        scope_id="QR",
        kind=AuditKind.SHIP_GATE,
    )
    eval_only = audit.list_audits(state, kind=AuditKind.EVALUATION)
    assert {a.id for a in eval_only} == {"AUD-001"}

    complete_only = audit.list_audits(state, status=AuditStatus.COMPLETE)
    assert {a.id for a in complete_only} == {"AUD-001"}


def test_state_transaction_persists_add_audit(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        _seed_artifact(state)
        record, event = audit.add_audit(
            state,
            audit_id="AUD-001",
            scope_id="QR",
            kind=AuditKind.EVALUATION,
            report_artifact_id="ART-001",
        )
        _io.append_jsonl(paths[StoreKind.AUDIT], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert body["audits"]["AUD-001"]["status"] == "complete"
    audit_lines = paths[StoreKind.AUDIT].read_text().splitlines()
    assert len(audit_lines) == 1
    event_lines = paths[StoreKind.EVENT].read_text().splitlines()
    assert len(event_lines) == 1


def test_add_audit_rejects_unknown_report_artifact_id(tmp_path: Path) -> None:
    """Orphan-ref guard: a report_artifact_id absent from state.artifacts
    raises InvalidInput so the audit-evidence anchor never points at a
    placeholder id (closes the missing W02 audit-evidence guard)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    # No artifact seeded — state.artifacts is empty.
    with pytest.raises(cli_errors.UserError, match="ART-999"):
        audit.add_audit(
            state,
            audit_id="AUD-001",
            scope_id="QR",
            kind=AuditKind.EVALUATION,
            report_artifact_id="ART-999",
            verdict=AuditVerdict.PASS,
        )
    # No audit was inserted before the guard fired.
    assert (state.audits or {}) == {}


def test_set_verdict_lifts_pending_to_complete(tmp_path: Path) -> None:
    """Pending audit + --report lands status=complete with the supplied verdict."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
    )
    assert state.audits["AUD-001"].status == AuditStatus.PENDING

    record, event = audit.set_verdict(
        state,
        audit_id="AUD-001",
        verdict=AuditVerdict.PASS,
        report_artifact_id="ART-001",
    )
    a = state.audits["AUD-001"]
    assert a.status == AuditStatus.COMPLETE
    assert a.verdict == AuditVerdict.PASS
    assert a.report_artifact_id == "ART-001"
    assert record.payload["verdict"] == "pass"
    assert event.payload["event_type"] == "audit.set_verdict"


def test_set_verdict_pending_without_report_raises(tmp_path: Path) -> None:
    """Pending audit + no --report raises InvalidInput."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
    )
    with pytest.raises(cli_errors.UserError, match="pending"):
        audit.set_verdict(
            state,
            audit_id="AUD-001",
            verdict=AuditVerdict.PASS,
            report_artifact_id=None,
        )


def test_set_verdict_unknown_audit_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="AUD-999"):
        audit.set_verdict(
            state,
            audit_id="AUD-999",
            verdict=AuditVerdict.PASS,
        )


def test_set_verdict_orphan_report_raises(tmp_path: Path) -> None:
    """Pending audit + unknown artifact raises InvalidInput (orphan-ref guard)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
    )
    with pytest.raises(cli_errors.UserError, match="ART-999"):
        audit.set_verdict(
            state,
            audit_id="AUD-001",
            verdict=AuditVerdict.PASS,
            report_artifact_id="ART-999",
        )


def test_set_verdict_complete_audit_updates_verdict(tmp_path: Path) -> None:
    """Already-complete audit: verdict updated in place, report unchanged."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.MINOR,
    )
    audit.set_verdict(
        state,
        audit_id="AUD-001",
        verdict=AuditVerdict.PASS,
    )
    a = state.audits["AUD-001"]
    assert a.verdict == AuditVerdict.PASS
    assert a.report_artifact_id == "ART-001"
    assert a.status == AuditStatus.COMPLETE


def test_set_verdict_complete_rejects_differing_report(tmp_path: Path) -> None:
    """Already-complete audit: differing --report rejected to prevent silent relink."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state, "ART-001")
    _seed_artifact(state, "ART-002")
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
    )
    with pytest.raises(cli_errors.UserError, match="ART-002"):
        audit.set_verdict(
            state,
            audit_id="AUD-001",
            verdict=AuditVerdict.PASS,
            report_artifact_id="ART-002",
        )


def test_set_verdict_complete_accepts_same_report(tmp_path: Path) -> None:
    """Passing the same --report on a complete audit is a no-op for the field."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
    )
    audit.set_verdict(
        state,
        audit_id="AUD-001",
        verdict=AuditVerdict.PASS,
        report_artifact_id="ART-001",
    )
    assert state.audits["AUD-001"].verdict == AuditVerdict.PASS
    assert state.audits["AUD-001"].report_artifact_id == "ART-001"


def test_add_audit_accepts_known_report_artifact_id(tmp_path: Path) -> None:
    """Happy path for the orphan-ref guard: the cited artifact already
    exists in state.artifacts, so the audit lands status=complete."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_artifact(state, "ART-001")
    record, event = audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.PASS,
    )
    assert state.audits is not None
    a = state.audits["AUD-001"]
    assert a.status == AuditStatus.COMPLETE
    assert a.report_artifact_id == "ART-001"
    assert record.payload["report_artifact_id"] == "ART-001"
    assert event.payload["event_type"] == "audit.add"
