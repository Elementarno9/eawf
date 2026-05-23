"""Unit tests for :mod:`eawf.evidence.outcome`.

Covers ``define_outcome`` happy/duplicate paths and the write-time
audit-evidence guard inside ``set_outcome``: missing audit, unknown audit, and
incomplete (pending) audit must all raise :class:`ValidationFailed`.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.evidence import _io, audit, outcome
from eawf.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    OutcomeDirection,
    OutcomeStatus,
    StoreKind,
)
from eawf.state.models import Artifact, Audit, State

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
    that is absent from ``state.artifacts``.
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


def test_define_outcome_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    event = outcome.define_outcome(
        state,
        outcome_id="OUT-001",
        scope_id="QR",
        metric="sharpe",
        threshold=1.0,
        direction=OutcomeDirection.MIN,
    )
    assert state.outcomes is not None
    assert state.outcomes["OUT-001"].status == OutcomeStatus.PENDING
    assert event.payload["event_type"] == "outcome.define"


def test_define_outcome_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    outcome.define_outcome(
        state,
        outcome_id="OUT-001",
        scope_id="QR",
        metric="sharpe",
        threshold=1.0,
        direction=OutcomeDirection.MIN,
    )
    with pytest.raises(cli_errors.UserError, match="already exists"):
        outcome.define_outcome(
            state,
            outcome_id="OUT-001",
            scope_id="QR",
            metric="other",
            threshold=2.0,
            direction=OutcomeDirection.MAX,
        )


def test_set_outcome_unknown_outcome_raises_not_found(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="OUT-999"):
        outcome.set_outcome(
            state,
            outcome_id="OUT-999",
            value=0.5,
            status=OutcomeStatus.MET,
            audit_id="AUD-001",
        )


def test_set_outcome_with_unknown_audit_raises_validation(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    outcome.define_outcome(
        state,
        outcome_id="OUT-001",
        scope_id="QR",
        metric="sharpe",
        threshold=1.0,
        direction=OutcomeDirection.MIN,
    )
    with pytest.raises(cli_errors.ValidationError, match="UNKNOWN"):
        outcome.set_outcome(
            state,
            outcome_id="OUT-001",
            value=0.5,
            status=OutcomeStatus.MISSED,
            audit_id="AUD-DOES-NOT-EXIST",
        )


def test_set_outcome_with_pending_audit_raises_validation(tmp_path: Path) -> None:
    """Audit exists but status != complete -> reject with NOT_COMPLETE."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)

    audits = dict(state.audits or {})
    audits["AUD-PENDING"] = Audit(
        id="AUD-PENDING",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        status=AuditStatus.PENDING,
        report_artifact_id=None,
        check_results=[],
        integrity_results=[],
        created_at=datetime.now(UTC),
        verdict=None,
    )
    state.audits = audits

    outcome.define_outcome(
        state,
        outcome_id="OUT-001",
        scope_id="QR",
        metric="sharpe",
        threshold=1.0,
        direction=OutcomeDirection.MIN,
    )
    with pytest.raises(cli_errors.ValidationError, match="NOT_COMPLETE"):
        outcome.set_outcome(
            state,
            outcome_id="OUT-001",
            value=0.5,
            status=OutcomeStatus.MISSED,
            audit_id="AUD-PENDING",
        )


def test_set_outcome_happy_with_complete_audit(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)

    outcome.define_outcome(
        state,
        outcome_id="OUT-001",
        scope_id="QR",
        metric="sharpe",
        threshold=1.0,
        direction=OutcomeDirection.MIN,
    )
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.PASS,
    )

    event = outcome.set_outcome(
        state,
        outcome_id="OUT-001",
        value=0.85,
        status=OutcomeStatus.MISSED,
        audit_id="AUD-001",
    )
    assert state.outcomes["OUT-001"].audit_id == "AUD-001"
    assert state.outcomes["OUT-001"].status == OutcomeStatus.MISSED
    assert state.outcomes["OUT-001"].value == 0.85
    assert event.payload["event_type"] == "outcome.set"


def test_state_transaction_persists_set_outcome(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)

    with state_transaction(state_path) as state:
        event = outcome.define_outcome(
            state,
            outcome_id="OUT-001",
            scope_id="QR",
            metric="sharpe",
            threshold=1.0,
            direction=OutcomeDirection.MIN,
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        _seed_artifact(state)
        record, event = audit.add_audit(
            state,
            audit_id="AUD-001",
            scope_id="QR",
            kind=AuditKind.EVALUATION,
            report_artifact_id="ART-001",
            verdict=AuditVerdict.PASS,
        )
        _io.append_jsonl(paths[StoreKind.AUDIT], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        event = outcome.set_outcome(
            state,
            outcome_id="OUT-001",
            value=0.5,
            status=OutcomeStatus.MISSED,
            audit_id="AUD-001",
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)

    body = json.loads(state_path.read_text())
    assert body["outcomes"]["OUT-001"]["audit_id"] == "AUD-001"
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert len(events) == 3
    types = [json.loads(line)["payload"]["event_type"] for line in events]
    assert types == ["outcome.define", "audit.add", "outcome.set"]
