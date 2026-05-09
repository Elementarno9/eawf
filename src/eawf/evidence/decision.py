"""Decision-area mutators: add / list.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.cli.errors import InvalidInput
from eawf.evidence import _io
from eawf.state.enums import DecisionStatus, StoreKind
from eawf.state.models import Decision, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def add_decision(
    state: State,
    *,
    decision_id: str,
    scope_id: str,
    summary: str,
    rationale: str,
    alternatives: list[str] | None = None,
) -> tuple[Envelope, Envelope]:
    """Add a new decision in place; return (record, event) envelopes."""
    decisions: dict[str, Decision] = dict(state.decisions or {})
    if decision_id in decisions:
        raise InvalidInput(f"decision {decision_id!r} already exists")
    if not rationale.strip():
        raise InvalidInput(f"decision {decision_id!r} must include a non-empty rationale")

    now = datetime.now(UTC)
    decision = Decision(
        id=decision_id,
        scope_id=scope_id,
        summary=summary,
        rationale=rationale,
        alternatives=list(alternatives or []),
        status=DecisionStatus.ACTIVE,
        created_at=now,
        superseded_by=None,
    )
    decisions[decision_id] = decision
    state.decisions = decisions
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=decision_id,
        kind=StoreKind.DECISION,
        scope_id=scope_id,
        summary=f"decision {decision_id}: {summary[:100]}",
        payload={
            "summary": summary,
            "rationale": rationale,
            "alternatives": list(alternatives or []),
        },
    )
    event = _io.event_envelope(
        event_id=f"EVT-decision-add-{decision_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="decision.add",
        actor="cli",
        command="decision add",
        args={"decision_id": decision_id, "summary": summary},
        summary=f"decision {decision_id} added",
    )
    return record, event


def list_decisions(state: State, *, scope_id: str | None = None) -> list[Decision]:
    """Filtered list of decisions, sorted by id."""
    out: list[Decision] = []
    for decision in (state.decisions or {}).values():
        if scope_id is not None and decision.scope_id != scope_id:
            continue
        out.append(decision)
    out.sort(key=lambda d: d.id)
    return out
