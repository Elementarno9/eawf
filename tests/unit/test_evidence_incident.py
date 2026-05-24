"""Unit tests for :mod:`eawf.evidence.incident`."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.evidence import _io, audit, incident
from eawf.kernel.state.enums import (
    AuditKind,
    AuditVerdict,
    IncidentSeverity,
    IncidentStatus,
    StoreKind,
)
from eawf.kernel.state.models import Artifact, State

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


def test_open_incident_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    record, event = incident.open_incident(
        state,
        incident_id="INC-001",
        scope_id="QR",
        severity=IncidentSeverity.HIGH,
        title="Train/test leakage",
    )
    inc = state.incidents["INC-001"]
    assert inc.status == IncidentStatus.OPEN
    assert inc.severity == IncidentSeverity.HIGH
    assert record.payload["severity"] == "high"
    assert event.payload["event_type"] == "incident.open"


def test_open_incident_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    incident.open_incident(
        state,
        incident_id="INC-001",
        scope_id="QR",
        severity=IncidentSeverity.LOW,
        title="t",
    )
    with pytest.raises(cli_errors.UserError, match="already exists"):
        incident.open_incident(
            state,
            incident_id="INC-001",
            scope_id="QR",
            severity=IncidentSeverity.LOW,
            title="t",
        )


def test_close_incident_unknown_raises_not_found(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError):
        incident.close_incident(
            state,
            incident_id="INC-999",
            root_cause="x",
            corrective_action_ids=[],
            audit_id="AUD-001",
        )


def test_close_incident_without_complete_audit_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    incident.open_incident(
        state,
        incident_id="INC-001",
        scope_id="QR",
        severity=IncidentSeverity.HIGH,
        title="t",
    )
    with pytest.raises(cli_errors.ValidationError, match="UNKNOWN"):
        incident.close_incident(
            state,
            incident_id="INC-001",
            root_cause="x",
            corrective_action_ids=[],
            audit_id="AUD-NOPE",
        )


def test_close_incident_happy_with_complete_audit(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    incident.open_incident(
        state,
        incident_id="INC-001",
        scope_id="QR",
        severity=IncidentSeverity.MEDIUM,
        title="t",
    )
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.INCIDENT,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.PASS,
    )
    record, event = incident.close_incident(
        state,
        incident_id="INC-001",
        root_cause="config bug",
        corrective_action_ids=["B-100"],
        audit_id="AUD-001",
    )
    inc = state.incidents["INC-001"]
    assert inc.status == IncidentStatus.RESOLVED
    assert inc.root_cause == "config bug"
    assert inc.corrective_action_ids == ["B-100"]
    assert inc.closed_at is not None
    assert record.payload["cause"] == "unknown"
    assert "root_cause" not in record.payload
    assert record.payload["timeline"][0]["entry"] == "closed: config bug"
    assert event.payload["event_type"] == "incident.close"


def test_view_incident_unknown_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError):
        incident.view_incident(state, "INC-999")


def test_state_transaction_persists_open_close_incident(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        record, event = incident.open_incident(
            state,
            incident_id="INC-001",
            scope_id="QR",
            severity=IncidentSeverity.HIGH,
            title="leak",
        )
        _io.append_jsonl(paths[StoreKind.INCIDENT], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        _seed_artifact(state)
        record, event = audit.add_audit(
            state,
            audit_id="AUD-001",
            scope_id="QR",
            kind=AuditKind.INCIDENT,
            report_artifact_id="ART-001",
            verdict=AuditVerdict.PASS,
        )
        _io.append_jsonl(paths[StoreKind.AUDIT], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        record, event = incident.close_incident(
            state,
            incident_id="INC-001",
            root_cause="x",
            corrective_action_ids=[],
            audit_id="AUD-001",
        )
        _io.append_jsonl(paths[StoreKind.INCIDENT], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert body["incidents"]["INC-001"]["status"] == "resolved"
    inc_lines = paths[StoreKind.INCIDENT].read_text().splitlines()
    assert len(inc_lines) == 2
    event_lines = paths[StoreKind.EVENT].read_text().splitlines()
    assert len(event_lines) == 3
