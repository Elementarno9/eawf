"""Read a tree's native entities, without a daemon and without a lock.

The ``list`` and ``show`` verbs of the Task, Milestone, plan revision,
decision and pending action groups read here, so an agent reads a record
as the selected generation stores it instead of parsing the document by
hand. The frozen epoch-1 ``state.json`` is never opened.

A record lives in one of two places. While it is in flight it is a row of
the generation's document, read with its machine-local status projection
laid over; once it ends it compacts into its collection's ledger. The live
document row wins over a compacted line of the same key. A native decision
is only ever filed in its ledger, so it is read through
:func:`~eawf.observability.reflect.decisions.read_native_decisions`.

A row is stored either as a native record or as the wrapper the cutover
imported an epoch-1 record into; a summary reads both through the
projection's own field readers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migration.epoch2.continuation import entity_ref
from eawf.kernel.migration.epoch2.cutover import ROW_PAYLOAD_FIELD
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.row_facts import parent_key_of, stored_fields, title_of
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import latest_rows
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for
from eawf.observability.reflect.decisions import read_native_decisions

logger = logging.getLogger(__name__)


class GenerationUnavailableError(LookupError):
    """The tree selects no native generation, so it holds no native entity to read."""


class EntitySummary(BaseModel):
    """One record as a list row: who it is, where it stands and what it is called.

    Attributes:
        key: The record's public key.
        urn: The record's canonical address; for an imported row the legacy name
            the projection addresses it by; ``None`` when the record states none.
        status: The record's stored status, or ``None`` when it states none.
        title: The record's own title (a Task's is read from its intent when it
            states none), or ``None``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    urn: str | None
    status: str | None
    title: str | None


class TaskSummary(EntitySummary):
    """One Task as a list row, with its priority and where it is filed and due.

    Attributes:
        priority: The Task's stored priority, or ``None``.
        batch_ref: The URN of the Batch it is filed in, or ``None``.
        due_scope: The URN of the scope it is due in, or ``None``.
    """

    priority: str | None
    batch_ref: str | None
    due_scope: str | None


def _document_path(tree_root: Path) -> Path:
    """Return the selected generation's document of the tree at *tree_root*.

    Raises:
        GenerationUnavailableError: The tree is not at authority epoch 2.
    """
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        gap = authority.gap.value if authority.gap is not None else None
        raise GenerationUnavailableError(
            f"tree is at authority epoch {authority.epoch} ({gap}); native entities live at epoch 2"
        )
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def read_standing_rows(tree_root: Path, collection: Epoch2Collection) -> dict[str, dict[str, Any]]:
    """Return every standing row of *collection* in the tree's selected generation.

    Args:
        tree_root: The tree's ``.ea`` directory.
        collection: The collection to read.

    Returns:
        Each row as stored, keyed by its public key: the document's live row
        over the latest compacted ledger line of the same key. A native
        decision is its validated record dumped as it was filed; an imported
        epoch-1 decision is passed over.

    Raises:
        GenerationUnavailableError: The tree is not at authority epoch 2.
        FileNotFoundError: The generation document does not exist.
        ValueError: The document, or a collection in it, is not a JSON object.
        LedgerTornTailError: The collection's ledger ends mid-line.
        pydantic.ValidationError: A ledger line, or a native decision, does not
            satisfy its model.
    """
    document_path = _document_path(tree_root)
    if collection is Epoch2Collection.DECISION:
        rows = {
            decision.key: decision.model_dump(mode="json")
            for decision in read_native_decisions(tree_root)
        }
    else:
        # Only a ledger-tier collection compacts; a document-tier one has no ledger.
        compacted = (
            latest_rows(ledger_path(document_path, collection))
            if tier_for(collection) is StorageTier.LEDGER
            else {}
        )
        rows = {**compacted, **document_rows(read_document(document_path), collection)}
    logger.debug(f"read_standing_rows collection={collection.value} rows={len(rows)}")
    return rows


def _identity(
    collection: Epoch2Collection, key: str, row: Any
) -> tuple[Mapping[str, Any], dict[str, str | None]]:
    """Return the fields *row* states and its summary identity, whatever form it is stored in."""
    fields, status = stored_fields(collection, key, row)
    urn = fields.get("urn")
    if not isinstance(urn, str):
        # An imported wrapper is addressed by the legacy name the projection gives it.
        imported = isinstance(row, dict) and ROW_PAYLOAD_FIELD in row
        urn = entity_ref(collection, key) if imported else None
    return fields, {
        "key": key,
        "urn": urn,
        "status": status,
        "title": title_of(collection, fields),
    }


def summarize(collection: Epoch2Collection, rows: Mapping[str, Any]) -> tuple[EntitySummary, ...]:
    """Return one list row per record of *rows*, in key order.

    Args:
        collection: The collection *rows* were read from.
        rows: The standing rows, keyed by public key.

    Returns:
        One :class:`EntitySummary` per row.
    """
    return tuple(
        EntitySummary.model_validate(_identity(collection, key, rows[key])[1])
        for key in sorted(rows)
    )


def _text(value: Any) -> str | None:
    """Return *value* when it is a non-blank string, else ``None``."""
    return value if isinstance(value, str) and value.strip() else None


def _key_of(urn: str | None) -> str | None:
    """Return the entity key a URN ends in, or ``None`` for no URN."""
    return None if urn is None else urn.rstrip("/").rsplit("/", 1)[-1]


def list_tasks(
    tree_root: Path, *, status: TaskStatus | None, milestone: str | None
) -> tuple[TaskSummary, ...]:
    """Return the Tasks of the tree's selected generation, in key order.

    Args:
        tree_root: The tree's ``.ea`` directory.
        status: Keep only Tasks in this status; ``None`` keeps every status.
        milestone: Keep only Tasks under the Milestone of this key -- due in it,
            or filed in a Batch cut under it; ``None`` keeps every Task.

    Returns:
        One :class:`TaskSummary` per kept Task.

    Raises:
        GenerationUnavailableError: The tree is not at authority epoch 2.
        FileNotFoundError: The generation document does not exist.
        ValueError: The document, or a collection in it, is not a JSON object.
        LedgerTornTailError: A ledger read ends mid-line.
        pydantic.ValidationError: A ledger line does not satisfy its model.
    """
    rows = read_standing_rows(tree_root, Epoch2Collection.TASK)
    batch_milestones: dict[str, str | None] = {}
    if milestone is not None:
        batch_milestones = {
            key: parent_key_of(
                Epoch2Collection.BATCH, stored_fields(Epoch2Collection.BATCH, key, row)[0]
            )
            for key, row in read_standing_rows(tree_root, Epoch2Collection.BATCH).items()
        }
    tasks: list[TaskSummary] = []
    for key in sorted(rows):
        fields, identity = _identity(Epoch2Collection.TASK, key, rows[key])
        task = TaskSummary.model_validate(
            {
                **identity,
                "priority": _text(fields.get("priority")),
                "batch_ref": _text(fields.get("batch_ref")),
                "due_scope": _text(fields.get("due_scope")),
            }
        )
        if status is not None and task.status != status.value:
            continue
        if milestone is not None and milestone not in {
            _key_of(task.due_scope),
            batch_milestones.get(_key_of(task.batch_ref) or ""),
        }:
            continue
        tasks.append(task)
    logger.debug(f"list_tasks rows={len(rows)} kept={len(tasks)}")
    return tuple(tasks)


__all__ = [
    "EntitySummary",
    "GenerationUnavailableError",
    "TaskSummary",
    "list_tasks",
    "read_standing_rows",
    "summarize",
]
