"""``memory.*`` native verbs: write memory notes to the generation's memory ledger.

Every verb reads the standing notes under the tree's locks, decides the
revisions, and commits each as one ledger line that supersedes the line it
revises, so a note is never edited in place and the firehose carries one
row per revision. A verb that selects nothing writes nothing.

The verbs are behind the epoch-2 fence: on a tree that is not in epoch 2
they are refused before a byte is read, and the frozen epoch-1 document is
never written. A promotion still reads its source record from the frozen
epoch-1 store, because that is where an epoch-1 record's text lives; it
only reads it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.enums import Confidence, MemoryStatus, MemoryTier, StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.models import State
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.kinds.memory import MemoryNote
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.memory.book import (
    StandingNote,
    next_note_id,
    note_line,
    read_book,
    select_archivable,
    select_prunable,
)
from eawf.platform.memory.promotion import PromotionError, extract_body, load_source
from eawf.platform.memory.store import summary_text
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import native_mutator, native_params

logger = logging.getLogger(__name__)

MEMORY_ADD_METHOD: Final = "memory.add"
MEMORY_PROMOTE_METHOD: Final = "memory.promote"
MEMORY_LINK_METHOD: Final = "memory.link"
MEMORY_PRUNE_METHOD: Final = "memory.prune"
MEMORY_GC_METHOD: Final = "memory.gc"
MEMORY_TIER_METHOD: Final = "memory.tier"

#: The frozen epoch-1 document beside the generations; it names the
#: project of a tree born at epoch 2, whose generation imported none.
_FROZEN_DOCUMENT: Final = "state.json"

_Text = Annotated[str, Field(min_length=1)]


class MemoryAdd(BaseModel):
    """What ``memory.add`` is asked for."""

    model_config = ConfigDict(extra="forbid")

    scope_id: _Text
    title: _Text
    body: str
    confidence: Confidence = Confidence.MEDIUM


class MemoryPromote(BaseModel):
    """What ``memory.promote`` is asked for: a store record to copy into a new note."""

    model_config = ConfigDict(extra="forbid")

    session: _Text
    source: _Text
    source_kind: StoreKind
    scope_id: str | None = None
    confidence: Confidence = Confidence.MEDIUM


class MemoryLink(BaseModel):
    """What ``memory.link`` is asked for: a note to retire into a standing decision."""

    model_config = ConfigDict(extra="forbid")

    session: _Text
    source: _Text
    artifact_id: _Text


class MemoryPrune(BaseModel):
    """What ``memory.prune`` is asked for."""

    model_config = ConfigDict(extra="forbid")

    age_days: Annotated[int, Field(ge=0)]
    status: MemoryStatus
    scope_id: str | None = None


class MemoryGc(BaseModel):
    """What ``memory.gc`` is asked for."""

    model_config = ConfigDict(extra="forbid")

    threshold_days: Annotated[int, Field(ge=0)]


class MemoryTierSet(BaseModel):
    """What ``memory.tier`` is asked for."""

    model_config = ConfigDict(extra="forbid")

    id: _Text
    tier: MemoryTier


def _refusal(code: str, message: str) -> DaemonValidationError:
    """Return the validation refusal the CLI routes on by *code*."""
    return DaemonValidationError(f"validation_failed: {code}: {message}")


def project_subject(context: Epoch2RootContext) -> str:
    """Return the project URN a project-wide session locks and its events are scoped to.

    Raises:
        DaemonValidationError: Neither the generation nor the frozen
            document names exactly one project, or the frozen document
            does not validate.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None and authority.generation_id is not None
    document = read_document(
        authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    )
    projects = sorted(document_rows(document, Epoch2Collection.PROJECT))
    code = projects[0] if len(projects) == 1 else None
    frozen = context.identity.tree_root / _FROZEN_DOCUMENT
    if code is None and frozen.exists():
        try:
            project = State.model_validate_json(frozen.read_bytes()).project
        except ValidationError as error:
            raise _refusal(
                "project_unresolved", f"the frozen document does not validate: {error}"
            ) from error
        code = project.code if project is not None else None
    if code is None:
        raise _refusal(
            "project_unresolved",
            "the tree names no single project to file the record under",
        )
    return format_qualified_urn(
        workspace_key=code,
        project_key=code,
        repository_key=None,
        kind=EntityKind.PROJECT,
        entity_key=code,
    )


def _book(session: RootSession) -> dict[str, StandingNote]:
    return read_book(session.ledger_path(Epoch2Collection.MEMORY))


def _commit(
    session: RootSession, book: dict[str, StandingNote], notes: Iterable[MemoryNote], at: datetime
) -> None:
    """Commit each note as a line superseding the one it revises."""
    for note in notes:
        standing = book.get(note.id)
        commit_ledger_append(
            session, note_line(note, at=at, replaces=None if standing is None else standing.line)
        )


def _standing(book: dict[str, StandingNote], mem_id: str) -> MemoryNote:
    """Return the standing note *mem_id* names.

    Raises:
        DaemonValidationError: No note stands under that id.
    """
    standing = book.get(mem_id)
    if standing is None:
        raise _refusal("memory_not_found", f"memory entry not found: {mem_id}")
    return standing.note


def add_note(context: Epoch2RootContext, args: MemoryAdd, *, now: datetime) -> MemoryNote:
    """File a new note under the first free id of the day and return it."""
    with context.session([project_subject(context)]) as session:
        book = _book(session)
        note = MemoryNote(
            id=next_note_id(book, now=now),
            scope_id=args.scope_id,
            title=args.title,
            summary=summary_text(args.title, args.body),
            body=args.body,
            confidence=args.confidence,
            created_at=now,
        )
        _commit(session, book, (note,), now)
    logger.info(f"add_note id={note.id} scope={note.scope_id} confidence={note.confidence.value}")
    return note


def promote_note(context: Epoch2RootContext, args: MemoryPromote, *, now: datetime) -> MemoryNote:
    """File a new note copied from a record of the frozen epoch-1 store.

    Raises:
        DaemonValidationError: The store or the record is missing.
    """
    frozen_store = store_path(context.identity.tree_root / _FROZEN_DOCUMENT, args.source_kind)
    try:
        source = load_source(frozen_store, args.source)
    except PromotionError as error:
        raise _refusal("memory_source_not_found", str(error)) from error
    title = source.summary or args.source
    body = extract_body(source)
    with context.session([project_subject(context)]) as session:
        book = _book(session)
        note = MemoryNote(
            id=next_note_id(book, now=now),
            scope_id=args.scope_id or source.scope_id or "unscoped",
            title=title,
            summary=summary_text(title, body),
            body=body,
            confidence=args.confidence,
            source_ref=f"{args.source_kind.value}/{args.source}",
            created_at=now,
        )
        _commit(session, book, (note,), now)
    logger.info(f"promote_note source={args.source} memory={note.id} session={args.session}")
    return note


def link_note(context: Epoch2RootContext, args: MemoryLink, *, now: datetime) -> MemoryNote:
    """Retire a note into a decision the decision ledger already holds.

    The decision is filed on its own, through the decision verb, because a
    decision carries its options and evidence and a note carries neither.

    Raises:
        DaemonValidationError: The note is missing or pruned, or no
            standing decision holds the key.
    """
    with context.session([project_subject(context)]) as session:
        book = _book(session)
        note = _standing(book, args.source)
        if note.status == MemoryStatus.PRUNED:
            raise _refusal(
                "memory_state_illegal",
                f"memory entry {args.source!r} is PRUNED — refusing to promote a tombstone",
            )
        decisions = effective_records(
            read_ledger_records(session.ledger_path(Epoch2Collection.DECISION))
        )
        if not any(line.record_key == args.artifact_id for line in decisions):
            raise _refusal(
                "memory_artifact_not_found",
                f"the decision ledger holds no decision {args.artifact_id!r}",
            )
        linked = note.model_copy(
            update={
                "status": MemoryStatus.SUPERSEDED,
                "promoted_to_artifact_id": args.artifact_id,
            }
        )
        _commit(session, book, (linked,), now)
    logger.info(
        f"link_note memory={args.source} artifact={args.artifact_id} session={args.session}"
    )
    return linked


def prune_notes(context: Epoch2RootContext, args: MemoryPrune, *, now: datetime) -> dict[str, Any]:
    """Retire the selected notes as PRUNED and return the selection."""
    with context.session([project_subject(context)]) as session:
        book = _book(session)
        selection = select_prunable(
            {mid: s.note for mid, s in book.items()},
            age_days=args.age_days,
            status_filter=args.status,
            scope_id=args.scope_id,
            now=now,
        )
        retired = [
            book[mid].note.model_copy(update={"status": MemoryStatus.PRUNED, "expired_at": now})
            for mid in selection.selected
        ]
        _commit(session, book, retired, now)
    logger.info(f"prune_notes pruned={len(selection.selected)} skipped={len(selection.skipped)}")
    return {"pruned_ids": selection.selected, "skipped_ids": selection.skipped}


def gc_notes(context: Epoch2RootContext, args: MemoryGc, *, now: datetime) -> dict[str, Any]:
    """Move the selected notes to the archival tier and return the selection."""
    with context.session([project_subject(context)]) as session:
        book = _book(session)
        selection = select_archivable(
            {mid: s.note for mid, s in book.items()},
            threshold_days=args.threshold_days,
            now=now,
        )
        archived = [
            book[mid].note.model_copy(update={"tier": MemoryTier.ARCHIVAL})
            for mid in selection.selected
        ]
        _commit(session, book, archived, now)
    logger.info(f"gc_notes archived={len(selection.selected)} skipped={len(selection.skipped)}")
    return {"archived_ids": selection.selected, "skipped_ids": selection.skipped}


def tier_note(
    context: Epoch2RootContext, args: MemoryTierSet, *, now: datetime
) -> tuple[MemoryNote, MemoryTier]:
    """Set one note's tier and return it beside the tier it left."""
    with context.session([project_subject(context)]) as session:
        book = _book(session)
        note = _standing(book, args.id)
        moved = note.model_copy(update={"tier": args.tier})
        _commit(session, book, (moved,), now)
    logger.info(f"tier_note id={args.id} tier={note.tier.value}->{args.tier.value}")
    return moved, note.tier


def _root(ctx: MethodContext, authority: RootAuthority) -> Epoch2RootContext:
    return ctx.native_root_context(authority.root)


@native_mutator(MEMORY_ADD_METHOD)
async def _add(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """File a new memory note."""
    args = native_params(MemoryAdd, params)
    note = await asyncio.to_thread(add_note, _root(ctx, authority), args, now=datetime.now(UTC))
    return {
        "id": note.id,
        "scope_id": note.scope_id,
        "confidence": note.confidence.value,
        "summary": note.summary,
    }


@native_mutator(MEMORY_PROMOTE_METHOD)
async def _promote(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """File a new memory note copied from a store record."""
    args = native_params(MemoryPromote, params)
    note = await asyncio.to_thread(promote_note, _root(ctx, authority), args, now=datetime.now(UTC))
    return {"id": note.id, "scope_id": note.scope_id, "source_id": args.source}


@native_mutator(MEMORY_LINK_METHOD)
async def _link(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Retire a memory note into a standing decision."""
    args = native_params(MemoryLink, params)
    note = await asyncio.to_thread(link_note, _root(ctx, authority), args, now=datetime.now(UTC))
    return {
        "id": note.id,
        "scope_id": note.scope_id,
        "promoted_to_artifact_id": note.promoted_to_artifact_id,
    }


@native_mutator(MEMORY_PRUNE_METHOD)
async def _prune(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Retire the notes a prune selects."""
    args = native_params(MemoryPrune, params)
    return await asyncio.to_thread(prune_notes, _root(ctx, authority), args, now=datetime.now(UTC))


@native_mutator(MEMORY_GC_METHOD)
async def _gc(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Archive the notes a GC pass selects."""
    args = native_params(MemoryGc, params)
    return await asyncio.to_thread(gc_notes, _root(ctx, authority), args, now=datetime.now(UTC))


@native_mutator(MEMORY_TIER_METHOD)
async def _tier(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Set one note's tier."""
    args = native_params(MemoryTierSet, params)
    note, prior = await asyncio.to_thread(
        tier_note, _root(ctx, authority), args, now=datetime.now(UTC)
    )
    return {
        "id": note.id,
        "scope_id": note.scope_id,
        "tier": note.tier.value,
        "prior_tier": prior.value,
    }


__all__ = [
    "MEMORY_ADD_METHOD",
    "MEMORY_GC_METHOD",
    "MEMORY_LINK_METHOD",
    "MEMORY_PROMOTE_METHOD",
    "MEMORY_PRUNE_METHOD",
    "MEMORY_TIER_METHOD",
    "add_note",
    "gc_notes",
    "link_note",
    "project_subject",
    "promote_note",
    "prune_notes",
    "tier_note",
]
