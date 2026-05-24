"""Unit tests for :mod:`eawf.workflow.evidence.backlog`.

Covers add (happy + duplicate) and close (audit-evidence guard plus happy path).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AuditKind,
    AuditVerdict,
    BacklogPriority,
    BacklogStatus,
    StoreKind,
)
from eawf.kernel.state.models import Artifact, State
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import state_transaction
from eawf.workflow.evidence import _io, audit, backlog

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
    with pytest.raises(cli_errors.UserError, match="already exists"):
        backlog.add_backlog(
            state, item_id="B023", title="t2", priority=BacklogPriority.P3, scope_id="QR"
        )


def test_close_backlog_unknown_raises_not_found(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError):
        backlog.close_backlog(
            state, item_id="B999", resolution="x", commit="abc", audit_id="AUD-001"
        )


def test_close_backlog_without_complete_audit_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.ValidationError, match="UNKNOWN"):
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


def test_set_priority_happy_each_value(tmp_path: Path) -> None:
    """Boundary: each priority value transition succeeds and writes a single event."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    for new in (BacklogPriority.P1, BacklogPriority.P0, BacklogPriority.P3):
        event = backlog.set_priority(state, item_id="B023", priority=new)
        assert state.backlog["B023"].priority == new
        assert event.payload["event_type"] == "backlog.set_priority"


def test_set_priority_unknown_raises_not_found(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="B999"):
        backlog.set_priority(state, item_id="B999", priority=BacklogPriority.P1)


def test_set_priority_closed_item_raises(tmp_path: Path) -> None:
    """Closed items are frozen — set_priority rejects with InvalidInput."""
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
    backlog.close_backlog(
        state,
        item_id="B023",
        resolution="done",
        commit="abc",
        audit_id="AUD-001",
    )
    with pytest.raises(cli_errors.UserError, match="closed"):
        backlog.set_priority(state, item_id="B023", priority=BacklogPriority.P0)


def test_set_priority_no_op_rejected(tmp_path: Path) -> None:
    """Re-applying the same priority is rejected to keep the event log clean."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.UserError, match="already has priority"):
        backlog.set_priority(state, item_id="B023", priority=BacklogPriority.P2)


def test_add_backlog_with_description(tmp_path: Path) -> None:
    """Happy: a description is stored on the item and flagged in the event."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    event = backlog.add_backlog(
        state,
        item_id="B023",
        title="Split workflow state",
        priority=BacklogPriority.P1,
        scope_id="QR",
        description="Long-form purpose explaining why this item exists.",
    )
    item = state.backlog["B023"]
    assert item.description == "Long-form purpose explaining why this item exists."
    assert event.payload["event_type"] == "backlog.add"


def test_add_backlog_default_description_none(tmp_path: Path) -> None:
    """Boundary: omitting --description leaves the item title-only."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    event = backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    assert state.backlog["B023"].description is None
    assert event.payload["event_type"] == "backlog.add"


def test_add_backlog_title_at_max_length(tmp_path: Path) -> None:
    """Boundary: a 72-char title (the max) is accepted."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="x" * 72, priority=BacklogPriority.P2, scope_id="QR"
    )
    assert len(state.backlog["B023"].title) == 72


def test_add_backlog_title_over_max_raises(tmp_path: Path) -> None:
    """Error: a 73-char title breaches the BacklogItem bound (InvalidInput)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="invalid backlog item"):
        backlog.add_backlog(
            state, item_id="B023", title="x" * 73, priority=BacklogPriority.P2, scope_id="QR"
        )


def test_add_backlog_description_over_max_raises(tmp_path: Path) -> None:
    """Error: a 501-char description breaches the BacklogItem bound."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="invalid backlog item"):
        backlog.add_backlog(
            state,
            item_id="B023",
            title="t",
            priority=BacklogPriority.P2,
            scope_id="QR",
            description="d" * 501,
        )


def test_edit_backlog_title_and_description(tmp_path: Path) -> None:
    """Happy: editing both fields updates the item and lists both in the event."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="old", priority=BacklogPriority.P2, scope_id="QR"
    )
    event = backlog.edit_backlog(
        state, item_id="B023", title="new title", description="new description"
    )
    item = state.backlog["B023"]
    assert item.title == "new title"
    assert item.description == "new description"
    assert event.payload["event_type"] == "backlog.edit"
    assert event.summary == "backlog B023 edited fields=description,title"


def test_edit_backlog_description_only_preserves_title(tmp_path: Path) -> None:
    """Boundary: a description-only edit leaves the title untouched."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="keep me", priority=BacklogPriority.P2, scope_id="QR"
    )
    backlog.edit_backlog(state, item_id="B023", description="added later")
    item = state.backlog["B023"]
    assert item.title == "keep me"
    assert item.description == "added later"


def test_edit_backlog_no_fields_raises(tmp_path: Path) -> None:
    """Error: editing with neither field is rejected (InvalidInput)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.UserError, match="no fields to edit"):
        backlog.edit_backlog(state, item_id="B023")


def test_edit_backlog_unknown_raises_not_found(tmp_path: Path) -> None:
    """Error: editing an absent item raises NotFound."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="not found"):
        backlog.edit_backlog(state, item_id="B999", title="x")


def test_edit_backlog_closed_item_raises(tmp_path: Path) -> None:
    """Error: a closed item is frozen — edit rejects with InvalidInput."""
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
    backlog.close_backlog(
        state, item_id="B023", resolution="done", commit="abc", audit_id="AUD-001"
    )
    with pytest.raises(cli_errors.UserError, match="closed"):
        backlog.edit_backlog(state, item_id="B023", title="new")


def test_edit_backlog_title_over_max_raises(tmp_path: Path) -> None:
    """Error: an over-72 title on edit breaches the re-validated bound."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.UserError, match="invalid backlog edit"):
        backlog.edit_backlog(state, item_id="B023", title="x" * 73)


def test_edit_backlog_persists_via_state_transaction(tmp_path: Path) -> None:
    """The edit round-trips through state_transaction onto disk."""
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        event = backlog.add_backlog(
            state, item_id="B023", title="t", priority=BacklogPriority.P1, scope_id="QR"
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        event = backlog.edit_backlog(state, item_id="B023", description="now described")
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B023"]["description"] == "now described"


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
