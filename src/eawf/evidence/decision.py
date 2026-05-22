"""Decision-area mutators: add / list.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.cli.errors import InvalidInput, NotFound
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
    supersedes: str | None = None,
) -> tuple[Envelope, Envelope]:
    """Add a new decision in place; return (record, event) envelopes.

    When ``supersedes`` is provided, the named parent decision is flipped to
    :data:`DecisionStatus.SUPERSEDED` with ``superseded_by`` pointing at the
    new decision. Both writes happen in the same caller-supplied state
    transaction (the CLI handler runs them inside
    :func:`eawf.cli._mutation.state_transaction` so they land atomically).

    Raises:
        InvalidInput: when *decision_id* already exists, *rationale* is empty,
            *supersedes* names an unknown decision, *supersedes* equals
            *decision_id* (self-supersede), or the named parent is not ACTIVE.
    """
    decisions: dict[str, Decision] = dict(state.decisions or {})
    if decision_id in decisions:
        raise InvalidInput(f"decision {decision_id!r} already exists")
    if not rationale.strip():
        raise InvalidInput(f"decision {decision_id!r} must include a non-empty rationale")
    if supersedes is not None:
        if supersedes == decision_id:
            raise InvalidInput(f"decision {decision_id!r} cannot supersede itself")
        if supersedes not in decisions:
            raise InvalidInput(f"unknown decision to supersede: {supersedes!r}")
        parent = decisions[supersedes]
        if parent.status != DecisionStatus.ACTIVE:
            raise InvalidInput(
                f"decision {supersedes!r} is {parent.status.value!r}; "
                "only ACTIVE decisions can be superseded"
            )

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
    if supersedes is not None:
        parent = decisions[supersedes]
        decisions[supersedes] = parent.model_copy(
            update={
                "status": DecisionStatus.SUPERSEDED,
                "superseded_by": decision_id,
            }
        )
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
            "supersedes": supersedes,
        },
    )
    event_args: dict[str, str] = {"decision_id": decision_id, "summary": summary}
    if supersedes is not None:
        event_args["supersedes"] = supersedes
    event = _io.event_envelope(
        event_id=f"EVT-decision-add-{decision_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="decision.add",
        actor="cli",
        command="decision add",
        args=event_args,
        summary=f"decision {decision_id} added"
        + (f" (supersedes {supersedes})" if supersedes else ""),
    )
    return record, event


def supersede_decision(
    state: State,
    *,
    old_id: str,
    new_id: str,
) -> tuple[Envelope, Envelope]:
    """Supersede *old_id* by *new_id* in place; return (record, event) envelopes.

    Flips ``old_id``'s status to :data:`DecisionStatus.SUPERSEDED` and sets
    ``old_id.superseded_by = new_id``. Unlike the supersede-on-add path in
    :func:`add_decision`, both decisions must already exist — this verb links
    an existing replacement to the decision it retires. The mutation runs
    inside the caller-supplied state transaction (the CLI handler wraps it in
    :func:`eawf.cli._mutation.state_transaction`) so the flip lands atomically.

    Raises:
        NotFound: when *old_id* or *new_id* names a decision absent from state.
        InvalidInput: when *old_id* equals *new_id* (self-supersede) or
            *old_id* is not currently ACTIVE.
    """
    decisions: dict[str, Decision] = dict(state.decisions or {})
    if old_id == new_id:
        raise InvalidInput(f"decision {old_id!r} cannot supersede itself")
    if old_id not in decisions:
        raise NotFound(f"decision {old_id!r} not found")
    if new_id not in decisions:
        raise NotFound(f"superseding decision {new_id!r} not found")
    old = decisions[old_id]
    if old.status != DecisionStatus.ACTIVE:
        raise InvalidInput(
            f"decision {old_id!r} is {old.status.value!r}; only ACTIVE decisions can be superseded"
        )

    now = datetime.now(UTC)
    decisions[old_id] = old.model_copy(
        update={
            "status": DecisionStatus.SUPERSEDED,
            "superseded_by": new_id,
        }
    )
    state.decisions = decisions
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=f"{old_id}-SUPERSEDE",
        kind=StoreKind.DECISION,
        scope_id=old.scope_id,
        summary=f"decision {old_id} superseded by {new_id}",
        payload={
            "summary": old.summary,
            "superseded_by": new_id,
        },
    )
    event = _io.event_envelope(
        event_id=f"EVT-decision-supersede-{old_id}-{int(now.timestamp() * 1000)}",
        scope_id=old.scope_id,
        event_type="decision.supersede",
        actor="cli",
        command="decision supersede",
        args={"old_id": old_id, "new_id": new_id},
        summary=f"decision {old_id} superseded by {new_id}",
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
