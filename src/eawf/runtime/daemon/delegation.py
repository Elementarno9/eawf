"""Reading a root's delegation tree: its lineage, each Run's ceiling, a child's parent.

A delegation tree is never stored as a tree. Each Run names the Run that delegated it,
and a finished Run is compacted out of the document into the run ledger, so the tree is
read from both tiers: a subtree that forgot its finished children would count fewer Runs
than it spent.

A Run's ``child_runs`` ceiling is the one its sealed capsule carries when its dispatch
recorded the capsule, and otherwise the one its scope class resolves to -- a Run the host
harness spawned was never sealed a capsule, and its scope is what bounds it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.delegation import (
    SubtreeOverrun,
    resolved_child_runs,
    subtree_overrun,
)
from eawf.kernel.state.epoch2.run import RunScope
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord, effective_records
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

_SCOPE: TypeAdapter[RunScope] = TypeAdapter(RunScope)


def _run_rows(
    document: dict[str, Any], records: tuple[LedgerRecord, ...]
) -> dict[str, Mapping[str, Any]]:
    """Return every Run row either tier holds, keyed by URN; the document's wins.

    A row the epoch-2 cutover imported states no URN and can name no lineage,
    so it is not part of any tree.
    """
    compacted = [
        item.payload
        for item in effective_records(records)
        if "payload_kind" not in item.payload and item.payload.get("key") == item.record_key
    ]
    live = document_rows(document, Epoch2Collection.RUN).values()
    return {row["urn"]: row for row in (*compacted, *live) if isinstance(row.get("urn"), str)}


def bound_capsules(records: tuple[LedgerRecord, ...]) -> dict[str, AuthorityCapsule]:
    """Return each dispatched Run's sealed capsule, keyed by Run URN, where recorded."""
    capsules: dict[str, AuthorityCapsule] = {}
    for item in records:
        if item.payload.get("payload_kind") != "run_binding":
            continue
        binding = RunBinding.model_validate(item.payload)
        if binding.capsule is not None:
            capsules[str(binding.run_ref)] = binding.capsule
    return capsules


def delegation_overrun(
    document: dict[str, Any],
    records: tuple[LedgerRecord, ...],
    *,
    child: QualifiedUrn,
    parent: QualifiedUrn,
) -> SubtreeOverrun | None:
    """Return the ancestor whose ceiling *child* takes its subtree past, if any.

    Args:
        document: The root's generation document.
        records: Every line the run ledger holds.
        child: The child Run, held already or about to be created.
        parent: The Run that delegated it.

    Returns:
        The nearest ancestor passed, or ``None`` when every ancestor admits it.
    """
    rows = _run_rows(document, records)
    parents: dict[str, str | None] = {urn: row.get("parent_run_ref") for urn, row in rows.items()}
    parents[str(child)] = str(parent)
    capsules = bound_capsules(records)

    def ceiling_of(urn: str) -> int:
        capsule = capsules.get(urn)
        if capsule is not None:
            return capsule.budget.child_runs
        return resolved_child_runs(_SCOPE.validate_python(rows[urn]["scope"]))

    return subtree_overrun(parents, child=str(child), ceiling_of=ceiling_of)


def delegation_parent(
    document: dict[str, Any], records: tuple[LedgerRecord, ...], urn: QualifiedUrn
) -> tuple[str | None, AuthorityCapsule | None]:
    """Return the Run that delegated *urn* and its sealed capsule, where each exists.

    Args:
        document: The root's generation document.
        records: Every line the run ledger holds.
        urn: The Run about to be sealed.

    Returns:
        The parent's URN, or ``None`` for a root or a Run neither tier holds,
        beside the parent's recorded capsule, or ``None`` when its dispatch
        recorded none.
    """
    row = _run_rows(document, records).get(str(urn))
    parent = None if row is None else row.get("parent_run_ref")
    if parent is None:
        return None, None
    return parent, bound_capsules(records).get(parent)


__all__ = ["bound_capsules", "delegation_overrun", "delegation_parent"]
