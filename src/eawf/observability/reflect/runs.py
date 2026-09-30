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
from eawf.kernel.store.ledger import effective_records, read_ledger_records
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


def read_tree_runs(tree_root: Path) -> tuple[RunReading, ...]:
    """Return every Run the tree's selected generation holds, in key order.

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
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        logger.debug(f"read_tree_runs root={tree_root.name} epoch={authority.epoch} runs=0")
        return ()
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    rows = document_rows(document, Epoch2Collection.RUN)
    records = read_ledger_records(ledger_path(document_path, Epoch2Collection.RUN))
    sequence = int(document.get(CANONICAL_SEQUENCE_KEY, 0))
    live = [Run.model_validate(rows[key]) for key in sorted(rows)]
    # A delegated child usually finishes first, so it is read from the ledger it
    # was compacted into; a subtree read off the document alone would miss it.
    compacted = [
        Run.model_validate(item.payload)
        for item in effective_records(records)
        if "payload_kind" not in item.payload
        and item.payload.get("key") == item.record_key
        and item.record_key not in rows
    ]
    tree = (*live, *compacted)
    decided = sandbox_decisions(
        read_ledger_records(ledger_path(document_path, Epoch2Collection.RECEIPT))
    )
    readings = [
        RunReading(
            run=run,
            events=run_events_of(records, run.urn),
            canonical_sequence=sequence,
            fold=fold_subtree(run, tree),
            sandbox_decisions=tuple(item for item in decided if item.run_ref.entity_key == run.key),
        )
        for run in live
    ]
    logger.debug(f"read_tree_runs root={tree_root.name} runs={len(readings)}")
    return tuple(readings)


def read_tree_run(tree_root: Path, ref: str) -> RunReading:
    """Return the one Run a key or URN names.

    Args:
        tree_root: The tree's ``.ea`` directory.
        ref: The Run's key (``RUN-...``) or its qualified URN.

    Returns:
        The Run's reading.

    Raises:
        LookupError: The tree holds no Run under that key.
    """
    key = ref.rsplit("/", 1)[-1]
    for reading in read_tree_runs(tree_root):
        if reading.run.key == key:
            return reading
    raise LookupError(f"the tree holds no Run keyed {key!r}")


__all__ = ["CANONICAL_SEQUENCE_KEY", "RunReading", "read_tree_run", "read_tree_runs"]
