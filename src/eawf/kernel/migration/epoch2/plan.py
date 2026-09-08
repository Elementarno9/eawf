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
from eawf.kernel.migration.epoch2.registry import mapping_rule_index
from eawf.kernel.migration.epoch2.rules import MappingRuleVersion, StrictMigrationModel
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
