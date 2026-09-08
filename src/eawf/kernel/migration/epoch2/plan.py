"""Applying the backlog and criteria rules to a whole source collection.

The rule modules each decide one thing about one row. This module is
where they are put together into the plan the cutover consumes, so the
rules have exactly one production call site and a reader can see, in one
place, the order the importer applies them in.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.allowlist import load_legacy_symbol_allowlist
from eawf.kernel.migration.epoch2.backlog import (
    DROPPED_TARGET_STATUS,
    BacklogResolution,
    ObsolescenceSweepResult,
    ResolutionCorpus,
    classify_backlog_resolution,
    sweep_backlog_obsolescence,
)
from eawf.kernel.migration.epoch2.criteria import ImportedCriterion, convert_criterion
from eawf.kernel.migration.epoch2.dispositions import DropProofForm, drop_proof_form
from eawf.kernel.migration.epoch2.lifecycle import (
    SOURCE_COLLECTIONS,
    ImportedLifecycleRecord,
    LifecycleSourceIndex,
    MintedRun,
    map_backlog_row,
    map_iter_row,
    map_phase_row,
    map_wave_row,
)
from eawf.kernel.migration.epoch2.registry import mapping_rule_index
from eawf.kernel.migration.epoch2.rules import MappingRuleVersion, StrictMigrationModel
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.migration.epoch2.status_map import (
    SourceLifecycle,
    apply_annotated_defaults,
    compact_annotations,
    map_source_status,
)
from eawf.kernel.spec.common import CriterionSpec, GateSpec

logger = logging.getLogger(__name__)


class ImportedBacklogRow(StrictMigrationModel):
    """One epoch-1 backlog row as the importer will write it.

    Attributes:
        backlog_id: The source row id, preserved.
        target_status: ``DRAFT`` or ``DROPPED``.
        record: The row with its annotated defaults filled.
        annotations: Every annotation the row earned at import.
        ledger_annotations: The subset that survives compaction, so a
            terminal row's ledger form is fixed at import time rather
            than recomputed later.
        resolution: The classified fate, present only on a closed row.
        obsolete: Whether the row names something the cutover deletes.
    """

    backlog_id: Annotated[str, Field(min_length=1)]
    target_status: Annotated[str, Field(min_length=1)]
    record: dict[str, Any]
    annotations: tuple[str, ...]
    ledger_annotations: tuple[str, ...]
    resolution: BacklogResolution | None
    obsolete: bool


class BacklogImportPlan(StrictMigrationModel):
    """The epoch-1 backlog collection as the importer will write it."""

    drop_proof: DropProofForm | None
    rows: tuple[ImportedBacklogRow, ...]
    obsolescence: ObsolescenceSweepResult
    mapping_rules: tuple[MappingRuleVersion, ...]

    @classmethod
    def build(
        cls,
        *,
        source: Mapping[str, Mapping[str, Any]] | None,
        corpus: ResolutionCorpus,
        allowlist_path: Path,
    ) -> BacklogImportPlan:
        """Apply every backlog rule to ``source`` and return the plan.

        A source collection with no rows reaches ``explicit_drop`` and
        carries its proof form, which distinguishes a present-but-empty
        container from a key that serialized as JSON ``null``.

        Args:
            source: The raw ``backlog`` value from the epoch-1 document.
            corpus: The identifier populations the classifier resolves
                against.
            allowlist_path: Location of the shared allowed-legacy-symbol
                allowlist.

        Returns:
            The import plan, with rows in sorted-id order.

        Raises:
            FileNotFoundError: When ``allowlist_path`` does not exist.
            ValueError: When the allowlist cannot be parsed.
            MigrationCountMismatchError: When a row carries a status
                outside the map, cannot default its intent, or reaches no
                classifier arm.
        """
        allowlist = load_legacy_symbol_allowlist(allowlist_path)
        rules = tuple(mapping_rule_index(allowlist).values())

        if not source:
            return cls(
                drop_proof=drop_proof_form(source),
                rows=(),
                obsolescence=sweep_backlog_obsolescence(rows={}, allowlist=allowlist),
                mapping_rules=rules,
            )

        sweep = sweep_backlog_obsolescence(rows=source, allowlist=allowlist)
        obsolete = set(sweep.obsolete_ids)

        rows: list[ImportedBacklogRow] = []
        for backlog_id in sorted(source):
            source_row = source[backlog_id]
            target_status = map_source_status(
                SourceLifecycle.BACKLOG, str(source_row.get("status"))
            )
            record, annotations = apply_annotated_defaults(source_row)
            resolution: BacklogResolution | None = None
            if target_status == DROPPED_TARGET_STATUS:
                resolution = classify_backlog_resolution(
                    backlog_id=backlog_id, row=source_row, corpus=corpus
                )
                annotations = annotations + resolution.annotations
            rows.append(
                ImportedBacklogRow(
                    backlog_id=backlog_id,
                    target_status=target_status,
                    record=record,
                    annotations=annotations,
                    ledger_annotations=compact_annotations(annotations),
                    resolution=resolution,
                    obsolete=backlog_id in obsolete,
                )
            )

        return cls(
            drop_proof=None,
            rows=tuple(rows),
            obsolescence=sweep,
            mapping_rules=rules,
        )


class CriteriaImportPlan(StrictMigrationModel):
    """The epoch-1 criteria and gates as one list of epoch-2 criteria."""

    criteria: tuple[ImportedCriterion, ...]

    @classmethod
    def build(
        cls,
        *,
        criteria: Iterable[CriterionSpec],
        gates: Iterable[GateSpec],
    ) -> CriteriaImportPlan:
        """Convert every criterion, binding each to the gates that score it.

        Args:
            criteria: The epoch-1 criterion rows.
            gates: Every co-resident epoch-1 gate row.

        Returns:
            The import plan, with criteria in source order.
        """
        gate_rows = list(gates)
        return cls(
            criteria=tuple(
                convert_criterion(criterion=criterion, gates=gate_rows) for criterion in criteria
            ),
        )


AUDITS_COLLECTION = "audits"
AUDIT_LEDGER = "audit"
LEDGER_ID_FIELD = "id"


def _keyed_rows(document: Mapping[str, Any], collection: str) -> dict[str, Mapping[str, Any]]:
    """Return one collection's rows, or nothing when the source holds none.

    Args:
        document: The decoded epoch-1 document.
        collection: The top-level key to read.

    Returns:
        The rows keyed by id. A key that serialized as JSON ``null`` or
        holds a non-object yields an empty mapping, because the census
        has already recorded that as a drop with its proof form.
    """
    value = document.get(collection)
    if not isinstance(value, dict):
        return {}
    return {key: row for key, row in value.items() if isinstance(row, dict)}


def _audit_population(snapshot: SourceSnapshot) -> frozenset[str]:
    """Return the audit ids the classifier resolves against.

    The population is the union of the document collection and the audit
    ledger: the ledger is part of the source, not a cache of the
    document, so a row only it holds still resolves.

    Args:
        snapshot: The frozen epoch-1 corpus.

    Returns:
        Every audit id either input holds.

    Raises:
        MigrationSourceUnreadableError: When the snapshot holds no audit
            ledger. A missing ledger is not an empty one.
    """
    document_ids = frozenset(_keyed_rows(snapshot.document, AUDITS_COLLECTION))
    ledger_ids = frozenset(
        row[LEDGER_ID_FIELD]
        for row in snapshot.ledger(AUDIT_LEDGER)
        if isinstance(row.get(LEDGER_ID_FIELD), str) and row[LEDGER_ID_FIELD]
    )
    return document_ids | ledger_ids


def _wave_criteria(row: Mapping[str, Any]) -> tuple[ImportedCriterion, ...]:
    """Convert one wave's success criteria through the criteria disposition.

    Args:
        row: The source wave row.

    Returns:
        The imported criteria, in source order; empty when the wave
        recorded none.

    Raises:
        ValidationError: When a criterion or gate row does not satisfy
            the epoch-1 spec models. A criterion the importer cannot read
            is a plan failure, never a silently skipped row.
    """
    criteria = row.get("success_criteria") or ()
    gates = row.get("gates") or ()
    return CriteriaImportPlan.build(
        criteria=[CriterionSpec.model_validate(item) for item in criteria],
        gates=[GateSpec.model_validate(item) for item in gates],
    ).criteria


class LifecycleImportPlan(StrictMigrationModel):
    """The four epoch-1 lifecycle collections as the importer will write them.

    Attributes:
        source_index: The populations the mappers resolved against.
        records: One imported record per source row, ordered by
            collection and then by source id, so two passes over one
            snapshot agree row for row.
        backlog: The backlog plan the Task records were lifted from.
        mapping_rules: The rule versions this plan was produced under.
    """

    source_index: LifecycleSourceIndex
    records: tuple[ImportedLifecycleRecord, ...]
    backlog: BacklogImportPlan
    mapping_rules: tuple[MappingRuleVersion, ...]

    @classmethod
    def build(cls, *, snapshot: SourceSnapshot, allowlist_path: Path) -> LifecycleImportPlan:
        """Map every phase, iter, wave and backlog row of ``snapshot``.

        The backlog is mapped through :class:`BacklogImportPlan` first, so
        the classifier and the obsolescence sweep run exactly once and the
        lifecycle record carries their verdicts rather than recomputing
        them under a second set of rules.

        Args:
            snapshot: The frozen epoch-1 corpus.
            allowlist_path: Location of the shared allowed-legacy-symbol
                allowlist.

        Returns:
            The lifecycle import plan.

        Raises:
            FileNotFoundError: When ``allowlist_path`` does not exist.
            MigrationSourceUnreadableError: When the snapshot holds no
                audit ledger.
            MigrationCountMismatchError: When a row carries a status
                outside the closed map, cannot default its intent, or
                reaches no classifier arm.
            ValidationError: When a wave criterion or gate is unreadable.
        """
        document = snapshot.document
        index = LifecycleSourceIndex.build(document)
        phases = _keyed_rows(document, SOURCE_COLLECTIONS[SourceLifecycle.PHASE])
        iters = _keyed_rows(document, SOURCE_COLLECTIONS[SourceLifecycle.ITER])
        waves = _keyed_rows(document, SOURCE_COLLECTIONS[SourceLifecycle.WAVE])
        backlog_rows = _keyed_rows(document, SOURCE_COLLECTIONS[SourceLifecycle.BACKLOG])

        corpus = ResolutionCorpus.build(
            wave_ids=sorted(waves),
            backlog_ids=backlog_rows.keys(),
            audit_ids=_audit_population(snapshot),
        )
        backlog = BacklogImportPlan.build(
            source=backlog_rows or None,
            corpus=corpus,
            allowlist_path=allowlist_path,
        )

        records: list[ImportedLifecycleRecord] = []
        for source_id in sorted(phases):
            records.append(map_phase_row(source_id=source_id, row=phases[source_id], index=index))
        for source_id in sorted(iters):
            records.append(map_iter_row(source_id=source_id, row=iters[source_id], index=index))
        for source_id in sorted(waves):
            row = waves[source_id]
            records.append(
                map_wave_row(
                    source_id=source_id,
                    row=row,
                    criteria=_wave_criteria(row),
                    index=index,
                )
            )
        for imported in backlog.rows:
            records.append(
                map_backlog_row(
                    source_id=imported.backlog_id,
                    row=backlog_rows[imported.backlog_id],
                    target_status=imported.target_status,
                    filled_record=imported.record,
                    annotations=imported.annotations,
                    resolution=imported.resolution,
                    obsolete=imported.obsolete,
                    index=index,
                )
            )

        return cls(
            source_index=index,
            records=tuple(records),
            backlog=backlog,
            mapping_rules=backlog.mapping_rules,
        )

    def for_lifecycle(self, lifecycle: SourceLifecycle) -> tuple[ImportedLifecycleRecord, ...]:
        """Return every imported record produced from one source collection.

        Args:
            lifecycle: Which source collection to select.

        Returns:
            The records, in source-id order.
        """
        return tuple(record for record in self.records if record.lifecycle is lifecycle)

    def record_for(self, source_id: str) -> ImportedLifecycleRecord:
        """Return the record imported from one source row.

        Args:
            source_id: The source row's own id.

        Returns:
            Its imported record.

        Raises:
            KeyError: When no source row of that id was imported.
        """
        for record in self.records:
            if record.origin.source_id == source_id:
                return record
        raise KeyError(source_id)

    def minted_runs(self) -> tuple[MintedRun, ...]:
        """Return every Run the source supports, across all records."""
        return tuple(run for record in self.records for run in record.minted_runs)
