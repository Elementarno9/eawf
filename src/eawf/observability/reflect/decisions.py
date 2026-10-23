"""Read a tree's native decisions, and explain one, without a daemon and without a lock.

After the cutover a decision is filed into the selected generation's
decision ledger, never into the frozen epoch-1 ``state.json``, so a reader
that wants the decisions taken since reads that ledger. The ledger also
carries every epoch-1 decision the cutover imported; those still load from
``state.json`` and are passed over here so a caller merging the two sees
each decision once.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class WhyReference(BaseModel):
    """One record an ``eawf why`` answer cites.

    Attributes:
        ref: The cited record's key or URN.
        kind: What the record is: ``scope``, ``evidence``, ``question``,
            ``supersedes`` or ``superseded_by``.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    kind: Literal["scope", "evidence", "question", "supersedes", "superseded_by"]


class WhyResult(BaseModel):
    """The typed ``eawf why`` answer for one native decision.

    Attributes:
        kind: The entity kind explained.
        id: The decision's key.
        title: The decision in one line.
        status: Its lifecycle status, so a superseded answer reads as history.
        summary: What was decided and why.
        refs: The scope, evidence, questions and supersession links it cites.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["decision"] = "decision"
    id: str
    title: str
    status: str
    summary: str
    refs: list[WhyReference] = Field(default_factory=list)


#: The URN path segment a decision address carries before its key.
_DECISION_SEGMENT: Final = "/decision/"


def explain_decision(decisions: Sequence[Decision], target: str) -> WhyResult:
    """Explain the native decision *target* names.

    Args:
        decisions: The selected generation's native decisions.
        target: A decision key (``D01``) or an address ending in
            ``/decision/<key>``.

    Returns:
        The decision's provenance: its rationale, the scope it governs, the
        evidence and questions it cites, and its supersession links.

    Raises:
        KeyError: The generation holds no decision under that key.
    """
    key = target.rsplit(_DECISION_SEGMENT, 1)[-1]
    found = next((d for d in decisions if d.key == key), None)
    if found is None:
        raise KeyError(f"why target not found: {target!r} names no decision the generation holds")
    refs = [WhyReference(ref=str(found.scope_ref), kind="scope")]
    refs += [WhyReference(ref=str(ref), kind="evidence") for ref in found.evidence_refs]
    refs += [WhyReference(ref=str(ref), kind="question") for ref in found.resolved_question_refs]
    if found.supersedes is not None:
        refs.append(WhyReference(ref=found.supersedes, kind="supersedes"))
    if found.superseded_by is not None:
        refs.append(WhyReference(ref=found.superseded_by, kind="superseded_by"))
    return WhyResult(
        id=found.key,
        title=found.title,
        status=found.status.value.lower(),
        summary=f"{found.decision} Rationale: {found.rationale}",
        refs=refs,
    )


__all__ = ["WhyReference", "WhyResult", "explain_decision", "read_native_decisions"]
