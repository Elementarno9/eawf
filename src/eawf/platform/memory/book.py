"""The memory notes a tree holds, read from either epoch's store.

A reader asks one question -- which notes stand, and what does each say --
and the answer has two sources. An epoch-1 tree keeps a summary per note
in ``state.memory_index`` and the note itself in ``memory.jsonl``. An
epoch-2 tree keeps every note on its generation's memory ledger: the rows
the cutover imported from those two epoch-1 places, and the native
:class:`~eawf.kernel.store.kinds.memory.MemoryNote` lines written since.
Both are read into the same :class:`MemoryNote` here, so the list, view,
staleness, context and selection logic is written once.

A native line is a revision of one note. It supersedes the line it
revises -- the note's previous native line, or its imported index row on
the first revision -- so the ledger reads back one standing note per id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from eawf.kernel.migration.epoch2.envelopes import ImportedLedgerRow, LedgerCollection
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.memory import MEMORY_STORE_SOURCE
from eawf.kernel.state.enums import MemoryStatus, MemoryTier
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.memory import MEMORY_NOTE_KIND, MemoryNote, MemoryPayload
from eawf.kernel.store.ledger import (
    LedgerError,
    LedgerRecord,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.memory.store import read_envelopes

logger = logging.getLogger(__name__)

#: The source collection the cutover imported index rows under.
MEMORY_INDEX_SOURCE = LedgerCollection.MEMORY_INDEX.value


@dataclass(frozen=True)
class StandingNote:
    """One note as the ledger reads it back.

    Attributes:
        note: The note.
        line: The line a revision of it supersedes.
    """

    note: MemoryNote
    line: LedgerRecord


@dataclass(frozen=True)
class NoteSelection:
    """Which notes an age-threshold pass selects, and why it passes over the rest.

    Attributes:
        selected: The selected ids, sorted.
        skipped: The passed-over ids, sorted.
        reasons: Why each passed-over id was passed over.
    """

    selected: list[str]
    skipped: list[str]
    reasons: dict[str, str] = field(default_factory=dict)


def generation_memory_ledger(tree_root: Path) -> Path | None:
    """Return the memory ledger of the generation *tree_root* selects.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        The ledger path, or ``None`` when the tree answers in epoch 1 and
        its notes still live in the epoch-1 document and store.
    """
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        return None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return ledger_path(document, Epoch2Collection.MEMORY)


def notes_from_state(state: State, memory_path: Path) -> dict[str, MemoryNote]:
    """Return the notes an epoch-1 tree holds.

    Args:
        state: The epoch-1 document; its ``memory_index`` names the notes.
        memory_path: The epoch-1 memory store, whose latest envelope per id
            carries the body and the creation time.

    Returns:
        One note per index row, keyed by id. A row the store holds no
        envelope for reads with an empty body and no creation time.
    """
    latest = {env.id: env for env in read_envelopes(memory_path)}
    notes: dict[str, MemoryNote] = {}
    for mid, summary in (state.memory_index or {}).items():
        env = latest.get(mid)
        body = env.payload.get("body") if env is not None else None
        notes[mid] = MemoryNote(
            id=mid,
            scope_id=summary.scope_id,
            title=summary.summary,
            summary=summary.summary,
            body=str(body) if body is not None else "",
            confidence=summary.confidence,
            status=summary.status,
            tier=summary.tier,
            review_due=summary.review_due,
            promoted_to_artifact_id=summary.promoted_to_artifact_id,
            created_at=env.created_at if env is not None else None,
        )
    return notes


def _imported_note(
    index_row: ImportedLedgerRow | None, store_row: ImportedLedgerRow | None
) -> MemoryNote:
    """Return the note an imported index row and store envelope describe together.

    The index row carries the summary, status and tier the note stood at;
    the store envelope carries its body and creation time. A note only the
    store held takes its status from the markers its envelope carries.

    Raises:
        ValidationError: A row does not read as the epoch-1 shape it claims.
    """
    env = Envelope.model_validate(store_row.payload) if store_row is not None else None
    stored = MemoryPayload.model_validate(env.payload) if env is not None else None
    if index_row is not None:
        row = index_row.payload
        return MemoryNote(
            id=index_row.source_id,
            scope_id=str(row["scope_id"]),
            title=str(row["summary"]),
            summary=str(row["summary"]),
            body=stored.body if stored is not None else "",
            confidence=row["confidence"],
            status=row["status"],
            tier=row.get("tier", MemoryTier.WORKING),
            review_due=row.get("review_due"),
            promoted_to_artifact_id=row.get("promoted_to_artifact_id"),
            expired_at=stored.expired_at if stored is not None else None,
            created_at=env.created_at if env is not None else None,
        )
    assert env is not None and stored is not None, "a note is imported from at least one row"
    if stored.expired_at is not None:
        status = MemoryStatus.PRUNED
    elif stored.promoted_to_artifact_id is not None:
        status = MemoryStatus.SUPERSEDED
    else:
        status = MemoryStatus.ACTIVE
    return MemoryNote(
        id=env.id,
        scope_id=env.scope_id or "unscoped",
        title=env.summary,
        summary=env.summary,
        body=stored.body,
        confidence=stored.confidence,
        status=status,
        review_due=stored.review_due,
        promoted_to_artifact_id=stored.promoted_to_artifact_id,
        expired_at=stored.expired_at,
        created_at=env.created_at,
    )


def read_book(path: Path) -> dict[str, StandingNote]:
    """Return every note an epoch-2 memory ledger holds, by id.

    Args:
        path: The generation's memory ledger; a missing file holds nothing.

    Returns:
        The standing note per id: its latest native revision when it has
        one, else the note its imported rows describe.

    Raises:
        LedgerError: A line is neither a native note nor an imported row,
            or the ledger ends mid-line.
        ValidationError: A line does not read as the shape it claims.
    """
    index_rows: dict[str, tuple[LedgerRecord, ImportedLedgerRow]] = {}
    store_rows: dict[str, tuple[LedgerRecord, ImportedLedgerRow]] = {}
    natives: dict[str, StandingNote] = {}
    for line in effective_records(read_ledger_records(path)):
        if line.payload.get("payload_kind") == MEMORY_NOTE_KIND:
            note = MemoryNote.model_validate(line.payload)
            natives[note.id] = StandingNote(note=note, line=line)
            continue
        imported = ImportedLedgerRow.model_validate(line.payload)
        if imported.source_collection == MEMORY_INDEX_SOURCE:
            index_rows[imported.source_id] = (line, imported)
        elif imported.source_collection == MEMORY_STORE_SOURCE:
            store_rows[imported.source_id] = (line, imported)
        else:
            raise LedgerError(
                f"memory ledger line {line.record_key!r} was imported from "
                f"{imported.source_collection!r}, which holds no memory"
            )
    book: dict[str, StandingNote] = {}
    for mid in sorted({*index_rows, *store_rows, *natives}):
        if mid in natives:
            book[mid] = natives[mid]
            continue
        index_entry, store_entry = index_rows.get(mid), store_rows.get(mid)
        standing = index_entry or store_entry
        assert standing is not None, "every id came from one of the three maps"
        book[mid] = StandingNote(
            note=_imported_note(
                index_entry[1] if index_entry is not None else None,
                store_entry[1] if store_entry is not None else None,
            ),
            line=standing[0],
        )
    logger.debug(f"read_book notes={len(book)} native={len(natives)}")
    return book


def note_line(note: MemoryNote, *, at: datetime, replaces: LedgerRecord | None) -> LedgerRecord:
    """Return the ledger line filing *note*, superseding *replaces* when given.

    Args:
        note: The note, as it stands after this revision.
        at: When the revision is recorded.
        replaces: The line the revision supersedes, or ``None`` for a new note.

    Returns:
        The line, keyed by the note's id.
    """
    return LedgerRecord(
        collection=Epoch2Collection.MEMORY,
        record_key=note.id,
        status=note.status.value,
        recorded_at=at,
        supersedes=None if replaces is None else line_digest(render_ledger_line(replaces)),
        payload=note.model_dump(mode="json"),
    )


def next_note_id(taken: Iterable[str], *, now: datetime) -> str:
    """Return the first free ``MEM-<UTC-date>-<NN>`` id of *now*'s day.

    Raises:
        ValueError: All 99 ids of the day are taken.
    """
    prefix = f"MEM-{now.strftime('%Y%m%d')}-"
    used = {
        int(suffix)
        for mid in taken
        if mid.startswith(prefix) and (suffix := mid.removeprefix(prefix)).isdigit()
    }
    for n in range(1, 100):
        if n not in used:
            return f"{prefix}{n:02d}"
    raise ValueError("memory id allocation saturated for today")


def _passes_age(note: MemoryNote, *, threshold: timedelta, now: datetime) -> str | None:
    """Return why *note* is too young to select, or ``None`` when it is old enough."""
    anchor = note.age_anchor
    if anchor is None:
        return "no-age-anchor"
    if now - anchor < threshold:
        return "younger-than-threshold"
    return None


def select_prunable(
    notes: Mapping[str, MemoryNote],
    *,
    age_days: int,
    status_filter: MemoryStatus,
    scope_id: str | None,
    now: datetime,
) -> NoteSelection:
    """Select the notes a prune retires.

    Args:
        notes: The notes, by id.
        age_days: The age a note must reach; ``>= 0``.
        status_filter: Only notes in this status are pruned.
        scope_id: Only notes of this scope are pruned, when given.
        now: The instant ages are measured at.

    Returns:
        The selection. An already-pruned note is passed over, so a second
        prune is a no-op rather than an error.
    """
    threshold = timedelta(days=age_days)
    selected: list[str] = []
    reasons: dict[str, str] = {}
    for mid, note in notes.items():
        if scope_id is not None and note.scope_id != scope_id:
            reasons[mid] = "scope-mismatch"
        elif note.status == MemoryStatus.PRUNED:
            reasons[mid] = "already-pruned"
        elif note.status != status_filter:
            reasons[mid] = f"status={note.status.value}"
        elif (reason := _passes_age(note, threshold=threshold, now=now)) is not None:
            reasons[mid] = reason
        else:
            selected.append(mid)
    return NoteSelection(selected=sorted(selected), skipped=sorted(reasons), reasons=reasons)


def select_archivable(
    notes: Mapping[str, MemoryNote], *, threshold_days: int, now: datetime
) -> NoteSelection:
    """Select the stale working-tier notes old enough to archive.

    Args:
        notes: The notes, by id.
        threshold_days: The age a note must reach; ``>= 0``.
        now: The instant ages are measured at.

    Returns:
        The selection.
    """
    threshold = timedelta(days=threshold_days)
    selected: list[str] = []
    reasons: dict[str, str] = {}
    for mid, note in notes.items():
        if note.status != MemoryStatus.STALE:
            reasons[mid] = f"status={note.status.value}"
        elif note.tier != MemoryTier.WORKING:
            reasons[mid] = f"tier={note.tier.value}"
        elif (reason := _passes_age(note, threshold=threshold, now=now)) is not None:
            reasons[mid] = reason
        else:
            selected.append(mid)
    return NoteSelection(selected=sorted(selected), skipped=sorted(reasons), reasons=reasons)


__all__ = [
    "MEMORY_INDEX_SOURCE",
    "NoteSelection",
    "StandingNote",
    "generation_memory_ledger",
    "next_note_id",
    "note_line",
    "notes_from_state",
    "read_book",
    "select_archivable",
    "select_prunable",
]
