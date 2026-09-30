"""Unit tests for the memory book and the note readers built on it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.envelopes import LedgerCollection, map_ledger_row
from eawf.kernel.state.enums import Confidence, MemoryStatus, MemoryTier
from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.memory import MemoryNote
from eawf.kernel.store.ledger import (
    LedgerError,
    LedgerRecord,
    append_ledger_record,
    line_digest,
    render_ledger_line,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.memory.book import (
    next_note_id,
    note_line,
    notes_from_state,
    read_book,
    select_archivable,
    select_prunable,
)
from eawf.platform.memory.markdown_view import SCOPE_ALL, render_note_view, render_note_views
from eawf.platform.memory.staleness import stale_notes
from eawf.runtime.daemon import methods
from tests._memory_epoch1 import add_epoch1_note
from tests.integration._memory_native import QR_DOCUMENT
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _note(mid: str, **fields: Any) -> MemoryNote:
    base: dict[str, Any] = {
        "id": mid,
        "scope_id": "QR",
        "title": mid,
        "summary": mid,
        "confidence": Confidence.MEDIUM,
        "created_at": NOW - timedelta(days=40),
    }
    return MemoryNote(**{**base, **fields})


def _imported_index_line(mid: str) -> LedgerRecord:
    row = map_ledger_row(
        collection=LedgerCollection.MEMORY_INDEX,
        source_id=mid,
        row={
            "id": mid,
            "scope_id": "QR",
            "summary": "imported",
            "confidence": "low",
            "status": "active",
            "store_record_id": mid,
            "tier": "working",
        },
        source_schema_version="1.20",
        store_only=False,
    )
    return LedgerRecord(
        collection=Epoch2Collection.MEMORY,
        record_key=row.alias,
        status="imported",
        recorded_at=NOW,
        payload=row.model_dump(mode="json"),
    )


def test_read_book_of_a_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert read_book(tmp_path / "memory.jsonl") == {}


def test_read_book_takes_a_native_revision_over_the_imported_row(tmp_path: Path) -> None:
    ledger = tmp_path / "memory.jsonl"
    imported = _imported_index_line("MEM-A")
    append_ledger_record(ledger, imported)
    first = _imported_index_line("MEM-B")
    append_ledger_record(ledger, first)
    revised = _note("MEM-A", tier=MemoryTier.ARCHIVAL)
    append_ledger_record(ledger, note_line(revised, at=NOW, replaces=imported))

    book = read_book(ledger)

    assert sorted(book) == ["MEM-A", "MEM-B"]
    assert book["MEM-A"].note == revised
    assert book["MEM-B"].note.summary == "imported"
    assert book["MEM-B"].note.created_at is None
    assert book["MEM-B"].line == first


def test_read_book_refuses_a_row_imported_from_elsewhere(tmp_path: Path) -> None:
    ledger = tmp_path / "memory.jsonl"
    row = map_ledger_row(
        collection=LedgerCollection.ARTIFACTS,
        source_id="ART-1",
        row={"id": "ART-1"},
        source_schema_version="1.20",
        store_only=False,
    )
    append_ledger_record(
        ledger,
        LedgerRecord(
            collection=Epoch2Collection.MEMORY,
            record_key=row.alias,
            status="imported",
            recorded_at=NOW,
            payload=row.model_dump(mode="json"),
        ),
    )
    with pytest.raises(LedgerError, match="holds no memory"):
        read_book(ledger)


def test_note_line_supersedes_the_line_it_revises() -> None:
    previous = note_line(_note("MEM-A"), at=NOW, replaces=None)
    revised = note_line(_note("MEM-A", status=MemoryStatus.PRUNED), at=NOW, replaces=previous)

    assert previous.supersedes is None
    assert revised.supersedes == line_digest(render_ledger_line(previous))
    assert (revised.record_key, revised.status) == ("MEM-A", "pruned")


def test_next_note_id_takes_the_first_free_id_of_the_day() -> None:
    assert next_note_id((), now=NOW) == "MEM-20260930-01"
    taken = ("MEM-20260930-01", "MEM-20260930-03", "MEM-20260929-02", "MEM-20260930-xx")
    assert next_note_id(taken, now=NOW) == "MEM-20260930-02"


def test_next_note_id_refuses_a_saturated_day() -> None:
    taken = [f"MEM-20260930-{n:02d}" for n in range(1, 100)]
    with pytest.raises(ValueError, match="saturated"):
        next_note_id(taken, now=NOW)


def test_select_prunable_names_why_each_note_is_passed_over() -> None:
    notes = {
        "old": _note("old"),
        "young": _note("young", created_at=NOW - timedelta(days=1)),
        "elsewhere": _note("elsewhere", scope_id="P01"),
        "gone": _note("gone", status=MemoryStatus.PRUNED),
        "stale": _note("stale", status=MemoryStatus.STALE),
        "ageless": _note("ageless", created_at=None),
    }

    selection = select_prunable(
        notes, age_days=30, status_filter=MemoryStatus.ACTIVE, scope_id="QR", now=NOW
    )

    assert selection.selected == ["old"]
    assert selection.reasons == {
        "young": "younger-than-threshold",
        "elsewhere": "scope-mismatch",
        "gone": "already-pruned",
        "stale": "status=stale",
        "ageless": "no-age-anchor",
    }
    assert selection.skipped == sorted(selection.reasons)


def test_select_prunable_at_the_exact_threshold_selects() -> None:
    notes = {"edge": _note("edge", created_at=NOW - timedelta(days=30))}
    selection = select_prunable(
        notes, age_days=30, status_filter=MemoryStatus.ACTIVE, scope_id=None, now=NOW
    )
    assert selection.selected == ["edge"]


def test_select_archivable_takes_only_old_stale_working_notes() -> None:
    notes = {
        "stale": _note("stale", status=MemoryStatus.STALE),
        "active": _note("active"),
        "cold": _note("cold", status=MemoryStatus.STALE, tier=MemoryTier.ARCHIVAL),
        "review": _note("review", status=MemoryStatus.STALE, review_due=NOW - timedelta(days=1)),
    }

    selection = select_archivable(notes, threshold_days=30, now=NOW)

    assert selection.selected == ["stale"]
    assert selection.reasons == {
        "active": "status=active",
        "cold": "tier=archival",
        "review": "younger-than-threshold",
    }


def test_select_archivable_over_no_notes_selects_nothing() -> None:
    selection = select_archivable({}, threshold_days=0, now=NOW)
    assert (selection.selected, selection.skipped) == ([], [])


def test_notes_from_state_reads_bodies_from_the_latest_envelope(tmp_path: Path) -> None:
    state = State.model_validate(QR_DOCUMENT)
    memory_path = tmp_path / "memory.jsonl"
    record = add_epoch1_note(
        state=state, memory_path=memory_path, scope_id="QR", title="t", body="the body", now=NOW
    )
    assert state.memory_index is not None
    orphan = record.summary.model_copy(update={"id": "MEM-ORPHAN"})
    state.memory_index["MEM-ORPHAN"] = orphan

    notes = notes_from_state(state, memory_path)

    assert notes[record.summary.id].body == "the body"
    assert notes[record.summary.id].created_at == NOW
    assert notes["MEM-ORPHAN"].body == ""
    assert notes["MEM-ORPHAN"].created_at is None


def test_memory_add_is_refused_on_a_tree_outside_epoch_2(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    methods.ensure_all_methods_registered()
    params = {"repo_root": str(tmp_path), "scope_id": "QR", "title": "t", "body": "b"}

    with pytest.raises(methods.DaemonValidationError, match="native_authority_required"):
        asyncio.run(methods.dispatch("memory.add", method_context(tmp_path / "rt"), params))


def test_stale_notes_takes_old_active_notes_below_high_confidence() -> None:
    notes = [
        _note("old"),
        _note("sure", confidence=Confidence.HIGH),
        _note("gone", status=MemoryStatus.PRUNED),
        _note("young", created_at=NOW - timedelta(days=29)),
        _note("ageless", created_at=None),
        _note("elsewhere", scope_id="P01"),
        _note("older", created_at=NOW - timedelta(days=90)),
    ]

    stale = stale_notes(notes, age_days=30, now=NOW, scope_id="QR")

    assert [entry.id for entry in stale] == ["older", "old"]
    assert stale[1].age_days == pytest.approx(40.0)


def test_stale_notes_over_no_notes_is_empty() -> None:
    assert stale_notes([], age_days=0, now=NOW) == []


def test_render_note_view_leaves_out_pruned_and_superseded_by_default() -> None:
    notes = {
        "MEM-1": _note("MEM-1"),
        "MEM-2": _note("MEM-2", status=MemoryStatus.SUPERSEDED),
        "MEM-3": _note("MEM-3", status=MemoryStatus.PRUNED),
    }

    body = render_note_view(notes, scope_id="QR")
    with_superseded = render_note_view(notes, scope_id=SCOPE_ALL, include_superseded=True)

    assert "memory-view-QR" in body
    assert "MEM-1" in body and "MEM-2" not in body and "MEM-3" not in body
    assert "MEM-2" in with_superseded and "MEM-3" not in with_superseded


def test_render_note_views_writes_one_view_per_scope_plus_all(tmp_path: Path) -> None:
    notes = {"MEM-1": _note("MEM-1"), "MEM-2": _note("MEM-2", scope_id="P01")}

    planned = render_note_views(notes, output_dir=tmp_path / "views", write=False)
    written = render_note_views(notes, output_dir=tmp_path / "views")

    assert [path.name for path in written] == ["P01.md", "QR.md", "_all.md"]
    assert planned == written
    assert render_note_views({}, output_dir=tmp_path / "none") == []
    assert not (tmp_path / "none").exists()
