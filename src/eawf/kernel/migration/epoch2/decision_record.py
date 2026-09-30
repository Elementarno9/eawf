"""The ledger lines one decision append becomes on a cut-over tree.

After the cutover a decision is filed through the generation's decision
ledger, and it is filed as the native :class:`~eawf.kernel.state.epoch2.decision.Decision`:
it keeps the options it weighed, the evidence it stands on, the questions
it settled and its place in a supersession chain. The epoch-1 decision
row kept none of the evidence and could not refuse a chain that cycles,
so a decision appended in that shape would be a conclusion without its
reasoning.

A decision moves only through its lifecycle functions. Appending a new
key files a proposed or an active decision; appending an active decision
that ``supersedes`` a standing one also files the retired decision's
successor line; appending a key that is already standing is accepted only
as that decision ratified or made obsolete, never as an edit. A successor
line names the digest of the line it replaces, so the ledger keeps every
revision and reads back only the current one.

This module decides; it does not write. The daemon runner reads the
ledger under its locks and commits the lines it is handed back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.migration.epoch2.cutover import ROW_PAYLOAD_FIELD
from eawf.kernel.state.epoch2.decision import (
    Decision,
    DecisionError,
    DecisionStatus,
    obsolete,
    ratify,
    supersede,
    validate_decision_chain,
)
from eawf.kernel.store.ledger import LedgerRecord, line_digest, render_ledger_line
from eawf.kernel.store.tiers import Epoch2Collection

#: The states a decision may be filed in under a key nothing holds yet.
_FILEABLE: Final = frozenset({DecisionStatus.PROPOSED, DecisionStatus.ACTIVE})


def _native(line: LedgerRecord) -> Decision | None:
    """Return the native decision *line* carries, or ``None`` for an imported epoch-1 row."""
    inner = line.payload.get(ROW_PAYLOAD_FIELD)
    try:
        return Decision.model_validate(inner)
    except ValidationError:
        return None


def _line(decision: Decision, *, at: datetime, replaces: LedgerRecord | None) -> LedgerRecord:
    """Return the ledger line filing *decision*, as a successor of *replaces* when given."""
    return LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key=decision.key,
        status=decision.status.value,
        recorded_at=at,
        supersedes=None if replaces is None else line_digest(render_ledger_line(replaces)),
        payload={ROW_PAYLOAD_FIELD: decision.model_dump(mode="json")},
    )


def _successor(previous: Decision, filed: Decision) -> Decision:
    """Return *filed* when it is *previous* ratified or made obsolete, and nothing else.

    Raises:
        DecisionError: ``decision_transition_illegal`` when *filed* is any
            other change to the standing decision.
    """
    if filed.status is DecisionStatus.ACTIVE and filed.ratified_by is not None:
        assert filed.ratified_at is not None, "the model files ratified_by with ratified_at"
        expected = ratify(previous, by=filed.ratified_by, at=filed.ratified_at)
    elif filed.status is DecisionStatus.OBSOLETE:
        expected = obsolete(previous)
    else:
        expected = None
    if expected != filed:
        raise DecisionError(
            "decision_transition_illegal",
            f"decision {filed.key} is standing; it is only ratified or made obsolete, "
            "and a changed decision is a new one that supersedes it",
        )
    return filed


def decision_lines(
    record: Mapping[str, Any], standing: Iterable[LedgerRecord], *, at: datetime
) -> tuple[LedgerRecord, ...]:
    """Return the lines appending *record* to the decision ledger writes.

    Args:
        record: The native decision as the caller supplied it.
        standing: The decision ledger's lines that no later line supersedes.
        at: When the append happens.

    Returns:
        The decision's own line, preceded by the retired decision's
        successor line when it supersedes one.

    Raises:
        ValidationError: *record* is not a complete native decision -- too
            few options with no reason, no evidence, or no consequences once
            active.
        DecisionError: ``decision_key_taken`` when an imported record holds
            the key; ``decision_transition_illegal`` when a standing key is
            re-filed as anything but its ratification or retirement, or a
            new key is filed already retired;
            ``decision_supersession_invalid`` when the superseded decision is
            not a standing native one, or the pair disagrees; and
            ``decision_chain_invalid`` when the result would dangle or cycle.
    """
    filed = Decision.model_validate(record)
    lines = tuple(standing)
    native = {line.record_key: (line, d) for line in lines if (d := _native(line)) is not None}
    imported = {line.record_key for line in lines} | {
        str(inner["id"])
        for line in lines
        if isinstance(inner := line.payload.get(ROW_PAYLOAD_FIELD), dict) and "id" in inner
    }
    decisions = {key: d for key, (_, d) in native.items()}
    written: list[LedgerRecord] = []
    if filed.key in native:
        previous_line, previous = native[filed.key]
        decisions[filed.key] = _successor(previous, filed)
        written.append(_line(filed, at=at, replaces=previous_line))
    elif filed.key in imported:
        raise DecisionError("decision_key_taken", f"the decision ledger already holds {filed.key}")
    else:
        if filed.status not in _FILEABLE:
            raise DecisionError(
                "decision_transition_illegal",
                f"a new decision is filed {' or '.join(sorted(_FILEABLE))}, "
                f"not {filed.status.value}",
            )
        if filed.supersedes is not None:
            if filed.supersedes not in native:
                raise DecisionError(
                    "decision_supersession_invalid",
                    f"{filed.supersedes} is not a standing native decision {filed.key} can "
                    "supersede",
                )
            old_line, old = native[filed.supersedes]
            decisions[old.key] = supersede(old, filed)
            written.append(_line(decisions[old.key], at=at, replaces=old_line))
        decisions[filed.key] = filed
        written.append(_line(filed, at=at, replaces=None))
    validate_decision_chain(decisions.values())
    return tuple(written)


__all__ = ["decision_lines"]
