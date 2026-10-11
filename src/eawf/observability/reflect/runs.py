"""Read a tree's Runs and their event lines, without a daemon and without a lock.

Reading needs no daemon: the selected generation's document holds every Run
record and the run ledger holds every event line, so a reader resolves the
generation the epoch marker names and reads both files as they stand. It
writes nothing, takes no lock and allocates no ordinal, which is what lets
the reflection verbs run at any time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import RunEventRecord
from eawf.kernel.runtime.sandbox_decision import SandboxDecision, sandbox_decisions
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
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
    """

    run: Run
    events: tuple[RunEventRecord, ...]
    canonical_sequence: int
    fold: SubtreeFold | None = None
    sandbox_decisions: tuple[SandboxDecision, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class _TreeRuns:
    """Every Run record of the selected generation, with what a reading is built from."""

    live: tuple[Run, ...]
    compacted: tuple[Run, ...]
    records: tuple[LedgerRecord, ...]
    canonical_sequence: int
    decided: tuple[SandboxDecision, ...]

    def reading(self, run: Run) -> RunReading:
        """Return *run*'s reading, its subtree folded over both tiers."""
        return RunReading(
            run=run,
            events=run_events_of(self.records, run.urn),
            canonical_sequence=self.canonical_sequence,
            fold=fold_subtree(run, (*self.live, *self.compacted)),
            sandbox_decisions=tuple(
                item for item in self.decided if item.run_ref.entity_key == run.key
            ),
        )


def _tree_runs(tree_root: Path) -> _TreeRuns | None:
    """Return the Runs the tree's selected generation holds, or ``None`` at epoch 1.

    Raises:
        pydantic.ValidationError: A Run row does not validate.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        logger.debug(f"read_tree_runs root={tree_root.name} epoch={authority.epoch} runs=0")
        return None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    rows = document_rows(document, Epoch2Collection.RUN)
    records = read_ledger_records(ledger_path(document_path, Epoch2Collection.RUN))
    # A terminal Run is compacted out of the document into the run ledger, and its
    # effective line is the record; a delegated child usually finishes first, so a
    # subtree read off the document alone would miss it.
    compacted = tuple(
        Run.model_validate(item.payload)
        for item in effective_records(records)
        if "payload_kind" not in item.payload
        and item.payload.get("key") == item.record_key
        and item.record_key not in rows
    )
    return _TreeRuns(
        live=tuple(Run.model_validate(rows[key]) for key in sorted(rows)),
        compacted=compacted,
        records=records,
        canonical_sequence=int(document.get(CANONICAL_SEQUENCE_KEY, 0)),
        decided=sandbox_decisions(
            read_ledger_records(ledger_path(document_path, Epoch2Collection.RECEIPT))
        ),
    )


def read_tree_runs(tree_root: Path) -> tuple[RunReading, ...]:
    """Return every Run the tree's selected generation holds in its document, in key order.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        One reading per Run row of the document. A tree still at epoch 1
        holds no native Run, so it reads as empty.

    Raises:
        pydantic.ValidationError: A Run row or an event line does not validate,
            which means the store is corrupt rather than merely unfamiliar.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    tree = _tree_runs(tree_root)
    if tree is None:
        return ()
    readings = tuple(tree.reading(run) for run in tree.live)
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
        # The last standing ledger line of a key is its record, as the store's
        # latest-row reader holds it.
        for run in (*tree.live, *reversed(tree.compacted)):
            if run.key == key:
                return tree.reading(run)
    raise LookupError(f"the tree holds no Run keyed {key!r}")


__all__ = ["CANONICAL_SEQUENCE_KEY", "RunReading", "read_tree_run", "read_tree_runs"]
