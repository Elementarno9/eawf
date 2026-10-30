"""Read a tree's Runs and their event lines, without a daemon and without a lock.

Reading needs no daemon: the selected generation's document holds every live Run
record and the run ledger holds every event line and every compacted terminal Run,
so a reader resolves the generation the epoch marker names and reads both files as
they stand. It
writes nothing, takes no lock and allocates no ordinal, which is what lets
the reflection verbs run at any time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from eawf.kernel.identity import project_code_of
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import RunEventRecord
from eawf.kernel.runtime.sandbox_decision import SandboxDecision, sandbox_decisions
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.base import RunKey
from eawf.kernel.state.epoch2.run import BatchScope, MilestoneScope, Run, TaskScope
from eawf.kernel.state.epoch2.urns import BatchUrn, MilestoneUrn, TaskUrn, TrackUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    latest_rows,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.measurement.fold import SubtreeFold, fold_subtree
from eawf.runtime.daemon.run_events import run_events_of

logger = logging.getLogger(__name__)

#: The document key the tree's committed canonical high-water mark is kept under.
CANONICAL_SEQUENCE_KEY = "canonical_sequence"


@dataclass(frozen=True, slots=True, kw_only=True)
class RunReading:
    """One Run as the tree holds it, with the event lines filed against it.

    Attributes:
        run: The validated Run record.
        events: The Run's event lines, in ledger order.
        canonical_sequence: The tree's committed canonical sequence at the read,
            which is the projection revision the reading stands at.
        fold: The Run's delegation subtree folded into it, or ``None`` when it
            delegated nothing.
        sandbox_decisions: Every authorisation the gateway decided for the Run's calls,
            allowed and denied, in ledger order.
        own_project: Whether the Run belongs to the project the tree holds; a tree
            that records no project row owns no Run.
        milestone_key: The Milestone the Run's scope sits under, or ``None``.
        track_keys: The Tracks that Milestone serves, primary first.
    """

    run: Run
    events: tuple[RunEventRecord, ...]
    canonical_sequence: int
    fold: SubtreeFold | None = None
    sandbox_decisions: tuple[SandboxDecision, ...] = ()
    own_project: bool = False
    milestone_key: str | None = None
    track_keys: tuple[str, ...] = ()


class _TaskPlacement(BaseModel):
    # The stored Task is validated in full by its writer; the reader takes only the
    # Batch it is placed in, and an imported row that wraps its record has none.
    model_config = ConfigDict(extra="ignore", frozen=True)

    batch_ref: BatchUrn | None = None


class _BatchPlacement(BaseModel):
    # A projection of the stored Batch: only the Milestone it ships under.
    model_config = ConfigDict(extra="ignore", frozen=True)

    milestone_ref: MilestoneUrn | None = None


class _MilestonePlacement(BaseModel):
    # A projection of the stored Milestone: only the Tracks it serves.
    model_config = ConfigDict(extra="ignore", frozen=True)

    primary_track_ref: TrackUrn | None = None
    contributing_track_refs: tuple[TrackUrn, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class _Placement:
    """Where the tree's Tasks, Batches and Milestones sit, keyed by public key.

    Attributes:
        projects: The project codes the tree holds a project row for.
        task_batches: The Batch key each placed Task is in.
        batch_milestones: The Milestone key each Batch ships under.
        milestone_tracks: The Track keys each Milestone serves, primary first.
    """

    projects: frozenset[str]
    task_batches: Mapping[str, str]
    batch_milestones: Mapping[str, str]
    milestone_tracks: Mapping[str, tuple[str, ...]]

    def milestone_of(self, run: Run) -> str | None:
        """Return the Milestone *run*'s scope sits under, or ``None``."""
        scope = run.scope
        if isinstance(scope, MilestoneScope):
            return scope.milestone_ref.entity_key
        if isinstance(scope, BatchScope):
            return self.batch_milestones.get(scope.batch_ref.entity_key)
        if isinstance(scope, TaskScope):
            batch = self.task_batches.get(scope.task_ref.entity_key)
            return None if batch is None else self.batch_milestones.get(batch)
        return None


def _standing_rows(
    document: dict[str, Any], document_path: Path, collection: Epoch2Collection
) -> dict[str, Any]:
    """Return every row of *collection*, the document's live row over its compacted line."""
    return {
        **latest_rows(ledger_path(document_path, collection)),
        **document_rows(document, collection),
    }


def _placement(document: dict[str, Any], document_path: Path) -> _Placement:
    """Return where the tree's Tasks, Batches and Milestones sit.

    Raises:
        pydantic.ValidationError: A stored reference is not a URN of its kind.
    """
    tasks = {
        key: _TaskPlacement.model_validate(row)
        for key, row in _standing_rows(document, document_path, Epoch2Collection.TASK).items()
    }
    batches = {
        key: _BatchPlacement.model_validate(row)
        for key, row in _standing_rows(document, document_path, Epoch2Collection.BATCH).items()
    }
    milestones = {
        key: _MilestonePlacement.model_validate(row)
        for key, row in _standing_rows(document, document_path, Epoch2Collection.MILESTONE).items()
    }
    return _Placement(
        projects=frozenset(document_rows(document, Epoch2Collection.PROJECT)),
        task_batches={
            key: row.batch_ref.entity_key for key, row in tasks.items() if row.batch_ref is not None
        },
        batch_milestones={
            key: row.milestone_ref.entity_key
            for key, row in batches.items()
            if row.milestone_ref is not None
        },
        milestone_tracks={
            key: tuple(
                ref.entity_key
                for ref in (
                    *(() if row.primary_track_ref is None else (row.primary_track_ref,)),
                    *row.contributing_track_refs,
                )
            )
            for key, row in milestones.items()
        },
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _TreeRuns:
    """Every Run record of the selected generation, with what a reading is built from.

    Attributes:
        runs: One record per Run key across both tiers, in key order.
    """

    runs: tuple[Run, ...]
    records: tuple[LedgerRecord, ...]
    canonical_sequence: int
    decided: tuple[SandboxDecision, ...]
    placement: _Placement

    def reading(self, run: Run) -> RunReading:
        """Return *run*'s reading, its subtree folded over both tiers and its scope placed."""
        milestone = self.placement.milestone_of(run)
        return RunReading(
            run=run,
            events=run_events_of(self.records, run.urn),
            canonical_sequence=self.canonical_sequence,
            fold=fold_subtree(run, self.runs),
            sandbox_decisions=tuple(
                item for item in self.decided if item.run_ref.entity_key == run.key
            ),
            own_project=project_code_of(run.urn.project_key) in self.placement.projects,
            milestone_key=milestone,
            track_keys=()
            if milestone is None
            else self.placement.milestone_tracks.get(milestone, ()),
        )


def _document_path(tree_root: Path) -> Path | None:
    """Return the selected generation's document, or ``None`` at epoch 1."""
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        logger.debug(f"read_tree_runs root={tree_root.name} epoch={authority.epoch} runs=0")
        return None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _run_payloads(
    rows: dict[str, Any], records: tuple[LedgerRecord, ...]
) -> dict[str, dict[str, Any]]:
    """Return each Run's stored payload, the document's live row over its ledger line."""
    # A terminal Run is compacted out of the document into the run ledger, and the
    # last standing line of its key is its record; most finished Runs, and the
    # delegated children a subtree folds, live only there.
    return {
        item.record_key: item.payload
        for item in effective_records(records)
        if "payload_kind" not in item.payload
        and item.payload.get("key") == item.record_key
        and item.record_key not in rows
    } | rows


def _tree_runs(tree_root: Path) -> _TreeRuns | None:
    """Return the Runs the tree's selected generation holds, or ``None`` at epoch 1.

    Raises:
        pydantic.ValidationError: A Run row, or a reference a Run is placed by, does
            not validate.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    document_path = _document_path(tree_root)
    if document_path is None:
        return None
    document = read_document(document_path)
    rows = document_rows(document, Epoch2Collection.RUN)
    records = read_ledger_records(ledger_path(document_path, Epoch2Collection.RUN))
    payloads = _run_payloads(rows, records)
    return _TreeRuns(
        runs=tuple(Run.model_validate(payloads[key]) for key in sorted(payloads)),
        records=records,
        canonical_sequence=int(document.get(CANONICAL_SEQUENCE_KEY, 0)),
        decided=sandbox_decisions(
            read_ledger_records(ledger_path(document_path, Epoch2Collection.RECEIPT))
        ),
        placement=_placement(document, document_path),
    )


def read_tree_runs(tree_root: Path) -> tuple[RunReading, ...]:
    """Return every Run the tree's selected generation holds, live or compacted, in key order.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        One reading per Run key, read from the document while the Run is live
        and from the run ledger once it is compacted. A tree still at epoch 1
        holds no native Run, so it reads as empty.

    Raises:
        pydantic.ValidationError: A Run row or an event line does not validate,
            which means the store is corrupt rather than merely unfamiliar.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    tree = _tree_runs(tree_root)
    if tree is None:
        return ()
    readings = tuple(tree.reading(run) for run in tree.runs)
    logger.debug(f"read_tree_runs root={tree_root.name} runs={len(readings)}")
    return readings


def read_tree_run(tree_root: Path, ref: str) -> RunReading:
    """Return the one Run a key or URN names, from the document or the run ledger.

    A finished Run is compacted out of the document, and it is the Run whose
    report is most often asked for, so the ledger's effective record answers
    for it.

    Args:
        tree_root: The tree's ``.ea`` directory.
        ref: The Run's key (``RUN-...``) or its qualified URN.

    Returns:
        The Run's reading.

    Raises:
        LookupError: Neither tier holds a Run under that key.
    """
    key = ref.rsplit("/", 1)[-1]
    tree = _tree_runs(tree_root)
    if tree is not None:
        for run in tree.runs:
            if run.key == key:
                return tree.reading(run)
    raise LookupError(f"the tree holds no Run keyed {key!r}")


class _BriefScope(BaseModel):
    # A projection of the stored scope: only the Task a Task scope names.
    model_config = ConfigDict(extra="ignore", frozen=True)

    task_ref: TaskUrn | None = None


class RunBrief(BaseModel):
    """A Run's key, the Task its scope names and its end stamp, and nothing else.

    A projection of the stored Run, which its writer validated in full, so a
    reader that only selects Runs need not validate or fold each whole record.

    Attributes:
        key: The Run's key.
        scope: The Run's scope, holding only the Task it names, if any.
        ended_at: When the Run stopped; ``None`` while it has not.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    key: RunKey
    scope: _BriefScope
    ended_at: UtcDatetime | None = None

    @property
    def task_key(self) -> str | None:
        """Return the key of the Task the Run's scope names, or ``None``."""
        return None if self.scope.task_ref is None else self.scope.task_ref.entity_key


def read_run_briefs(tree_root: Path) -> tuple[RunBrief, ...]:
    """Return a brief of every Run the tree's selected generation holds, in key order.

    Each Run is read once from its stored row and nothing is folded across Runs,
    so the cost grows with the Run count, where :func:`read_tree_runs` grows
    with its square.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        One brief per Run key, live or compacted; empty for a tree at epoch 1.

    Raises:
        pydantic.ValidationError: A Run row's key, Task reference or end stamp
            does not validate.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    document_path = _document_path(tree_root)
    if document_path is None:
        return ()
    payloads = _run_payloads(
        document_rows(read_document(document_path), Epoch2Collection.RUN),
        read_ledger_records(ledger_path(document_path, Epoch2Collection.RUN)),
    )
    briefs = tuple(RunBrief.model_validate(payloads[key]) for key in sorted(payloads))
    logger.debug(f"read_run_briefs root={tree_root.name} runs={len(briefs)}")
    return briefs


__all__ = [
    "CANONICAL_SEQUENCE_KEY",
    "RunBrief",
    "RunReading",
    "read_run_briefs",
    "read_tree_run",
    "read_tree_runs",
]
