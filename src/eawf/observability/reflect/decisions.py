"""Read a tree's native decisions, without a daemon and without a lock.

After the cutover a decision is filed into the selected generation's
decision ledger, never into the frozen epoch-1 ``state.json``, so a reader
that wants the decisions taken since reads that ledger. The ledger also
carries every epoch-1 decision the cutover imported; those still load from
``state.json`` and are passed over here so a caller merging the two sees
each decision once.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from eawf.kernel.migration.epoch2.cutover import ROW_PAYLOAD_FIELD
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.decision import Decision, DecisionStatus
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

#: The line statuses a native decision is filed under; an imported epoch-1
#: row carries the cutover's own status instead.
_NATIVE_STATUSES: Final = frozenset(status.value for status in DecisionStatus)


def read_native_decisions(tree_root: Path) -> tuple[Decision, ...]:
    """Return every native decision the tree's selected generation holds, in ledger order.

    Only the standing line of each decision is read, so a decision that was
    ratified, superseded or made obsolete reads back in its latest state.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        One decision per native key. A tree still at epoch 1 holds no native
        decision, so it reads as empty.

    Raises:
        LedgerTornTailError: The decision ledger ends mid-line.
        ValidationError: A ledger line, or the decision a native line
            carries, does not satisfy its model.
    """
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        return ()
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    lines = effective_records(
        read_ledger_records(ledger_path(document_path, Epoch2Collection.DECISION))
    )
    decisions = tuple(
        Decision.model_validate(line.payload[ROW_PAYLOAD_FIELD])
        for line in lines
        if line.status in _NATIVE_STATUSES
    )
    logger.debug(f"read_native_decisions root={tree_root.name} decisions={len(decisions)}")
    return decisions


__all__ = ["read_native_decisions"]
