"""The audit population, reconciled across the two places it lives.

Epoch 1 wrote audits twice: into the ``audits`` document collection and
into the audit ledger, and the two never fully agreed. Treating either
one as authoritative loses rows, so the importer takes their union and
proves, arithmetically, that the union it took really is one.

An iter reference that resolves in neither input is reported rather than
repaired. Inventing an audit to make a reference resolve would fabricate
a source fact, which is the one thing the importer never does.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Annotated

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


class AuditUnionCensus(StrictMigrationModel):
    """The audit population, counted once per id.

    Attributes:
        document_rows: Distinct ids in the ``audits`` collection.
        ledger_rows: Distinct ids in the audit ledger.
        union_rows: Distinct ids across both, counted once per id.
        store_only_imported: Ids the ledger holds and the document does
            not; these import from the ledger rather than being lost.
        refs_resolving_in_neither: Audit references held by iters that
            resolve in neither input, in sorted order.
    """

    document_rows: Annotated[int, Field(ge=0)]
    ledger_rows: Annotated[int, Field(ge=0)]
    union_rows: Annotated[int, Field(ge=0)]
    store_only_imported: Annotated[int, Field(ge=0)]
    refs_resolving_in_neither: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        document_ids: Iterable[str],
        ledger_ids: Iterable[str],
        iter_audit_refs: Iterable[str],
    ) -> AuditUnionCensus:
        """Reconcile the document and ledger audit populations.

        The result carries counts only; proving those counts describe a
        union is :func:`check_audit_union`, which the census applies to
        every reconciliation it assembles.

        Args:
            document_ids: Ids in the ``audits`` document collection.
            ledger_ids: Ids in the audit ledger, in file order.
            iter_audit_refs: Every audit reference held by an iter row.

        Returns:
            The reconciled census.
        """
        document = frozenset(document_ids)
        ledger = frozenset(ledger_ids)
        union = document | ledger
        store_only = ledger - document
        return cls(
            document_rows=len(document),
            ledger_rows=len(ledger),
            union_rows=len(union),
            store_only_imported=len(store_only),
            refs_resolving_in_neither=tuple(
                sorted({ref for ref in iter_audit_refs if ref not in union})
            ),
        )


def check_audit_union(census: AuditUnionCensus) -> None:
    """Refuse an audit reconciliation that cannot be a union.

    A union is never smaller than either input, and every row it holds
    beyond the document is a row only the store had. A count set that
    breaks either identity describes a reconciliation that dropped rows,
    which is exactly the failure the union exists to prevent. The check
    is separate from the reconciliation so it also holds over counts that
    arrive from somewhere else, such as a manifest read back at cutover.

    Args:
        census: The reconciled audit counts.

    Raises:
        MigrationCountMismatchError: When the union is smaller than
            either input, or when it does not equal the document rows
            plus the store-only rows.
    """
    largest_input = max(census.document_rows, census.ledger_rows)
    if census.union_rows < largest_input:
        raise MigrationCountMismatchError(
            f"audit union holds {census.union_rows} rows, fewer than its largest input "
            f"(document {census.document_rows}, ledger {census.ledger_rows})"
        )
    if census.union_rows != census.document_rows + census.store_only_imported:
        raise MigrationCountMismatchError(
            f"audit union holds {census.union_rows} rows but the document contributes "
            f"{census.document_rows} and the store contributes {census.store_only_imported}"
        )
