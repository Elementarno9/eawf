"""The memory population, reconciled across the index and the memory store.

Epoch 1 kept a memory note in two places: a summary row in the document's
``memory_index`` and the note itself -- its body, when it was written and
every later revision -- as envelopes in the memory store. The index alone
is not the note, so importing only the index would leave every body behind
in a store nothing in epoch 2 reads. The importer takes both: each index
row travels as it always did, and each store id travels as its latest
envelope, so a reader rebuilds the whole note from the memory ledger.

An id only the store holds is imported rather than lost, and the counts
prove it: the notes are the union of the two id sets, the union is the
index plus the store-only ids, and the memory ledger receives exactly one
line per index row plus one per store id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)

#: The epoch-1 store ledger the memory union reads its second input from.
MEMORY_LEDGER: Final = "memory"

#: The source kind a store envelope is imported under. The store is the
#: second half of the ``memory_index`` population, so the dotted kind
#: counts against that collection's mapping, as a Run minted from a wave
#: entry counts against ``waves``; and it is not the index's own kind, so a
#: note both hold is aliased once under each.
MEMORY_STORE_SOURCE: Final = "memory_index.store"

#: The envelope field that names a store row's note.
STORE_ID_FIELD: Final = "id"


class MemoryUnionCensus(StrictMigrationModel):
    """The memory population, counted once per note id.

    Attributes:
        document_rows: Distinct ids in the ``memory_index`` collection.
        store_rows: Distinct ids in the memory store.
        union_rows: Distinct ids across both, counted once per id.
        store_only_imported: Ids the store holds and the index does not;
            these import from the store rather than being lost.
        ledger_lines: The memory ledger lines the import writes.
    """

    document_rows: Annotated[int, Field(ge=0)]
    store_rows: Annotated[int, Field(ge=0)]
    union_rows: Annotated[int, Field(ge=0)]
    store_only_imported: Annotated[int, Field(ge=0)]
    ledger_lines: Annotated[int, Field(ge=0)]

    @classmethod
    def build(
        cls, *, document_ids: Iterable[str], store_ids: Iterable[str], ledger_lines: int
    ) -> MemoryUnionCensus:
        """Count the index and store populations beside the lines imported from them.

        Args:
            document_ids: Ids in the ``memory_index`` collection.
            store_ids: Ids in the memory store, in file order.
            ledger_lines: How many memory ledger lines the import wrote.

        Returns:
            The census; :func:`check_memory_union` proves it balances.
        """
        document = frozenset(document_ids)
        store = frozenset(store_ids)
        return cls(
            document_rows=len(document),
            store_rows=len(store),
            union_rows=len(document | store),
            store_only_imported=len(store - document),
            ledger_lines=ledger_lines,
        )


def check_memory_union(census: MemoryUnionCensus) -> None:
    """Refuse a memory import whose counts do not reconcile.

    Raises:
        MigrationCountMismatchError: When the union is smaller than either
            input, is not the index plus the store-only ids, or the ledger
            lines are not one per index row plus one per store id.
    """
    if census.union_rows < max(census.document_rows, census.store_rows):
        raise MigrationCountMismatchError(
            f"memory union holds {census.union_rows} notes, fewer than its largest input "
            f"(index {census.document_rows}, store {census.store_rows})"
        )
    if census.union_rows != census.document_rows + census.store_only_imported:
        raise MigrationCountMismatchError(
            f"memory union holds {census.union_rows} notes but the index contributes "
            f"{census.document_rows} and the store contributes {census.store_only_imported}"
        )
    if census.ledger_lines != census.document_rows + census.store_rows:
        raise MigrationCountMismatchError(
            f"the memory ledger receives {census.ledger_lines} lines, not the "
            f"{census.document_rows} index rows plus {census.store_rows} store notes"
        )


def latest_store_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Return the latest envelope of every note the memory store holds.

    The store is append-only and a revision re-appends the whole envelope,
    so the last row for an id is the note as it stood at the barrier.

    Args:
        rows: The store's rows, in file order.

    Returns:
        The latest row per id. A row without a string id is skipped: it
        names no note, and the row census has already reported it.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        row_id = row.get(STORE_ID_FIELD)
        if isinstance(row_id, str) and row_id:
            latest[row_id] = row
    return latest


def memory_union_rule_payload() -> dict[str, Any]:
    """Return the digestable statement of the memory union."""
    return {
        "union_inputs": ["memory_index_document_collection", MEMORY_LEDGER],
        "store_source_collection": MEMORY_STORE_SOURCE,
        "store_row_per_id": "latest",
        "manifest_fields": [
            "document_rows",
            "store_rows",
            "union_rows",
            "store_only_imported",
            "ledger_lines",
        ],
    }


__all__ = [
    "MEMORY_LEDGER",
    "MEMORY_STORE_SOURCE",
    "MemoryUnionCensus",
    "check_memory_union",
    "latest_store_rows",
    "memory_union_rule_payload",
]
