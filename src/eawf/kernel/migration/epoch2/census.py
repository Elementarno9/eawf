"""The source census: what the epoch-1 corpus actually contains.

The cutover can only prove it imported everything if it first says, with
numbers, what everything was. This module produces that statement from a
frozen snapshot: one row per declared collection, a proof form for every
collection that has no rows to count, the audit population reconciled
across the document and the ledger, and a located failure for every row
that cannot be read through its own schema.

Nothing here reaches the filesystem. The snapshot is the only source, so
two censuses of the same snapshot are always the same census.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.audits import AuditUnionCensus, check_audit_union
from eawf.kernel.migration.epoch2.dispositions import (
    COLLECTION_DISPOSITION_INDEX,
    Disposition,
    DropProofForm,
    StorageTier,
    drop_proof_form,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationCollectionOmittedError,
    MigrationCollectionUnknownError,
    MigrationRowValidationError,
)
from eawf.kernel.migration.epoch2.registry import AUDIT_UNION_RULE, TOTALITY_RULE
from eawf.kernel.migration.epoch2.rows import (
    COLLECTION_ROW_CONTRACT_INDEX,
    RowValidationFailure,
    SourceShape,
    validate_collection,
    validate_ledger_rows,
)
from eawf.kernel.migration.epoch2.rules import MappingRuleVersion, StrictMigrationModel
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot, SourceSnapshotIdentity

logger = logging.getLogger(__name__)


AUDITS_COLLECTION = "audits"
AUDIT_LEDGER = "audit"
ITERS_COLLECTION = "iters"
ITER_AUDIT_REF_FIELD = "audit_id"


class CollectionCensusRow(StrictMigrationModel):
    """The census of one epoch-1 top-level collection.

    Attributes:
        source_collection: The top-level key.
        disposition: Its declared fate, or ``None`` for a document
            metadata key that carries no rows.
        target_collection: Where its rows land in epoch 2.
        tier: Which store the imported rows are written to.
        shape: What the key is declared to hold.
        row_count: How many rows the source holds, or ``None`` when the
            source holds no container to count -- a null slot or a
            metadata scalar. Reporting ``0`` for a null would assert a
            container the source does not have.
        proof_form: Which ``explicit_drop`` proof the collection carries,
            present only when there is nothing to import.
    """

    source_collection: Annotated[str, Field(min_length=1, max_length=64)]
    disposition: Disposition | None
    target_collection: Annotated[str, Field(min_length=1, max_length=64)]
    tier: StorageTier
    shape: SourceShape
    row_count: Annotated[int, Field(ge=0)] | None
    proof_form: DropProofForm | None


def _census_row(*, collection: str, value: Any) -> CollectionCensusRow:
    """Build the census row for one collection from its raw source value."""
    disposition = COLLECTION_DISPOSITION_INDEX[collection]
    contract = COLLECTION_ROW_CONTRACT_INDEX[collection]

    row_count: int | None = None
    proof_form: DropProofForm | None = None
    if value is None:
        proof_form = drop_proof_form(value)
    elif isinstance(value, (dict, list, tuple, set)):
        row_count = len(value)
        if row_count == 0:
            proof_form = drop_proof_form(value)

    return CollectionCensusRow(
        source_collection=collection,
        disposition=disposition.disposition,
        target_collection=disposition.target_collection,
        tier=disposition.tier,
        shape=contract.shape,
        row_count=row_count,
        proof_form=proof_form,
    )


def _reconcile_collections(document: dict[str, Any]) -> tuple[str, ...]:
    """Return the declared collections after checking the source covers them.

    Args:
        document: The decoded epoch-1 document.

    Returns:
        Every declared collection, in sorted order.

    Raises:
        MigrationCollectionOmittedError: When a declared collection is
            absent from the document.
        MigrationCollectionUnknownError: When the document carries a
            collection no disposition row declares.
    """
    declared = frozenset(COLLECTION_DISPOSITION_INDEX)
    present = frozenset(document)
    omitted = sorted(declared - present)
    if omitted:
        raise MigrationCollectionOmittedError(
            f"the source omits {len(omitted)} declared collections: {', '.join(omitted)}"
        )
    unknown = sorted(present - declared)
    if unknown:
        raise MigrationCollectionUnknownError(
            f"the source carries {len(unknown)} undeclared collections: {', '.join(unknown)}"
        )
    return tuple(sorted(declared))


def _audit_document_ids(document: dict[str, Any]) -> tuple[str, ...]:
    """Return the ids of the ``audits`` collection, tolerating a bad shape.

    A mis-shaped ``audits`` value already produced a row failure, so the
    union is computed from what is readable rather than crashing here and
    hiding every other failure in the same pass.
    """
    audits = document.get(AUDITS_COLLECTION)
    if not isinstance(audits, dict):
        return ()
    return tuple(key for key in audits if key)


def _iter_audit_refs(document: dict[str, Any]) -> tuple[str, ...]:
    """Return every audit reference an iter row carries."""
    iters = document.get(ITERS_COLLECTION)
    if not isinstance(iters, dict):
        return ()
    refs: list[str] = []
    for row in iters.values():
        if not isinstance(row, dict):
            continue
        ref = row.get(ITER_AUDIT_REF_FIELD)
        if isinstance(ref, str) and ref:
            refs.append(ref)
    return tuple(refs)


class SourceCensus(StrictMigrationModel):
    """The complete, deterministic inventory of one epoch-1 snapshot.

    Attributes:
        identity: The digest set naming the snapshot the census covers.
        collections: One row per declared collection, in name order.
        audits: The reconciled audit population.
        row_failures: Every row that cannot be read through its schema,
            in source order. The census reports all of them; refusing is
            :meth:`require_importable`.
        mapping_rules: The rule versions this census is taken under.
    """

    identity: SourceSnapshotIdentity
    collections: tuple[CollectionCensusRow, ...]
    audits: AuditUnionCensus
    row_failures: tuple[RowValidationFailure, ...]
    mapping_rules: tuple[MappingRuleVersion, ...]

    @classmethod
    def build(cls, snapshot: SourceSnapshot) -> SourceCensus:
        """Census ``snapshot`` under the rules this package declares.

        Args:
            snapshot: The frozen epoch-1 corpus.

        Returns:
            The census, including any row failures found.

        Raises:
            MigrationCollectionOmittedError: When a declared collection
                is absent from the document.
            MigrationCollectionUnknownError: When the document carries an
                undeclared collection.
            MigrationSourceUnreadableError: When the snapshot holds no
                audit ledger.
            MigrationCountMismatchError: When the audit counts cannot
                describe a union.
        """
        document = snapshot.document
        collections = _reconcile_collections(document)
        ledger_rows = snapshot.ledger(AUDIT_LEDGER)

        failures: list[RowValidationFailure] = []
        rows: list[CollectionCensusRow] = []
        for collection in collections:
            value = document[collection]
            rows.append(_census_row(collection=collection, value=value))
            failures.extend(validate_collection(collection=collection, value=value))
        failures.extend(validate_ledger_rows(ledger=AUDIT_LEDGER, rows=ledger_rows))

        audits = AuditUnionCensus.build(
            document_ids=_audit_document_ids(document),
            ledger_ids=tuple(
                row["id"] for row in ledger_rows if isinstance(row.get("id"), str) and row["id"]
            ),
            iter_audit_refs=_iter_audit_refs(document),
        )
        check_audit_union(audits)

        return cls(
            identity=snapshot.identity,
            collections=tuple(rows),
            audits=audits,
            row_failures=tuple(failures),
            mapping_rules=(TOTALITY_RULE, AUDIT_UNION_RULE),
        )

    def collection(self, source_collection: str) -> CollectionCensusRow:
        """Return the census row for ``source_collection``.

        Args:
            source_collection: The top-level source key.

        Returns:
            Its census row.

        Raises:
            KeyError: When the census holds no such collection.
        """
        for row in self.collections:
            if row.source_collection == source_collection:
                return row
        raise KeyError(source_collection)

    def proof_forms(self) -> dict[str, DropProofForm]:
        """Return the proof form of every collection that carries one."""
        return {
            row.source_collection: row.proof_form
            for row in self.collections
            if row.proof_form is not None
        }

    def failure_locators(self) -> tuple[str, ...]:
        """Return the locator of every failing row, in source order."""
        return tuple(failure.locator for failure in self.row_failures)

    def require_importable(self) -> None:
        """Refuse the plan when any source row failed its schema.

        Raises:
            MigrationRowValidationError: When at least one row failed.
                The message names every locator, because a failure an
                operator cannot find in the source is a failure they
                cannot fix.
        """
        if not self.row_failures:
            return
        locators = ", ".join(self.failure_locators())
        raise MigrationRowValidationError(
            f"{len(self.row_failures)} epoch-1 rows fail their declared schema: {locators}"
        )
