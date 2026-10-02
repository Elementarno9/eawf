"""Moving a terminal record out of the document and into its ledger.

The transaction has three durable writes and they are ordered so that a
crash between any two of them leaves the record recoverable to exactly
one canonical place. The ledger is appended first, the document is
rewritten second, and the derived index is rebuilt last.

That order makes the ledger the winner. A crash after the append leaves
the record in both places, and recovery finishes the move by dropping the
document row -- never the other way round, because deleting a committed
ledger line is exactly what the append-only tier forbids. A crash before
the append leaves the record only in the document, which is the state the
transaction started from. The index is derived, so a crash before it is
rebuilt costs a regeneration and nothing else.

Nothing here writes a live tree implicitly: the caller passes the
``state.json`` it means, so a staging tree and the real one are the same
code path with different arguments.

The document is one object to every reader and two files on disk. What
every claim, start and completion rewrites lives in the machine-local
status projection beside the document, so a Task runs from claim to
completion without touching a tracked file; everything a fresh clone
needs to plan, resolve and number work stays in the committed
``state.json``. The split, field by field:

- committed: every row of every collection but ``run``; each Task row's
  definition (every field not listed below); a Task's status and status
  fields while it is ``DRAFT``, ``DEFERRED``, ``DROPPED``, ``PLANNED`` or
  terminal; and ``canonical_sequence`` as of the last committed write.
- local: the ``run`` collection whole; for a Task in an in-flight status
  (``CLAIMED``, ``RUNNING``, ``READY_TO_INTEGRATE``) its
  :data:`IN_FLIGHT_TASK_FIELDS`, while the committed row keeps the
  status fields it last held; and the live ``canonical_sequence``.

:func:`read_document` lays each local Task entry over its committed row,
field by field, and takes the larger of the two sequences. A Task
claimed before the committed file ever held it in a planning status is
committed as ``PLANNED``, the status a claim leaves.

A write that changes both files is made atomic by digest. The projection
names the digest of the committed bytes it belongs to, and it is written
first, carrying the projection it replaces under the digest of the
committed bytes that one belonged to. The committed file is replaced
second. A crash before the first replace leaves the old pair; a crash
between them leaves the old committed file, which the old projection it
carries still matches; after the second, the new pair. So a reader sees
exactly the document before or the document after, never a mix. A
committed file that matches neither digest moved under the projection
from outside -- a checkout or a pull -- and the projection is laid over it
as it stands, dropping each Task entry whose committed row is gone or
already terminal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.epoch2.task import TERMINAL_TASK_STATUSES, Task, TaskStatus
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.index import regenerate_indexes, write_ledger_index
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    guarded_ledger_write,
    line_digest,
    read_ledger_records,
    render_ledger_line,
    truncate_torn_tail,
)
from eawf.kernel.store.paths import (
    LEDGER_DIRNAME,
    LOCAL_DIRNAME,
    ledger_path,
    seed_ledger_path,
    status_projection_path,
)
from eawf.kernel.store.tiers import (
    LEDGER_COLLECTIONS,
    STATUS_PROJECTION_COLLECTIONS,
    Epoch2Collection,
    StorageTier,
    tier_for,
)

logger = logging.getLogger(__name__)


class CompactionCrashPoint(StrEnum):
    """The durable boundaries of the transaction, as fault-injection points.

    Each names the instant *after* which the process is made to die, so a
    test can leave a genuinely half-written tree behind and then assert
    what recovery makes of it.
    """

    BEFORE_LEDGER_APPEND = "before_ledger_append"
    AFTER_LEDGER_APPEND = "after_ledger_append"
    AFTER_DOCUMENT_REWRITE = "after_document_rewrite"
    AFTER_INDEX_UPDATE = "after_index_update"


class CompactionCrashError(RuntimeError):
    """The injected crash fired at one of the transaction's boundaries."""


class RecordLocation(StrEnum):
    """Which canonical tier currently holds a record."""

    DOCUMENT = "document"
    LEDGER = "ledger"
    ABSENT = "absent"


class RecordInTwoPlacesError(RuntimeError):
    """A record is in the document and the ledger at once."""


class CompactionResult(BaseModel):
    """What one completed compaction wrote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: Epoch2Collection
    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    ledger_offset: Annotated[int, Field(ge=0)]
    line_digest: Sha256DigestStr
    document_rows_remaining: Annotated[int, Field(ge=0)]


class RecoveryReport(BaseModel):
    """What recovery had to repair in a tree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repaired_ledgers: tuple[Epoch2Collection, ...] = ()
    document_rows_dropped: tuple[str, ...] = ()
    regenerated_indexes: tuple[Epoch2Collection, ...] = ()


#: The Task statuses whose status fields live in the status projection.
IN_FLIGHT_TASK_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.READY_TO_INTEGRATE}
)

#: The Task fields an in-flight Task keeps local: its status, the lease and
#: the record's own revision stamp. Every other field is its definition.
IN_FLIGHT_TASK_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "status",
        "active_run_ref",
        "claimed_by",
        "first_claimed_at",
        "integrated_binding",
        "revision",
        "updated_at",
    }
)

#: The document key of the workspace-global high-water mark.
CANONICAL_SEQUENCE_KEY: Final = "canonical_sequence"

_TASK_KEY: Final = Epoch2Collection.TASK.value
_IN_FLIGHT_VALUES: Final = frozenset(status.value for status in IN_FLIGHT_TASK_STATUSES)
_TERMINAL_VALUES: Final = frozenset(status.value for status in TERMINAL_TASK_STATUSES)
_LOCAL_COLLECTIONS: Final = frozenset(item.value for item in STATUS_PROJECTION_COLLECTIONS)
#: The keys a projection written before Task definitions were committed
#: held whole, replacing the committed file's.
_PRE_DEFINITION_KEYS: Final = frozenset({_TASK_KEY, *_LOCAL_COLLECTIONS, CANONICAL_SEQUENCE_KEY})


class StatusProjection(BaseModel):
    """The status projection file, bound to the committed bytes it completes.

    Attributes:
        base: The sha256 hex digest of the committed file this belongs to.
        status: The local half of the document: the ``run`` collection, the
            in-flight Task entries under ``task`` and the live sequence.
        previous: The projection this one replaced, under the digest of the
            committed file that one belonged to, while the committed file
            may still be that one.
    """

    model_config = ConfigDict(extra="forbid")

    base: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    status: dict[str, Any]
    previous: PreviousProjection | None = None


class PreviousProjection(BaseModel):
    """The projection a two-file write replaced, kept until the write lands.

    Attributes:
        base: The digest of the committed file the replaced projection
            belonged to.
        projection: That projection as it read against that file: a
            :class:`StatusProjection` without a ``previous``, a projection
            from before Task definitions were committed, or ``None`` when
            the committed file read alone.
    """

    model_config = ConfigDict(extra="forbid")

    base: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    projection: StatusProjection | dict[str, Any] | None


StatusProjection.model_rebuild()


def read_document(state_path: Path) -> dict[str, Any]:
    """Read the tree's document, its status projection merged in.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The decoded document: the committed file with the projection that
        belongs to it laid over, or the committed file alone when there
        is no projection.

    Raises:
        FileNotFoundError: The document does not exist.
        ValueError: The document or the projection is not a JSON object,
            so it holds no collections to compact out of.
        pydantic.ValidationError: The projection is not a status projection.
    """
    raw = state_path.read_bytes()
    committed = _decode_object(raw, state_path)
    return _merged(committed, _read_projection(state_path), _digest(raw))


def write_document(state_path: Path, document: dict[str, Any]) -> None:
    """Replace the tree's document atomically, committed and local parts apart.

    The projection is written first, carrying the one it replaces, and
    the committed file second, so a crash anywhere reads as the document
    before or the document after (see the module docstring). The
    committed file is left untouched when its bytes would not change.

    Args:
        state_path: Path to the tree's ``state.json``.
        document: The whole document to leave behind.

    Raises:
        TypeError: The document holds a value ``json`` cannot encode.
        LedgerAppendOnlyError: The tree's local Task ledger is not an
            extension of its committed one.
    """
    settle_status_ledgers(state_path)
    old_raw = state_path.read_bytes() if state_path.is_file() else None
    old_committed = {} if old_raw is None else _decode_object(old_raw, state_path)
    committed, status = _split(document, old_committed)
    payload = _render(committed)
    projection = status_projection_path(state_path)
    projection.parent.mkdir(parents=True, exist_ok=True)
    if payload == old_raw:
        _write_object(
            projection, _render_projection(StatusProjection(base=_digest(payload), status=status))
        )
        return
    previous = None
    if old_raw is not None:
        old_base = _digest(old_raw)
        previous = PreviousProjection(
            base=old_base, projection=_settled(_read_projection(state_path), old_base)
        )
    _write_object(
        projection,
        _render_projection(
            StatusProjection(base=_digest(payload), status=status, previous=previous)
        ),
    )
    _write_object(state_path, payload)


def settle_status_ledgers(state_path: Path) -> tuple[Epoch2Collection, ...]:
    """Bring the tree's ledgers to their tiers: the Task ledger committed, Runs local.

    A Task ledger kept under ``local/ledger/`` by a tree written before
    Task definitions were committed is the committed ledger plus the lines
    appended since, so it replaces the committed one and is removed. A
    Run ledger the tree committed before the split, or a clone's checkout
    of one, is the history the local Run ledger starts from, so it is
    copied once and never written again.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The collections whose ledger moved or was seeded, in name order.

    Raises:
        LedgerAppendOnlyError: The local Task ledger does not extend the
            committed one, so replacing it would drop committed lines.
    """
    settled: list[Epoch2Collection] = []
    task_ledger = ledger_path(state_path, Epoch2Collection.TASK)
    stranded = state_path.parent / LOCAL_DIRNAME / LEDGER_DIRNAME / task_ledger.name
    if stranded.is_file():
        content = stranded.read_bytes()
        if not task_ledger.is_file() or task_ledger.read_bytes() != content:
            guarded_ledger_write(task_ledger, content)
        stranded.unlink()
        settled.append(Epoch2Collection.TASK)
    for collection in sorted(STATUS_PROJECTION_COLLECTIONS):
        local, seed = ledger_path(state_path, collection), seed_ledger_path(state_path, collection)
        if local.exists() or not seed.is_file():
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, local)
        settled.append(collection)
    if settled:
        logger.info(f"settle_status_ledgers state_path={state_path} settled={len(settled)}")
    return tuple(sorted(settled))


def migrate_status_projection(state_path: Path) -> bool:
    """Bring a tree written under an earlier split to the current one.

    Covers a document from before any split, whose Run rows and in-flight
    Task status sit in the committed file, and one from the split that
    kept whole Task rows and the sequence local, whose committed file
    lacks the Task definitions. Idempotent: a current tree is left as is.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        ``True`` when the document was rewritten, ``False`` when it was
        already current.

    Raises:
        FileNotFoundError: The document does not exist.
        LedgerAppendOnlyError: The local Task ledger does not extend the
            committed one.
    """
    settle_status_ledgers(state_path)
    raw = state_path.read_bytes()
    projection = _read_projection(state_path)
    document = _merged(_decode_object(raw, state_path), projection, _digest(raw))
    committed, _ = _split(document, _decode_object(raw, state_path))
    if not isinstance(projection, dict) and _render(committed) == raw:
        return False
    write_document(state_path, document)
    logger.info(f"migrate_status_projection state_path={state_path}")
    return True


def _split(
    document: dict[str, Any], old_committed: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return *document*'s committed half and its local half.

    Args:
        document: The whole document to write.
        old_committed: The committed file as it stands, whose Task rows
            supply the status an in-flight Task's committed row keeps and
            whose sequence is kept while nothing else committed changes.

    Returns:
        ``(committed, status)`` per the module docstring's field split.
    """
    committed = {
        key: value
        for key, value in document.items()
        if key not in _LOCAL_COLLECTIONS and key != CANONICAL_SEQUENCE_KEY
    }
    status = {key: value for key, value in document.items() if key in _LOCAL_COLLECTIONS}
    tasks = document.get(_TASK_KEY)
    if isinstance(tasks, dict):
        bases = old_committed.get(_TASK_KEY)
        bases = bases if isinstance(bases, dict) else {}
        committed_tasks: dict[str, Any] = {}
        in_flight: dict[str, Any] = {}
        for key, row in tasks.items():
            if not isinstance(row, dict) or row.get("status") not in _IN_FLIGHT_VALUES:
                committed_tasks[key] = row
                continue
            in_flight[key] = {field: row[field] for field in IN_FLIGHT_TASK_FIELDS if field in row}
            committed_tasks[key] = _committed_task(row, bases.get(key))
        committed[_TASK_KEY] = committed_tasks
        if in_flight:
            status[_TASK_KEY] = in_flight
    if CANONICAL_SEQUENCE_KEY in document:
        sequence = document[CANONICAL_SEQUENCE_KEY]
        status[CANONICAL_SEQUENCE_KEY] = sequence
        kept = old_committed.get(CANONICAL_SEQUENCE_KEY)
        unchanged = committed == {
            key: value for key, value in old_committed.items() if key != CANONICAL_SEQUENCE_KEY
        }
        # A status-only write keeps the committed copy, so the committed file
        # stays byte-identical, unless the copy is ahead of the live value.
        if not unchanged or (kept is not None and kept > sequence):
            committed[CANONICAL_SEQUENCE_KEY] = sequence
        elif kept is not None:
            committed[CANONICAL_SEQUENCE_KEY] = kept
    return committed, status


def _committed_task(row: dict[str, Any], base: Any) -> dict[str, Any]:
    """Return the committed row of an in-flight Task.

    Its definition is *row*'s; its status fields are the committed base
    row's when that row holds a planning status, and otherwise *row*'s
    own as a ``PLANNED`` row with the lease cleared.
    """
    definition = {key: value for key, value in row.items() if key not in IN_FLIGHT_TASK_FIELDS}
    if isinstance(base, dict) and base.get("status") not in _IN_FLIGHT_VALUES:
        held = {key: value for key, value in base.items() if key in IN_FLIGHT_TASK_FIELDS}
        return definition | held
    held = {key: value for key, value in row.items() if key in IN_FLIGHT_TASK_FIELDS}
    held.pop("claimed_by", None)
    held["status"] = TaskStatus.PLANNED.value
    if "active_run_ref" in held:
        held["active_run_ref"] = None
    return definition | held


def _merged(
    committed: dict[str, Any],
    projection: StatusProjection | dict[str, Any] | None,
    digest: str,
) -> dict[str, Any]:
    """Return *committed* with the projection that belongs to it laid over.

    Args:
        committed: The committed file, decoded.
        projection: The projection as read: current, from before Task
            definitions were committed, or absent.
        digest: The digest of the committed file's bytes.
    """
    if projection is None:
        return committed
    if isinstance(projection, dict):
        kept = {key: value for key, value in committed.items() if key not in _PRE_DEFINITION_KEYS}
        return kept | projection
    previous = projection.previous
    if projection.base != digest and previous is not None and previous.base == digest:
        return _merged(committed, previous.projection, digest)
    return _overlay(committed, projection.status)


def _overlay(committed: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """Lay one projection's local half over the committed file."""
    document = {key: value for key, value in committed.items() if key not in _LOCAL_COLLECTIONS}
    document.update({key: value for key, value in status.items() if key in _LOCAL_COLLECTIONS})
    sequences = [
        value
        for value in (committed.get(CANONICAL_SEQUENCE_KEY), status.get(CANONICAL_SEQUENCE_KEY))
        if value is not None
    ]
    if sequences:
        document[CANONICAL_SEQUENCE_KEY] = max(sequences)
    rows = document.get(_TASK_KEY)
    entries = status.get(_TASK_KEY)
    if isinstance(rows, dict) and isinstance(entries, dict):
        merged = dict(rows)
        for key, entry in entries.items():
            row = rows.get(key)
            if not isinstance(row, dict) or row.get("status") in _TERMINAL_VALUES:
                continue
            definition = {
                name: value for name, value in row.items() if name not in IN_FLIGHT_TASK_FIELDS
            }
            merged[key] = definition | entry
        document[_TASK_KEY] = merged
    return document


def _settled(
    projection: StatusProjection | dict[str, Any] | None, digest: str
) -> StatusProjection | dict[str, Any] | None:
    """Return the projection that reads against the committed file *digest* names.

    The result carries no ``previous``, so a projection that keeps it as
    its own ``previous`` reads the same as it did, one level deep.
    """
    if projection is None or isinstance(projection, dict):
        return projection
    previous = projection.previous
    if projection.base != digest and previous is not None and previous.base == digest:
        return previous.projection
    return StatusProjection(base=projection.base, status=projection.status)


def _read_projection(state_path: Path) -> StatusProjection | dict[str, Any] | None:
    """Return the tree's status projection, ``None`` when it has none.

    A projection with no ``base`` was written before Task definitions were
    committed and is returned as the raw object it is.

    Raises:
        ValueError: The projection is not a JSON object.
        pydantic.ValidationError: It names a base but is not a status projection.
    """
    path = status_projection_path(state_path)
    if not path.is_file():
        return None
    decoded = _read_object(path)
    if "base" not in decoded:
        return decoded
    return StatusProjection.model_validate(decoded)


def _render_projection(projection: StatusProjection) -> bytes:
    """Return the canonical bytes of *projection*."""
    return _render(projection.model_dump(mode="json"))


def _digest(raw: bytes) -> str:
    """Return the sha256 hex digest of a committed file's bytes."""
    return hashlib.sha256(raw).hexdigest()


def _decode_object(raw: bytes, path: Path) -> dict[str, Any]:
    """Return the JSON object *raw*, read from *path*, holds.

    Raises:
        ValueError: The bytes hold something other than a JSON object.
    """
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} holds a {type(decoded).__name__}, not a JSON object")
    return decoded


def _read_object(path: Path) -> dict[str, Any]:
    """Return the JSON object *path* holds.

    Raises:
        FileNotFoundError: The file does not exist.
        ValueError: The file holds something other than a JSON object.
    """
    return _decode_object(path.read_bytes(), path)


def _render(document: dict[str, Any]) -> bytes:
    """Return the canonical bytes of *document*."""
    return f"{json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)}\n".encode()


def _write_object(path: Path, payload: bytes) -> None:
    """Replace *path* with *payload* atomically."""
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def document_rows(document: dict[str, Any], collection: Epoch2Collection) -> dict[str, Any]:
    """Return the mutable row map *collection* holds in *document*.

    Args:
        document: The decoded document.
        collection: The collection to address.

    Returns:
        The collection's rows, keyed by public key. A collection the
        document does not carry reads as empty.

    Raises:
        ValueError: The document holds something other than an object
            under the collection key.
    """
    rows = document.get(collection.value)
    if rows is None:
        return {}
    if not isinstance(rows, dict):
        raise ValueError(
            f"document collection {collection.value!r} holds a {type(rows).__name__}, not an object"
        )
    return rows


def _crash_if(crash_at: CompactionCrashPoint | None, point: CompactionCrashPoint) -> None:
    """Raise the injected crash when *crash_at* names this boundary."""
    if crash_at is point:
        raise CompactionCrashError(f"injected crash at {point.value}")


def locate_record(
    state_path: Path,
    *,
    collection: Epoch2Collection,
    record_key: str,
) -> RecordLocation:
    """Return the one canonical tier holding *record_key*.

    Args:
        state_path: Path to the tree's ``state.json``.
        collection: The collection to look in.
        record_key: The public key to locate.

    Returns:
        Whether the record is in the document, in the ledger, or in
        neither.

    Raises:
        RecordInTwoPlacesError: Both tiers hold it, which means a
            compaction was interrupted and recovery has not run.
        LedgerTornTailError: The ledger ends mid-line.
    """
    in_document = record_key in document_rows(read_document(state_path), collection)
    in_ledger = any(
        item.record_key == record_key
        for item in read_ledger_records(ledger_path(state_path, collection))
    )
    if in_document and in_ledger:
        raise RecordInTwoPlacesError(
            f"{collection.value}/{record_key} is in the document and the ledger"
        )
    if in_ledger:
        return RecordLocation.LEDGER
    if in_document:
        return RecordLocation.DOCUMENT
    return RecordLocation.ABSENT


def compact_terminal_record(
    state_path: Path,
    *,
    record: LedgerRecord,
    crash_at: CompactionCrashPoint | None = None,
) -> CompactionResult:
    """Move one terminal record from the document into its ledger.

    Args:
        state_path: Path to the tree's ``state.json``.
        record: The ledger line the terminal record becomes.
        crash_at: The boundary to die at, for fault injection. Production
            callers leave this unset.

    Returns:
        Where the line landed and how many rows the collection still
        holds in the document.

    Raises:
        ValueError: The record's collection is not declared at the ledger
            tier, or the ledger has already committed the record, which
            makes a second append a duplicate of committed history.
        KeyError: The document does not hold the record, so there is
            nothing to move.
        RecordInTwoPlacesError: An earlier compaction was interrupted and
            recovery has not run.
        CompactionCrashError: The injected crash fired.
    """
    collection = record.collection
    if tier_for(collection) is not StorageTier.LEDGER:
        raise ValueError(
            f"{collection.value!r} is declared at the "
            f"{tier_for(collection).value} tier and never compacts"
        )
    located = locate_record(state_path, collection=collection, record_key=record.record_key)
    if located is RecordLocation.LEDGER:
        raise ValueError(
            f"{collection.value}/{record.record_key} is already committed to the ledger"
        )
    if located is RecordLocation.ABSENT:
        raise KeyError(
            f"document collection {collection.value!r} holds no row {record.record_key!r}"
        )
    document = read_document(state_path)
    rows = document_rows(document, collection)

    _crash_if(crash_at, CompactionCrashPoint.BEFORE_LEDGER_APPEND)
    offset = append_ledger_record(ledger_path(state_path, collection), record)
    _crash_if(crash_at, CompactionCrashPoint.AFTER_LEDGER_APPEND)

    del rows[record.record_key]
    document[collection.value] = rows
    write_document(state_path, document)
    _crash_if(crash_at, CompactionCrashPoint.AFTER_DOCUMENT_REWRITE)

    write_ledger_index(state_path, collection)
    _crash_if(crash_at, CompactionCrashPoint.AFTER_INDEX_UPDATE)

    logger.info(
        f"compact_terminal_record collection={collection.value} "
        f"record_key={record.record_key} offset={offset}"
    )
    return CompactionResult(
        collection=collection,
        record_key=record.record_key,
        ledger_offset=offset,
        line_digest=line_digest(render_ledger_line(record)),
        document_rows_remaining=len(rows),
    )


def compact_terminal_task(
    state_path: Path,
    *,
    task: Task,
    recorded_at: UtcDatetime,
    crash_at: CompactionCrashPoint | None = None,
) -> CompactionResult:
    """Compact a Task that has reached one of its three terminal states.

    Args:
        state_path: Path to the tree's ``state.json``.
        task: The Task to move out of the document.
        recorded_at: When the terminal state was reached.
        crash_at: The boundary to die at, for fault injection. Production
            callers leave this unset.

    Returns:
        Where the line landed.

    Raises:
        ValueError: The Task has not reached a terminal state. A Task
            still in flight is read and rewritten constantly, so
            appending it to an append-only ledger would commit a row that
            is about to change.
        KeyError: The document does not hold the Task.
        CompactionCrashError: The injected crash fired.
    """
    if task.status not in TERMINAL_TASK_STATUSES:
        admitted = ", ".join(sorted(status.value for status in TERMINAL_TASK_STATUSES))
        raise ValueError(f"Task {task.key} is {task.status.value}; compaction admits {admitted}")
    record = LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key=task.key,
        status=task.status.value,
        recorded_at=recorded_at,
        payload=task.model_dump(mode="json"),
    )
    return compact_terminal_record(state_path, record=record, crash_at=crash_at)


def recover_store_tree(state_path: Path) -> RecoveryReport:
    """Finish every interrupted compaction the tree carries.

    Brings a tree written under an earlier split to the current one first
    (:func:`migrate_status_projection`), so the rest of the pass and every
    later write see the current split. Then
    repairs a torn ledger tail, drops each document row whose record the
    ledger already committed, and regenerates the derived indexes.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        What was repaired.

    Raises:
        FileNotFoundError: The document does not exist.
    """
    migrate_status_projection(state_path)
    document = read_document(state_path)
    repaired: list[Epoch2Collection] = []
    dropped: list[str] = []

    for collection in LEDGER_COLLECTIONS:
        ledger = ledger_path(state_path, collection)
        if not ledger.exists():
            continue
        if truncate_torn_tail(ledger):
            repaired.append(collection)
        dropped.extend(_drop_committed_rows(document, collection, ledger))

    if dropped:
        write_document(state_path, document)
    report = RecoveryReport(
        repaired_ledgers=tuple(repaired),
        document_rows_dropped=tuple(dropped),
        regenerated_indexes=regenerate_indexes(state_path),
    )
    logger.info(
        f"recover_store_tree state_path={state_path} repaired={len(repaired)} "
        f"dropped={len(dropped)}"
    )
    return report


def _drop_committed_rows(
    document: dict[str, Any],
    collection: Epoch2Collection,
    ledger: Path,
) -> list[str]:
    """Remove the document rows *ledger* has already committed.

    A superseded line is not a commitment: the record it described was
    corrected, so only the lines that still stand evict a document row.

    Args:
        document: The decoded document, mutated in place.
        collection: The collection being reconciled.
        ledger: The collection's ledger file.

    Returns:
        The ``collection/key`` locators that were dropped, sorted.
    """
    rows = document_rows(document, collection)
    if not rows:
        return []
    standing = effective_records(read_ledger_records(ledger))
    committed = {record.record_key for record in standing}
    doomed = sorted(committed & rows.keys())
    for key in doomed:
        del rows[key]
    if doomed:
        document[collection.value] = rows
    return [f"{collection.value}/{key}" for key in doomed]


__all__ = [
    "CANONICAL_SEQUENCE_KEY",
    "IN_FLIGHT_TASK_FIELDS",
    "IN_FLIGHT_TASK_STATUSES",
    "CompactionCrashError",
    "CompactionCrashPoint",
    "CompactionResult",
    "PreviousProjection",
    "RecordInTwoPlacesError",
    "RecordLocation",
    "RecoveryReport",
    "StatusProjection",
    "compact_terminal_record",
    "compact_terminal_task",
    "document_rows",
    "locate_record",
    "migrate_status_projection",
    "read_document",
    "recover_store_tree",
    "settle_status_ledgers",
    "write_document",
]
