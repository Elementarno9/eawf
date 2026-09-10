"""Immutable legacy envelopes, and the ledgers that take rows verbatim.

Some epoch-1 collections have no epoch-2 successor that can hold them
without changing what they mean. An ``agent_sessions`` row is provenance
about an episode that has already ended; a ``worktrees`` row is the
record that a checkout once existed. Neither is a live capability, so
each imports as an immutable legacy envelope: the source row preserved
whole, addressable by an alias, and minting nothing.

A worktree that was still ``active`` when the barrier was taken is the
sharp case. Minting a WorkLease from it would hand the epoch-2 runtime a
claim on a tree nobody is holding, and the first thing a lease does is
block work. So the envelope carries an annotation saying the checkout was
open at cutover, and the operator -- not the importer -- decides what to
do about it.

The ledger imports are the other half. Artifacts, memory notes and audits
already have epoch-2 homes that take their rows unchanged, so those rows
travel byte for byte and only their addressing changes: each is reachable
under a reindexed alias rather than under whatever position the epoch-1
document happened to give it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationDuplicateKeyError
from eawf.kernel.migration.epoch2.origins import build_legacy_origin, source_digest
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.state.epoch2.values import EntityOrigin

logger = logging.getLogger(__name__)


class EnvelopeCollection(StrEnum):
    """The epoch-1 collections that import as immutable legacy envelopes."""

    AGENT_SESSIONS = "agent_sessions"
    WORKTREES = "worktrees"


class LedgerCollection(StrEnum):
    """The epoch-1 collections whose rows land in a ledger unchanged."""

    ARTIFACTS = "artifacts"
    MEMORY_INDEX = "memory_index"
    AUDITS = "audits"


#: Which epoch-2 ledger each collection's rows are written to.
LEDGER_TARGETS: Mapping[LedgerCollection, str] = MappingProxyType(
    {
        LedgerCollection.ARTIFACTS: "artifact",
        LedgerCollection.MEMORY_INDEX: "memory",
        LedgerCollection.AUDITS: "audit",
    }
)

#: The epoch-1 store ledger the audit union reads its second input from.
AUDIT_LEDGER = "audit"

#: The live epoch-2 records the importer could mint from these rows and
#: does not. Naming them is the point: an empty tuple beside a record
#: kind is a decision, where a silently absent one is an omission.
WORK_LEASE_RECORD = "work_lease"
RUN_RECORD = "run"

ENVELOPE_MINTS: Mapping[EnvelopeCollection, tuple[str, ...]] = MappingProxyType(
    {
        EnvelopeCollection.AGENT_SESSIONS: (),
        EnvelopeCollection.WORKTREES: (),
    }
)

#: The status field and value that make a row open at cutover, and the
#: annotation the envelope carries in its place.
STATUS_FIELD = "status"
WORKTREE_ACTIVE_STATUS = "active"
WORKTREE_ACTIVE_ANNOTATION = "worktree_active_at_cutover_without_lease"
SESSION_ACTIVE_STATUS = "active"
SESSION_ACTIVE_ANNOTATION = "session_active_at_cutover_without_run"

#: How an imported row is addressed once the epoch-1 document's own
#: positions are gone.
LEGACY_ALIAS_PREFIX = "legacy"


def legacy_alias(*, collection: str, source_id: str) -> str:
    """Return the alias one imported row is reachable under.

    Args:
        collection: The epoch-1 top-level key the row came from.
        source_id: The row's own key in that collection.

    Returns:
        The alias, which is collection-qualified so two collections that
        reuse an id do not collide in the reindex.

    Raises:
        ValueError: When either part is empty, which would produce an
            alias that addresses a whole collection instead of a row.
    """
    if not collection or not source_id:
        raise ValueError("a legacy alias needs both a collection and a source id")
    return f"{LEGACY_ALIAS_PREFIX}:{collection}/{source_id}"


class LegacyEnvelope(StrictMigrationModel):
    """One epoch-1 row preserved whole, granting nothing.

    Attributes:
        source_collection: The epoch-1 top-level key the row came from.
        source_id: The row's own key in that collection.
        alias: The alias the row is reachable under after the reindex.
        origin: The legacy origin the envelope carries.
        payload: The source row, preserved.
        payload_digest: The digest of that row in ``sha256:<hex>`` form.
        minted_records: The live epoch-2 records this envelope produced,
            which is always empty for an envelope collection.
        annotations: Every annotation the row earned at import.
    """

    source_collection: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    alias: Annotated[str, Field(min_length=1)]
    origin: EntityOrigin
    payload: dict[str, Any]
    payload_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    minted_records: tuple[str, ...]
    annotations: tuple[str, ...]


class ImportedLedgerRow(StrictMigrationModel):
    """One epoch-1 row written into an epoch-2 ledger unchanged.

    Attributes:
        ledger: The epoch-2 ledger the row is written to.
        source_collection: The epoch-1 top-level key the row came from.
        source_id: The row's own key in that collection.
        alias: The alias the row is reachable under after the reindex.
        origin: The legacy origin the record carries.
        payload: The source row, byte-identical under canonical form.
        payload_digest: The digest of that row in ``sha256:<hex>`` form.
        store_only: Whether the row came from the store ledger rather
            than from the document collection, which is what the audit
            union's store-only count is made of.
    """

    ledger: Annotated[str, Field(min_length=1)]
    source_collection: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    alias: Annotated[str, Field(min_length=1)]
    origin: EntityOrigin
    payload: dict[str, Any]
    payload_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    store_only: bool


def _annotations(*, collection: EnvelopeCollection, row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the annotations one envelope row earned at import."""
    status = row.get(STATUS_FIELD)
    if collection is EnvelopeCollection.WORKTREES and status == WORKTREE_ACTIVE_STATUS:
        return (WORKTREE_ACTIVE_ANNOTATION,)
    if collection is EnvelopeCollection.AGENT_SESSIONS and status == SESSION_ACTIVE_STATUS:
        return (SESSION_ACTIVE_ANNOTATION,)
    return ()


def map_legacy_envelope(
    *,
    collection: EnvelopeCollection,
    source_id: str,
    row: Mapping[str, Any],
    source_schema_version: str,
) -> LegacyEnvelope:
    """Wrap one epoch-1 row as an immutable legacy envelope.

    Args:
        collection: Which envelope collection the row came from.
        source_id: The row's own key in that collection.
        row: The source row, preserved whole.
        source_schema_version: The epoch-1 schema version, stamped on
            the origin.

    Returns:
        The envelope, minting nothing and annotated where the row was
        still open when the barrier was taken.

    Raises:
        TypeError: When the row holds a value ``json`` cannot encode.
        ValidationError: When a field violates the model contract.
    """
    annotations = _annotations(collection=collection, row=row)
    return LegacyEnvelope(
        source_collection=collection.value,
        source_id=source_id,
        alias=legacy_alias(collection=collection.value, source_id=source_id),
        origin=build_legacy_origin(
            source_kind=collection.value,
            source_id=source_id,
            row=row,
            source_schema_version=source_schema_version,
            confidence="supported" if annotations else "exact",
        ),
        payload=dict(row),
        payload_digest=source_digest(row),
        minted_records=ENVELOPE_MINTS[collection],
        annotations=annotations,
    )


def map_ledger_row(
    *,
    collection: LedgerCollection,
    source_id: str,
    row: Mapping[str, Any],
    source_schema_version: str,
    store_only: bool,
) -> ImportedLedgerRow:
    """Write one epoch-1 row into its epoch-2 ledger unchanged.

    Args:
        collection: Which collection the row came from.
        source_id: The row's own key in that collection.
        row: The source row, copied without conversion.
        source_schema_version: The epoch-1 schema version.
        store_only: Whether the row came from the store ledger only.

    Returns:
        The ledger row, addressable by its reindexed alias.

    Raises:
        TypeError: When the row holds a value ``json`` cannot encode.
        ValidationError: When a field violates the model contract.
    """
    return ImportedLedgerRow(
        ledger=LEDGER_TARGETS[collection],
        source_collection=collection.value,
        source_id=source_id,
        alias=legacy_alias(collection=collection.value, source_id=source_id),
        origin=build_legacy_origin(
            source_kind=collection.value,
            source_id=source_id,
            row=row,
            source_schema_version=source_schema_version,
            confidence="exact",
        ),
        payload=dict(row),
        payload_digest=source_digest(row),
        store_only=store_only,
    )


def _keyed_rows(document: Mapping[str, Any], collection: str) -> dict[str, Mapping[str, Any]]:
    """Return one collection's object rows, keyed by id."""
    value = document.get(collection)
    if not isinstance(value, Mapping):
        return {}
    return {str(key): row for key, row in value.items() if key and isinstance(row, Mapping)}


class EnvelopeImportPlan(StrictMigrationModel):
    """Every collection that imports without conversion.

    Attributes:
        envelopes: The session and worktree rows, collection by
            collection and each in source-id order.
        ledger_rows: The artifact, memory and audit rows, in the same
            order, with audits taken as the union of the document
            collection and the store ledger.
    """

    envelopes: tuple[LegacyEnvelope, ...]
    ledger_rows: tuple[ImportedLedgerRow, ...]

    @classmethod
    def build(
        cls,
        *,
        document: Mapping[str, Any],
        audit_ledger_rows: Iterable[Mapping[str, Any]],
        source_schema_version: str,
    ) -> EnvelopeImportPlan:
        """Import every envelope and ledger collection of ``document``.

        Args:
            document: The decoded epoch-1 state document.
            audit_ledger_rows: The rows of the epoch-1 audit ledger, in
                file order. An id the document also holds is imported
                once, from the document.
            source_schema_version: The epoch-1 schema version.

        Returns:
            The import plan.

        Raises:
            TypeError: When a row holds a value ``json`` cannot encode.
            ValidationError: When a row violates the model contract.
        """
        envelopes: list[LegacyEnvelope] = []
        for envelope_collection in EnvelopeCollection:
            envelope_source = _keyed_rows(document, envelope_collection.value)
            envelopes.extend(
                map_legacy_envelope(
                    collection=envelope_collection,
                    source_id=source_id,
                    row=envelope_source[source_id],
                    source_schema_version=source_schema_version,
                )
                for source_id in sorted(envelope_source)
            )

        ledger_rows: list[ImportedLedgerRow] = []
        for ledger_collection in LedgerCollection:
            ledger_source = _keyed_rows(document, ledger_collection.value)
            ledger_rows.extend(
                map_ledger_row(
                    collection=ledger_collection,
                    source_id=source_id,
                    row=ledger_source[source_id],
                    source_schema_version=source_schema_version,
                    store_only=False,
                )
                for source_id in sorted(ledger_source)
            )
            if ledger_collection is LedgerCollection.AUDITS:
                ledger_rows.extend(
                    _store_only_audits(
                        document_ids=frozenset(ledger_source),
                        audit_ledger_rows=audit_ledger_rows,
                        source_schema_version=source_schema_version,
                    )
                )

        return cls(envelopes=tuple(envelopes), ledger_rows=tuple(ledger_rows))

    def for_collection(self, collection: EnvelopeCollection) -> tuple[LegacyEnvelope, ...]:
        """Return every envelope imported from one collection.

        Args:
            collection: Which envelope collection to select.

        Returns:
            The envelopes, in source-id order.
        """
        return tuple(row for row in self.envelopes if row.source_collection == collection.value)

    def for_ledger(self, collection: LedgerCollection) -> tuple[ImportedLedgerRow, ...]:
        """Return every ledger row imported from one collection.

        Args:
            collection: Which ledger collection to select.

        Returns:
            The rows, in source-id order.
        """
        return tuple(row for row in self.ledger_rows if row.source_collection == collection.value)

    def alias_index(self) -> dict[str, LegacyEnvelope | ImportedLedgerRow]:
        """Return every imported row keyed by its reindexed alias.

        Returns:
            The alias index the epoch-2 store is addressed through.

        Raises:
            MigrationDuplicateKeyError: When two rows claim one alias,
                which would make the reindex lose whichever it wrote
                first.
        """
        index: dict[str, LegacyEnvelope | ImportedLedgerRow] = {}
        rows: tuple[LegacyEnvelope | ImportedLedgerRow, ...] = (
            *self.envelopes,
            *self.ledger_rows,
        )
        for row in rows:
            if row.alias in index:
                raise MigrationDuplicateKeyError(f"two imported rows claim the alias {row.alias!r}")
            index[row.alias] = row
        return index


def _store_only_audits(
    *,
    document_ids: frozenset[str],
    audit_ledger_rows: Iterable[Mapping[str, Any]],
    source_schema_version: str,
) -> tuple[ImportedLedgerRow, ...]:
    """Import the audits only the store ledger holds.

    The ledger is part of the source rather than a cache of the
    document, so a row only it holds is imported rather than lost. A row
    both hold is imported once, from the document.

    Args:
        document_ids: The audit ids the document collection holds.
        audit_ledger_rows: The rows of the epoch-1 audit ledger.
        source_schema_version: The epoch-1 schema version.

    Returns:
        The store-only rows, in ledger-id order.

    Raises:
        TypeError: When a row holds a value ``json`` cannot encode.
        ValidationError: When a row violates the model contract.
    """
    store_only: dict[str, Mapping[str, Any]] = {}
    for row in audit_ledger_rows:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id or row_id in document_ids:
            continue
        store_only.setdefault(row_id, row)
    return tuple(
        map_ledger_row(
            collection=LedgerCollection.AUDITS,
            source_id=row_id,
            row=store_only[row_id],
            source_schema_version=source_schema_version,
            store_only=True,
        )
        for row_id in sorted(store_only)
    )


def envelope_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the envelope and ledger tables."""
    return {
        "envelope_collections": [collection.value for collection in EnvelopeCollection],
        "envelope_mints": {
            collection.value: list(records) for collection, records in ENVELOPE_MINTS.items()
        },
        "never_minted": sorted({WORK_LEASE_RECORD, RUN_RECORD}),
        "ledger_targets": {
            collection.value: ledger for collection, ledger in LEDGER_TARGETS.items()
        },
        "alias_prefix": LEGACY_ALIAS_PREFIX,
        "active_annotations": {
            EnvelopeCollection.WORKTREES.value: WORKTREE_ACTIVE_ANNOTATION,
            EnvelopeCollection.AGENT_SESSIONS.value: SESSION_ACTIVE_ANNOTATION,
        },
        "audit_union_inputs": ["audits_document_collection", AUDIT_LEDGER],
    }
