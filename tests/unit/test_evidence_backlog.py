"""Unit tests for :mod:`eawf.workflow.evidence.backlog`.

Covers add (happy + duplicate) and close (audit-evidence guard plus happy path).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.spec.intent import IntentBrief
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
from eawf.surfaces.render.agents_md import ENTITY_TITLE_MAX, normalize_entity_title
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


def _audited_intent(**overrides: object) -> IntentBrief:
    """Build a minimum-valid W24-audited :class:`IntentBrief` for tests."""
    payload: dict[str, object] = {
        "problem": "backlog has no structured intent",
        "desired_outcome": "backlog carries typed brief",
    }
    payload.update(overrides)
    return IntentBrief.model_validate(payload)


def test_edit_backlog_sets_w24_audited_intent(tmp_path: Path) -> None:
    """``edit_backlog`` accepts the W24-audited brief shape.

    The persisted ``BacklogItem.intent`` exposes the canonical fields
    (``problem`` / ``desired_outcome`` / ``planned_steps`` / ``risks``
    / ``priority_rationale``) — the legacy ``goal`` triad is gone post-W61.
    """
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    event = backlog.edit_backlog(
        state,
        item_id="B023",
        intent=_audited_intent(
            planned_steps=["draft", "ratify"],
            risks=["scope creep"],
            priority_rationale="audit ranked it above polish",
        ),
    )
    item = state.backlog["B023"]
    assert item.intent is not None
    assert item.intent.problem == "backlog has no structured intent"
    assert item.intent.desired_outcome == "backlog carries typed brief"
    assert item.intent.planned_steps == ["draft", "ratify"]
    assert item.intent.risks == ["scope creep"]
    assert item.intent.priority_rationale == "audit ranked it above polish"
    assert event.summary == "backlog B023 edited fields=intent"


def test_edit_backlog_sets_intent(tmp_path: Path) -> None:
    """Happy: editing intent persists the typed brief and records the field."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    event = backlog.edit_backlog(
        state,
        item_id="B023",
        intent=_audited_intent(
            problem="Retain the operator problem",
            desired_outcome="Planner context survives backlog edits.",
            evidence_refs=["urn:eawf:v1:artifact:QR/ART-001"],
            source_brief_ids=[".ea/artifacts/research/bootstrap.md"],
        ),
    )
    item = state.backlog["B023"]
    assert item.intent is not None
    assert item.intent.problem == "Retain the operator problem"
    assert item.intent.evidence_refs == ["urn:eawf:v1:artifact:QR/ART-001"]
    assert event.summary == "backlog B023 edited fields=intent"


def test_edit_backlog_clears_intent(tmp_path: Path) -> None:
    """Happy: clear_intent removes an attached brief."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    backlog.edit_backlog(state, item_id="B023", intent=_audited_intent())
    event = backlog.edit_backlog(state, item_id="B023", clear_intent=True)
    assert state.backlog["B023"].intent is None
    assert event.summary == "backlog B023 edited fields=intent"


def test_edit_backlog_rejects_intent_and_clear_intent(tmp_path: Path) -> None:
    """Error: set and clear are mutually exclusive."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B023", title="t", priority=BacklogPriority.P2, scope_id="QR"
    )
    with pytest.raises(cli_errors.UserError, match="cannot pass intent and clear_intent"):
        backlog.edit_backlog(
            state,
            item_id="B023",
            intent=_audited_intent(),
            clear_intent=True,
        )


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


def test_add_backlog_empty_description_raises(tmp_path: Path) -> None:
    """Error: a zero-length ``description`` is rejected at ingestion (W56)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="invalid backlog item"):
        backlog.add_backlog(
            state,
            item_id="B023",
            title="t",
            priority=BacklogPriority.P2,
            scope_id="QR",
            description="",
        )


def test_edit_backlog_empty_description_raises(tmp_path: Path) -> None:
    """Error: editing to a zero-length ``description`` is rejected (W56)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state,
        item_id="B023",
        title="t",
        priority=BacklogPriority.P2,
        scope_id="QR",
        description="seed",
    )
    with pytest.raises(cli_errors.UserError, match="invalid backlog edit"):
        backlog.edit_backlog(state, item_id="B023", description="")


# ---- normalize_entity_title (pure helper) ----------------------------------


def test_normalize_entity_title_strips_trailing_period() -> None:
    """A trailing period is removed; titles are labels, not prose."""
    assert normalize_entity_title("Add a bounded title.") == "Add a bounded title"


def test_normalize_entity_title_strips_repeated_trailing_periods() -> None:
    """An ellipsis-style trailing run collapses to a period-free title."""
    assert normalize_entity_title("Do the thing...") == "Do the thing"


def test_normalize_entity_title_truncates_over_cap_on_word_boundary() -> None:
    """An over-72 title is cut to the last whole word that fits the cap."""
    title = "Split the workflow state module into layered submodules for clarity now"
    # 70 chars already; extend past the cap with a final word.
    over = title + " indeed-extra-trailing-token"
    result = normalize_entity_title(over)
    assert len(result) <= ENTITY_TITLE_MAX
    assert not result.endswith(" ")
    # The break lands on a word boundary, so the result is a prefix of the input
    # ending at a whole word (no partial token).
    assert over.startswith(result)
    assert result.split() == [w for w in over.split() if over.index(w) < len(result)]


def test_normalize_entity_title_hard_slices_when_no_word_boundary() -> None:
    """A single over-cap word with no space is hard-sliced to the cap."""
    result = normalize_entity_title("x" * 90)
    assert result == "x" * ENTITY_TITLE_MAX


def test_normalize_entity_title_derives_from_description_when_placeholder() -> None:
    """An empty-placeholder title derives a candidate from the description."""
    result = normalize_entity_title("tbd", "Make the sweep idempotent. Second clause ignored.")
    assert result == "Make the sweep idempotent"


def test_normalize_entity_title_placeholder_without_description_unchanged() -> None:
    """A placeholder title with no description is returned unchanged (caller flags)."""
    assert normalize_entity_title("tbd", None) == "tbd"


def test_normalize_entity_title_leaves_compliant_title_unchanged() -> None:
    """A title already satisfying the rule is returned byte-for-byte."""
    assert normalize_entity_title("Enforce sandbox deny-list at dispatch") == (
        "Enforce sandbox deny-list at dispatch"
    )


def test_normalize_entity_title_is_idempotent() -> None:
    """Re-normalizing a normalized title is a no-op."""
    once = normalize_entity_title("Add a bounded title to every entity.")
    assert normalize_entity_title(once) == once


# ---- backfill_titles (library sweep + apply) -------------------------------


def _seed_backlog_item(
    state: State,
    item_id: str,
    title: str,
    *,
    description: str | None = None,
) -> None:
    """Insert a raw :class:`BacklogItem` bypassing the title bound.

    ``add_backlog`` would reject an over-cap title at ingestion, so a
    deliberately-violating fixture is built with ``model_construct`` to seed the
    sweep / backfill paths with non-compliant data.
    """
    from eawf.kernel.state.models import BacklogItem

    backlog_map = dict(state.backlog or {})
    backlog_map[item_id] = BacklogItem.model_construct(
        id=item_id,
        scope_id="QR",
        title=title,
        description=description,
        priority=BacklogPriority.P2,
        status=BacklogStatus.OPEN,
        created_at=datetime.now(UTC),
        closed_at=None,
        resolution=None,
        commit=None,
        intent=None,
    )
    state.backlog = backlog_map


def test_backfill_titles_dry_run_reports_without_mutating(tmp_path: Path) -> None:
    """Boundary: --dry-run flags a trailing-period title but mutates nothing."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B001", title="Clean title", priority=BacklogPriority.P2, scope_id="QR"
    )
    _seed_backlog_item(state, "B002", "Has a trailing period.")
    report, event = backlog.backfill_titles(state, apply=False)
    assert event is None
    assert report.applied is False
    assert report.total == 2
    assert report.changed == 1
    # The trailing-period title trips the style lint.
    assert report.violations == 1
    row = next(r for r in report.rows if r.item_id == "B002")
    assert row.before == "Has a trailing period."
    assert row.after == "Has a trailing period"
    assert row.changed is True
    # State is untouched.
    assert state.backlog["B002"].title == "Has a trailing period."


def test_backfill_titles_dry_run_clean_backlog_zero_violations(tmp_path: Path) -> None:
    """Boundary: a fully-compliant backlog reports zero changes and zero violations."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state,
        item_id="B001",
        title="Compliant title one",
        priority=BacklogPriority.P1,
        scope_id="QR",
    )
    backlog.add_backlog(
        state,
        item_id="B002",
        title="Compliant title two",
        priority=BacklogPriority.P2,
        scope_id="QR",
    )
    report, event = backlog.backfill_titles(state, apply=False)
    assert event is None
    assert report.total == 2
    assert report.changed == 0
    assert report.violations == 0
    assert all(r.changed is False for r in report.rows)


def test_backfill_titles_apply_persists_normalized_title(tmp_path: Path) -> None:
    """Happy: --apply strips a trailing period and persists through state_transaction."""
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        event = backlog.add_backlog(
            state, item_id="B001", title="t", priority=BacklogPriority.P1, scope_id="QR"
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    # Seed a violating title directly on disk via a second transaction.
    with state_transaction(state_path) as state:
        _seed_backlog_item(state, "B002", "Trailing period title.")
    with state_transaction(state_path) as state:
        report, event = backlog.backfill_titles(state, apply=True)
        assert event is not None
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    assert report.applied is True
    assert report.changed == 1
    body = json.loads(state_path.read_text())
    assert body["backlog"]["B002"]["title"] == "Trailing period title"
    assert event.payload["event_type"] == "backlog.backfill_titles"


def test_backfill_titles_apply_no_changes_returns_no_event(tmp_path: Path) -> None:
    """Boundary: --apply over a clean backlog mutates nothing and emits no event."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B001", title="Already compliant", priority=BacklogPriority.P2, scope_id="QR"
    )
    report, event = backlog.backfill_titles(state, apply=True)
    assert event is None
    assert report.applied is False
    assert report.changed == 0


def test_backfill_titles_truncates_over_cap_title_on_apply(tmp_path: Path) -> None:
    """Apply trims an over-72 title to a word boundary within the cap."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    over = "Split the workflow state module into layered submodules for long-term clarity"
    _seed_backlog_item(state, "B001", over)
    report, event = backlog.backfill_titles(state, apply=True)
    assert event is not None
    new_title = state.backlog["B001"].title
    assert len(new_title) <= 72
    assert over.startswith(new_title)
    row = next(r for r in report.rows if r.item_id == "B001")
    assert row.changed is True


def test_backfill_titles_derives_from_description_when_placeholder(tmp_path: Path) -> None:
    """Apply derives a title from the description when the title is a placeholder."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_backlog_item(
        state,
        "B001",
        "tbd",
        description="Backfill the backlog titles. Extra detail not in the title.",
    )
    report, event = backlog.backfill_titles(state, apply=True)
    assert event is not None
    assert state.backlog["B001"].title == "Backfill the backlog titles"
    row = next(r for r in report.rows if r.item_id == "B001")
    assert row.changed is True


def test_backfill_titles_placeholder_without_description_unchanged(tmp_path: Path) -> None:
    """A placeholder title with no description is left unchanged (model forbids empty)."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    _seed_backlog_item(state, "B001", "tbd", description=None)
    report, event = backlog.backfill_titles(state, apply=True)
    assert event is None
    assert state.backlog["B001"].title == "tbd"
    row = next(r for r in report.rows if r.item_id == "B001")
    assert row.changed is False


def test_backfill_titles_leaves_closed_item_unchanged(tmp_path: Path) -> None:
    """Closed items are swept for reporting but never mutated under --apply."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    backlog.add_backlog(
        state, item_id="B001", title="t", priority=BacklogPriority.P2, scope_id="QR"
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
        state, item_id="B001", resolution="done", commit="abc", audit_id="AUD-001"
    )
    # Force a violating title onto the now-closed item via model_construct.
    _seed_backlog_item(state, "B001", "Closed with trailing period.")
    state.backlog["B001"] = state.backlog["B001"].model_copy(
        update={"status": BacklogStatus.CLOSED}
    )
    report, event = backlog.backfill_titles(state, apply=True)
    assert event is None
    # Title preserved; the row still records the style violation for the sweep.
    assert state.backlog["B001"].title == "Closed with trailing period."
    row = next(r for r in report.rows if r.item_id == "B001")
    assert row.changed is False
    assert row.violations  # trailing-period lint still fires


def test_backfill_titles_empty_backlog(tmp_path: Path) -> None:
    """Boundary: an empty backlog returns a zero-row report and no event."""
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    report, event = backlog.backfill_titles(state, apply=True)
    assert event is None
    assert report.total == 0
    assert report.rows == []
