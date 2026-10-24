"""Read a tree's decisions, and explain one, without a daemon and without a lock.

After the cutover a decision is filed into the selected generation's
decision ledger, never into the frozen epoch-1 ``state.json``, so a reader
that wants the decisions taken since reads that ledger. The ledger also
carries every epoch-1 decision the cutover imported, under the cutover's
own line status and in the epoch-1 shape; :func:`read_native_decisions`
passes them over and :func:`read_imported_decisions` reads them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.migration.epoch2.cutover import HISTORY_RECORD_STATUS, ROW_PAYLOAD_FIELD
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.decision import Decision, DecisionStatus
from eawf.kernel.state.models import Decision as ImportedDecision
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

#: The line statuses a native decision is filed under; an imported epoch-1
#: row carries the cutover's own status instead.
_NATIVE_STATUSES: Final = frozenset(status.value for status in DecisionStatus)


def _standing_decision_lines(tree_root: Path) -> tuple[LedgerRecord, ...]:
    """Return the standing line of every decision the selected generation holds."""
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        return ()
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return effective_records(
        read_ledger_records(ledger_path(document_path, Epoch2Collection.DECISION))
    )


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
    decisions = tuple(
        Decision.model_validate(line.payload[ROW_PAYLOAD_FIELD])
        for line in _standing_decision_lines(tree_root)
        if line.status in _NATIVE_STATUSES
    )
    logger.debug(f"read_native_decisions root={tree_root.name} decisions={len(decisions)}")
    return decisions


def read_imported_decisions(tree_root: Path) -> tuple[ImportedDecision, ...]:
    """Return every epoch-1 decision the cutover imported into the selected generation.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        One decision per imported key whose standing line is still the
        import, in ledger order; empty for a tree born at epoch 2 or still
        at epoch 1.

    Raises:
        LedgerTornTailError: The decision ledger ends mid-line.
        ValidationError: A ledger line, or the epoch-1 decision an imported
            line carries, does not satisfy its model.
    """
    decisions = tuple(
        ImportedDecision.model_validate(line.payload[ROW_PAYLOAD_FIELD])
        for line in _standing_decision_lines(tree_root)
        if line.status == HISTORY_RECORD_STATUS
    )
    logger.debug(f"read_imported_decisions root={tree_root.name} decisions={len(decisions)}")
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


def explain_decision(
    decisions: Sequence[Decision], imported: Sequence[ImportedDecision], target: str
) -> WhyResult:
    """Explain the native or imported decision *target* names.

    Args:
        decisions: The selected generation's native decisions.
        imported: The epoch-1 decisions the generation imported.
        target: A decision key (``D01``) or an address ending in
            ``/decision/<key>``.

    Returns:
        The decision's provenance: its rationale, the scope it governs, the
        evidence and questions it cites, and its supersession links. An
        imported decision cites only its scope and its successor, the
        links the epoch-1 shape records.

    Raises:
        KeyError: The generation holds no decision under that key.
    """
    key = target.rsplit(_DECISION_SEGMENT, 1)[-1]
    found = next((d for d in decisions if d.key == key), None)
    if found is None:
        legacy = next((d for d in imported if d.id == key), None)
        if legacy is None:
            raise KeyError(
                f"why target not found: {target!r} names no decision the generation holds"
            )
        return _explain_imported(legacy)
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


def _explain_imported(decision: ImportedDecision) -> WhyResult:
    """Explain one imported epoch-1 decision from the fields its shape carries."""
    refs = [WhyReference(ref=decision.scope_id, kind="scope")]
    if decision.superseded_by is not None:
        refs.append(WhyReference(ref=decision.superseded_by, kind="superseded_by"))
    return WhyResult(
        id=decision.id,
        title=decision.title,
        status=decision.status.value,
        summary=f"{decision.description or decision.title} Rationale: {decision.rationale}",
        refs=refs,
    )


__all__ = [
    "WhyReference",
    "WhyResult",
    "explain_decision",
    "read_imported_decisions",
    "read_native_decisions",
]
