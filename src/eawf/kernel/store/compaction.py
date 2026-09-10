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
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.epoch2.task import TERMINAL_TASK_STATUSES, Task
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.index import regenerate_indexes, write_ledger_index
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
    truncate_torn_tail,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import (
    LEDGER_COLLECTIONS,
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


def read_document(state_path: Path) -> dict[str, Any]:
    """Read the tree's document.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The decoded document.

    Raises:
        FileNotFoundError: The document does not exist.
        ValueError: The document is not a JSON object, so it holds no
            collections to compact out of.
    """
    decoded = json.loads(state_path.read_text("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"{state_path} holds a {type(decoded).__name__}, not a JSON object")
    return decoded


def write_document(state_path: Path, document: dict[str, Any]) -> None:
    """Replace the tree's document atomically.

    Args:
        state_path: Path to the tree's ``state.json``.
        document: The whole document to leave behind.

    Raises:
        TypeError: The document holds a value ``json`` cannot encode.
    """
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    tmp = state_path.with_name(f"{state_path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("wb") as fh:
            fh.write(f"{payload}\n".encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, state_path)
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

    Repairs a torn ledger tail, drops each document row whose record the
    ledger already committed, and regenerates the derived indexes.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        What was repaired.

    Raises:
        FileNotFoundError: The document does not exist.
    """
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
    "CompactionCrashError",
    "CompactionCrashPoint",
    "CompactionResult",
    "RecordInTwoPlacesError",
    "RecordLocation",
    "RecoveryReport",
    "compact_terminal_record",
    "compact_terminal_task",
    "document_rows",
    "locate_record",
    "read_document",
    "recover_store_tree",
    "write_document",
]
