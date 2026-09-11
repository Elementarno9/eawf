"""Writing a staged import into its tiers, and the two gates that guard it.

Plan mode stages the whole import in memory. This is where it lands on
disk, and the one decision that matters is per record rather than per
collection: a record that will never change again goes into its
append-only ledger, and only work in flight goes into the document. The
epoch-1 monolith was 5.9 MB because every finished wave stayed in the one
file that every mutation rewrote; placing the finished ones in a ledger is
what stops that from being rebuilt.

What this buys is bounded rewrite cost, not a smaller repository. The
history does not evaporate -- it moves into ledgers that are themselves
committed, so the bytes on disk are comparable. What changes is that the
compare-and-swap document is now sized by the work in flight, so the cost
of one mutation stops growing with the project's whole past.

Two gates run after the write, against what is actually on disk rather
than against the plan that intended it. The first refuses a document that
retains a record nothing can move out of; the second refuses a tree
carrying a concrete home-directory path. Both run after the write on
purpose: a staging tree is discardable, so the honest thing is to write
it, measure it, and refuse on what was measured.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.envelopes import ImportedLedgerRow, LegacyEnvelope
from eawf.kernel.migration.epoch2.errors import (
    MigrationFabricationDetectedError,
    MigrationTerminalInDocumentError,
    MigrationTierUndeclaredError,
)
from eawf.kernel.migration.epoch2.lifecycle import ImportedLifecycleRecord, LifecycleTarget
from eawf.kernel.migration.epoch2.manifest import MANIFEST_SCHEMA_VERSION, MigrationManifest
from eawf.kernel.migration.epoch2.manifest_rows import BackupRecord, TierPlacement
from eawf.kernel.migration.epoch2.measurements import ImportedMeasurement, MeasurementKind
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.plan_mode import MigrationPlan
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.migration.epoch2.runs import MintedRun
from eawf.kernel.migration.epoch2.scrub import (
    require_no_home_path_leaks,
    scan_staged_tree,
)
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot, digest_bytes
from eawf.kernel.migration.epoch2.validation import LIFECYCLE_ENTITY_KINDS, run_source_address
from eawf.kernel.state.epoch2.task import TERMINAL_TASK_STATUSES
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.kernel.store.compaction import document_rows, read_document, write_document
from eawf.kernel.store.index import regenerate_indexes
from eawf.kernel.store.ledger import (
    LedgerRecord,
    guarded_ledger_write,
    render_ledger_line,
)
from eawf.kernel.store.paths import index_dir, ledger_path
from eawf.kernel.store.tiers import (
    ENTITY_COLLECTIONS,
    LEDGER_COLLECTIONS,
    Epoch2Collection,
    StorageTier,
    tier_for,
)

logger = logging.getLogger(__name__)


#: The document key carrying the epoch-2 schema generation. The cutover
#: writes it so a reader can tell a staged epoch-2 tree from the epoch-1
#: document it was imported from by one field.
DOCUMENT_SCHEMA_KEY: Final = "schema_version"

#: The document field holding a row's lifecycle status, which is the only
#: field the terminal-residency gate reads.
ROW_STATUS_FIELD: Final = "status"

#: The document field holding the imported record itself.
ROW_PAYLOAD_FIELD: Final = "payload"

#: The document field recording when the row's status was reached.
ROW_RECORDED_AT_FIELD: Final = "recorded_at"

#: The status an imported record with no lifecycle of its own carries. A
#: legacy envelope, a re-pointed measurement and a verbatim ledger row each
#: record a fact rather than a state, so the ledger line names the
#: commitment instead of inventing a lifecycle for them to be in.
HISTORY_RECORD_STATUS: Final = "imported"

#: The statuses that move a record of a split collection out of the
#: document. A Task's set is the compaction-admitting set rather than the
#: transition table's terminal set, because ``DROPPED`` is terminal yet
#: never entered delivery, so it carries no history a ledger line would
#: preserve -- and the writer and the gate have to agree on that or a
#: dropped Task would red the gate that placed it.
COMPACTING_STATUSES: Final[Mapping[Epoch2Collection, frozenset[str]]] = {
    Epoch2Collection.MILESTONE: TERMINAL_STATUSES[LifecycleEntity.MILESTONE],
    Epoch2Collection.BATCH: TERMINAL_STATUSES[LifecycleEntity.DELIVERY_BATCH],
    Epoch2Collection.TASK: frozenset(status.value for status in TERMINAL_TASK_STATUSES),
}

#: The ledger collections whose entire imported population is history. A
#: Run the source recorded is a finished episode, never a live lease, and
#: every legacy, measurement and ledger row is immutable by the
#: disposition that produced it -- so none of them has an in-flight form
#: the document could hold.
HISTORY_COLLECTIONS: Final[frozenset[Epoch2Collection]] = frozenset(
    {
        Epoch2Collection.RUN,
        Epoch2Collection.LEGACY,
        Epoch2Collection.ESTIMATE,
        Epoch2Collection.ACTUAL,
        Epoch2Collection.ARTIFACT,
        Epoch2Collection.MEMORY,
        Epoch2Collection.AUDIT,
    }
)

#: Which epoch-2 collection each measurement kind's rows are written to.
MEASUREMENT_LEDGERS: Final[Mapping[MeasurementKind, Epoch2Collection]] = {
    MeasurementKind.ESTIMATE: Epoch2Collection.ESTIMATE,
    MeasurementKind.ACTUAL: Epoch2Collection.ACTUAL,
}

#: Which epoch-2 collection each lifecycle target's records are written to,
#: resolved through the shared entity-kind table so the placement and the
#: manifest cannot route one record two ways.
LIFECYCLE_COLLECTIONS: Final[Mapping[LifecycleTarget, Epoch2Collection]] = {
    target: ENTITY_COLLECTIONS[kind] for target, kind in LIFECYCLE_ENTITY_KINDS.items()
}


class StagedRecord(StrictMigrationModel):
    """One imported record, addressed at the collection that will hold it.

    Attributes:
        collection: The epoch-2 collection the record belongs to.
        record_key: Its key within that collection. The alias and the
            source id it is built from are both collection-qualified, so a
            collision needs two rows of one collection to share a key --
            which :func:`staged_records` refuses rather than trusts.
        status: The record's epoch-2 status, which is what decides whether
            it stays in the document or moves to the ledger.
        payload: The imported record, carried verbatim. No string in here
            is rewritten on the way to disk.
    """

    collection: Epoch2Collection
    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    status: Annotated[str, Field(min_length=1, max_length=64)]
    payload: dict[str, Any]

    def belongs_in_ledger(self) -> bool:
        """Return whether this record is history and so leaves the document.

        Returns:
            ``True`` for a record of a history-only collection, and for a
            split collection's record whose status nothing can leave.

        Raises:
            MigrationTierUndeclaredError: The collection is at the ledger
                tier but neither table says how its records split, so
                there is no rule to apply.
        """
        if self.collection in HISTORY_COLLECTIONS:
            return True
        statuses = COMPACTING_STATUSES.get(self.collection)
        if statuses is None:
            raise MigrationTierUndeclaredError(
                f"{self.collection.value!r} is declared at the ledger tier but no residency "
                "rule says which of its records leave the document"
            )
        return self.status in statuses


class DocumentResidencyFinding(StrictMigrationModel):
    """One document row that has no business being in the document.

    Attributes:
        collection: The collection the row sits under.
        record_key: The row's key.
        detail: Why the row is refused -- the terminal status it holds, or
            the reason its status could not be read at all.
    """

    collection: Epoch2Collection
    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    detail: Annotated[str, Field(min_length=1, max_length=200)]


def _require_source_id(record: ImportedLifecycleRecord) -> str:
    """Return the source row id one lifecycle record came from.

    Args:
        record: The imported record.

    Returns:
        Its source row id.

    Raises:
        MigrationFabricationDetectedError: The record cites no source row,
            so there is no key to address it by and inventing one would
            put a record in the tree the source never held.
    """
    source_id = record.origin.source_id
    if source_id is None:
        raise MigrationFabricationDetectedError(
            f"a {record.target.value} imported from {record.source_collection!r} cites no "
            "source row, so the cutover has no key to write it under"
        )
    return source_id


def staged_records(plan: CorpusImportPlan) -> tuple[StagedRecord, ...]:
    """Address every record of one import plan at the collection holding it.

    The order is the import order the staged reduction already publishes --
    lifecycle records, Runs, measurements, legacy envelopes, ledger rows --
    so two runs over one corpus write the same bytes in the same sequence.

    Args:
        plan: The corpus import plan.

    Returns:
        One record per record the import writes.

    Raises:
        MigrationFabricationDetectedError: A lifecycle record cites no
            source row.
        MigrationTierUndeclaredError: Two records claim one
            ``(collection, key)`` slot, which would silently drop one of
            them from the document or commit the same key twice.
        ValidationError: A record violates :class:`StagedRecord`.
    """
    records = tuple(_iter_staged(plan))
    seen: set[tuple[Epoch2Collection, str]] = set()
    for record in records:
        slot = (record.collection, record.record_key)
        if slot in seen:
            raise MigrationTierUndeclaredError(
                f"two staged records claim {record.collection.value}/{record.record_key}, "
                "so one of them would be lost"
            )
        seen.add(slot)
    return records


def _iter_staged(plan: CorpusImportPlan) -> Iterator[StagedRecord]:
    """Yield one staged record per record the plan writes, in import order."""
    for record in plan.lifecycle.records:
        yield StagedRecord(
            collection=LIFECYCLE_COLLECTIONS[record.target],
            record_key=_require_source_id(record),
            status=record.target_status,
            payload=record.model_dump(mode="json"),
        )
    for run in plan.lifecycle.minted_runs():
        yield _run_record(run)
    for measurement in plan.measurements.measurements:
        yield _measurement_record(measurement)
    for envelope in plan.envelopes.envelopes:
        yield _envelope_record(envelope)
    for row in plan.envelopes.ledger_rows:
        yield _ledger_row_record(row)


def _run_record(run: MintedRun) -> StagedRecord:
    """Address one minted Run at the Run ledger."""
    return StagedRecord(
        collection=Epoch2Collection.RUN,
        record_key=run_source_address(run),
        status=run.status,
        payload=run.model_dump(mode="json"),
    )


def _measurement_record(measurement: ImportedMeasurement) -> StagedRecord:
    """Address one re-pointed measurement at its own ledger."""
    return StagedRecord(
        collection=MEASUREMENT_LEDGERS[measurement.kind],
        record_key=measurement.map_key,
        status=HISTORY_RECORD_STATUS,
        payload=measurement.model_dump(mode="json"),
    )


def _envelope_record(envelope: LegacyEnvelope) -> StagedRecord:
    """Address one immutable legacy envelope at the legacy ledger."""
    return StagedRecord(
        collection=Epoch2Collection.LEGACY,
        record_key=envelope.alias,
        status=HISTORY_RECORD_STATUS,
        payload=envelope.model_dump(mode="json"),
    )


def _ledger_row_record(row: ImportedLedgerRow) -> StagedRecord:
    """Address one verbatim ledger row at the ledger its table declares.

    Raises:
        ValueError: The row names a ledger no epoch-2 collection declares.
    """
    return StagedRecord(
        collection=Epoch2Collection(row.ledger),
        record_key=row.alias,
        status=HISTORY_RECORD_STATUS,
        payload=row.model_dump(mode="json"),
    )


def document_row(record: StagedRecord, *, recorded_at: datetime) -> dict[str, Any]:
    """Return the shape one in-flight record occupies in the document.

    The shape is the ledger line's shape minus the fields the document
    already implies by position, so a later compaction moves the row into
    its ledger without reinterpreting it.

    Args:
        record: The record staying in the document.
        recorded_at: When the import placed it.

    Returns:
        The document row.
    """
    return {
        ROW_STATUS_FIELD: record.status,
        ROW_RECORDED_AT_FIELD: recorded_at.isoformat(),
        ROW_PAYLOAD_FIELD: record.payload,
    }


def ledger_record(record: StagedRecord, *, recorded_at: datetime) -> LedgerRecord:
    """Return the append-only ledger line one terminal record becomes.

    Args:
        record: The record leaving the document.
        recorded_at: When the import placed it.

    Returns:
        The ledger line.

    Raises:
        ValidationError: The record's key or status violates the ledger
            contract.
    """
    return LedgerRecord(
        collection=record.collection,
        record_key=record.record_key,
        status=record.status,
        recorded_at=recorded_at,
        payload=record.payload,
    )


def document_residency_findings(state_path: Path) -> tuple[DocumentResidencyFinding, ...]:
    """Return every document row that is not work in flight.

    Two shapes are refused, and they are different claims. A row holding a
    status nothing can leave is history the document should not be
    rewriting. A row whose status cannot be read is a row nobody can prove
    is in flight, which is the same risk without the evidence.

    Args:
        state_path: The staged tree's ``state.json``.

    Returns:
        The findings, in collection-then-key order.

    Raises:
        FileNotFoundError: The document does not exist.
        ValueError: The document, or one of its collections, is not an
            object.
    """
    document = read_document(state_path)
    findings: list[DocumentResidencyFinding] = []
    for collection in sorted(COMPACTING_STATUSES):
        statuses = COMPACTING_STATUSES[collection]
        rows = document_rows(document, collection)
        for key in sorted(rows):
            row = rows[key]
            status = row.get(ROW_STATUS_FIELD) if isinstance(row, Mapping) else None
            if not isinstance(status, str) or not status:
                findings.append(
                    DocumentResidencyFinding(
                        collection=collection,
                        record_key=key,
                        detail="the row records no status, so it cannot be shown to be in flight",
                    )
                )
                continue
            if status in statuses:
                findings.append(
                    DocumentResidencyFinding(
                        collection=collection,
                        record_key=key,
                        detail=f"{status} is terminal, so the row belongs in the ledger",
                    )
                )
    return tuple(findings)


def require_document_holds_only_work_in_flight(state_path: Path) -> None:
    """Refuse a staged document that retains a record nothing can move out of.

    Args:
        state_path: The staged tree's ``state.json``.

    Raises:
        MigrationTerminalInDocumentError: At least one row is terminal or
            has no readable status. Every offender is named, because a row
            an operator cannot locate is a row they cannot move.
        FileNotFoundError: The document does not exist.
    """
    findings = document_residency_findings(state_path)
    if not findings:
        return
    named = ", ".join(
        f"{finding.collection.value}/{finding.record_key} ({finding.detail})"
        for finding in findings
    )
    raise MigrationTerminalInDocumentError(
        f"the staged document retains {len(findings)} records that are not work in "
        f"flight, so every future mutation would rewrite them: {named}"
    )


def _backup_surfaces(state_path: Path) -> tuple[str, ...]:
    """Return one pinned digest per file the staged write would rewrite.

    Args:
        state_path: The staged tree's ``state.json``.

    Returns:
        ``<locator>@sha256:<hex>`` entries in locator order, over the
        document, the ledgers and the derived indexes that already exist.
        An empty result records that the write found nothing to preserve,
        which is a different claim from having taken no backup.
    """
    state_dir = state_path.parent
    candidates = [state_path]
    for directory in (state_dir / "ledger", index_dir(state_path)):
        if directory.is_dir():
            candidates.extend(item for item in sorted(directory.iterdir()) if item.is_file())
    return tuple(
        f"{item.relative_to(state_dir).as_posix()}@sha256:{digest_bytes(item.read_bytes())}"
        for item in candidates
        if item.is_file()
    )


def _write_ledgers(
    state_path: Path,
    records: tuple[StagedRecord, ...],
    *,
    recorded_at: datetime,
) -> int:
    """Append every history record to its ledger and return the bytes held.

    One guarded whole-file write per collection rather than one append per
    record: the guard refuses any content that is not an extension of what
    the ledger already holds, so the append-only property is checked over
    the batch rather than trusted per line.

    Args:
        state_path: The staged tree's ``state.json``.
        records: The records that belong in a ledger, in import order.
        recorded_at: When the import placed them.

    Returns:
        How many bytes the ledgers occupy after the write.

    Raises:
        LedgerAppendOnlyError: A write would not extend the ledger.
        ValueError: A record's collection is not at the ledger tier.
    """
    batched: dict[Epoch2Collection, list[str]] = {}
    for record in records:
        line = render_ledger_line(ledger_record(record, recorded_at=recorded_at))
        batched.setdefault(record.collection, []).append(line)

    held = 0
    for collection in sorted(batched, key=lambda item: item.value):
        path = ledger_path(state_path, collection)
        current = path.read_bytes() if path.exists() else b""
        addition = "".join(f"{line}\n" for line in batched[collection]).encode("utf-8")
        guarded_ledger_write(path, current + addition)
        held += len(current) + len(addition)
    return held


def _write_document(
    state_path: Path,
    records: tuple[StagedRecord, ...],
    *,
    recorded_at: datetime,
) -> int:
    """Merge the in-flight records into the document and return its row count.

    The document is extended rather than replaced: a staging tree may
    already hold rows an earlier step wrote, and silently discarding them
    would hide exactly the residency defect the gate exists to catch.

    Args:
        state_path: The staged tree's ``state.json``.
        records: The records that stay in the document, in import order.
        recorded_at: When the import placed them.

    Returns:
        How many rows the document holds across every ledger collection,
        not only the ones this write touched: a row an earlier step left
        behind is part of the residual whether or not the import added to
        its collection.

    Raises:
        ValueError: The document holds a non-object under a collection key.
    """
    document = read_document(state_path) if state_path.exists() else {}
    document[DOCUMENT_SCHEMA_KEY] = MANIFEST_SCHEMA_VERSION
    for record in records:
        rows = document_rows(document, record.collection)
        rows[record.record_key] = document_row(record, recorded_at=recorded_at)
        document[record.collection.value] = rows
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_document(state_path, document)
    return sum(len(document_rows(document, collection)) for collection in LEDGER_COLLECTIONS)


def stage_cutover(
    *,
    plan: MigrationPlan,
    snapshot_root: Path,
    allowlist_path: Path,
    state_path: Path,
    recorded_at: datetime,
) -> MigrationManifest:
    """Write one plan's import into the tiers of a staged tree.

    A staged write is a rehearsal into a discardable tree, so an
    unresolved row does not refuse here. It refuses at the apply, where a
    row with no target would silently shrink the corpus; staging exists
    precisely so those rows can be read off a real tier layout first. The
    corpus digest *is* checked, because a write taken over different bytes
    than the plan measured would describe a tree nobody planned.

    Args:
        plan: The sealed plan an operator approved.
        snapshot_root: The staging directory holding the epoch-1 corpus,
            re-digested so a corpus that moved since the plan refuses.
        allowlist_path: Location of the shared allowed-legacy-symbol
            allowlist.
        state_path: The staged tree's ``state.json``. Created with its
            parent when absent.
        recorded_at: The timestamp every written row records. A parameter
            rather than a clock read, so two runs over one corpus differ in
            nothing.

    Returns:
        The plan's manifest at the ``staged`` rollback boundary, carrying
        the per-tier counts, the residual document size and the restore
        point the write took.

    Raises:
        MigrationSourceChangedError: The corpus moved since the plan.
        MigrationTerminalInDocumentError: The written document retains a
            record that is not work in flight.
        MigrationHomePathLeakError: The written tree carries a concrete
            home-directory path.
        MigrationTierUndeclaredError: A record routes to a collection with
            no residency rule, or two records claim one key.
        ValidationError: An assembled model violates its contract.
    """
    plan.require_source_unchanged(snapshot_root)
    snapshot = SourceSnapshot.read(snapshot_root)
    corpus = CorpusImportPlan.build(snapshot=snapshot, allowlist_path=allowlist_path)
    records = staged_records(corpus)
    _require_ledger_tier_routes(records)

    backup = BackupRecord.of(taken_at=recorded_at, surfaces=_backup_surfaces(state_path))
    history = tuple(record for record in records if record.belongs_in_ledger())
    in_flight = tuple(record for record in records if not record.belongs_in_ledger())

    ledger_bytes = _write_ledgers(state_path, history, recorded_at=recorded_at)
    document_count = _write_document(state_path, in_flight, recorded_at=recorded_at)
    indexed = regenerate_indexes(state_path)

    require_document_holds_only_work_in_flight(state_path)
    require_no_home_path_leaks(
        scan_staged_tree(
            state_path,
            ledger_paths=[ledger_path(state_path, collection) for collection in indexed],
        )
    )

    placement = TierPlacement.of(
        records_by_tier={
            StorageTier.DOCUMENT: document_count,
            StorageTier.LEDGER: len(history),
        },
        document_record_count=document_count,
        document_byte_length=state_path.stat().st_size,
        ledger_byte_length=ledger_bytes,
        indexed_collections=indexed,
    )
    logger.info(
        f"stage_cutover state_path={state_path} ledger_records={len(history)} "
        f"document_records={document_count} document_bytes={placement.document_byte_length}"
    )
    return plan.manifest.staged(placement=placement, backup=backup)


def _require_ledger_tier_routes(records: tuple[StagedRecord, ...]) -> None:
    """Refuse a staged record routed outside the ledger tier.

    Every collection the importer materialises today is a ledger
    collection: its records either split by terminality or are history
    outright. The collections the tier table puts in the document --
    Project, Track, the outcome metrics -- are planned by the manifest and
    written by no importer stage yet, so a record arriving at one of them
    has no residency rule, and guessing one is how a record lands
    somewhere plausible and wrong.

    Args:
        records: The staged records.

    Raises:
        MigrationTierUndeclaredError: A record's collection is not declared
            at the ledger tier.
    """
    stray = sorted(
        {
            f"{record.collection.value} ({tier_for(record.collection).value})"
            for record in records
            if tier_for(record.collection) is not StorageTier.LEDGER
        }
    )
    if stray:
        raise MigrationTierUndeclaredError(
            f"the cutover places ledger-tier collections only, but {len(stray)} records "
            f"route elsewhere: {', '.join(stray)}"
        )


__all__ = [
    "COMPACTING_STATUSES",
    "HISTORY_COLLECTIONS",
    "LIFECYCLE_COLLECTIONS",
    "MEASUREMENT_LEDGERS",
    "DocumentResidencyFinding",
    "StagedRecord",
    "document_residency_findings",
    "document_row",
    "ledger_record",
    "require_document_holds_only_work_in_flight",
    "stage_cutover",
    "staged_records",
]
