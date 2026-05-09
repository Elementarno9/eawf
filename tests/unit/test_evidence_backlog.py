"""Unit tests for :mod:`eawf.evidence.backlog`.

Covers add (happy + duplicate) and close (audit-evidence guard plus happy path).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.evidence import _io, audit, backlog
from eawf.state.enums import (
    AuditKind,
    AuditVerdict,
    BacklogPriority,
    BacklogStatus,
    StoreKind,
)
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


def test_add_backlog_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    event = backlog.add_backlog(
        state,
        item_id="B023",
        title="Split workflow state",
        priority=BacklogPriority.P1,
        scope_id="QR",
    )
    item = state.backlog["B023"]
    assert item.status == BacklogStatus.OPEN
    assert item.priority == BacklogPriority.P1
    assert event.payload["event_type"] == "backlog.add"


def test_add_backlog_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.InvalidInput, match="already exists"):
        backlog.add_backlog(
            state, item_id="B023", title="t2", priority=BacklogPriority.P3, scope_id="QR"
        )


def test_close_backlog_unknown_raises_not_found(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.NotFound):
        backlog.close_backlog(
            state, item_id="B999", resolution="x", commit="abc", audit_id="AUD-001"
        )


def test_close_backlog_without_complete_audit_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.ValidationFailed, match="UNKNOWN"):
        backlog.close_backlog(
            state, item_id="B023", resolution="x", commit="abc", audit_id="AUD-NOPE"
        )


def test_close_backlog_happy_with_complete_audit(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
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
    event = backlog.close_backlog(
        state,
        item_id="B023",
        resolution="implemented",
        commit="abc123",
        audit_id="AUD-001",
    )
    item = state.backlog["B023"]
    assert item.status == BacklogStatus.CLOSED
    assert item.resolution == "implemented"
    assert item.commit == "abc123"
    assert event.payload["event_type"] == "backlog.close"


def test_state_transaction_persists_close_backlog(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        event = backlog.add_backlog(
            state, item_id="B023", title="t", priority=BacklogPriority.P1, scope_id="QR"
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
        event = backlog.close_backlog(
            state, item_id="B023", resolution="done", commit="abc", audit_id="AUD-001"
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["status"] == "closed"
    event_lines = paths[StoreKind.EVENT].read_text().splitlines()
    assert len(event_lines) == 3
