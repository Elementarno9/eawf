"""``eawf outcome define`` and ``eawf outcome set`` mutators.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction` to serialise
load + mutate + write under ``portalock(state.json)``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.cli.errors import UserError
from eawf.evidence import _io
from eawf.evidence.guards import require_complete_audit
from eawf.state.enums import OutcomeDirection, OutcomeStatus
from eawf.state.models import Outcome, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def define_outcome(
    state: State,
    *,
    outcome_id: str,
    scope_id: str,
    metric: str,
    threshold: float,
    direction: OutcomeDirection,
) -> Envelope:
    """Create a pending :class:`Outcome` in place and return the event envelope."""
    outcomes: dict[str, Outcome] = dict(state.outcomes or {})
    if outcome_id in outcomes:
        raise UserError(f"outcome {outcome_id!r} already exists", kind="InvalidInput")

    now = datetime.now(UTC)
    outcome = Outcome(
        id=outcome_id,
        scope_id=scope_id,
        metric=metric,
        threshold=threshold,
        direction=direction,
        value=None,
        status=OutcomeStatus.PENDING,
        audit_id=None,
        updated_at=now,
    )
    outcomes[outcome_id] = outcome
    state.outcomes = outcomes
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-outcome-define-{outcome_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="outcome.define",
        actor="cli",
        command="outcome define",
        args={
            "outcome_id": outcome_id,
            "metric": metric,
            "threshold": threshold,
            "direction": direction.value,
        },
        summary=f"outcome {outcome_id} defined ({metric} {direction.value} {threshold})",
    )


def set_outcome(
    state: State,
    *,
    outcome_id: str,
    value: float,
    status: OutcomeStatus,
    audit_id: str,
) -> Envelope:
    """Record an outcome measurement in place.

    Calls :func:`require_complete_audit` *before* mutating so the verdict-
    bearing rule fails fast with ``VALIDATION_FAILED`` even when the current
    outcome would otherwise be untouched.
    """
    outcomes: dict[str, Outcome] = dict(state.outcomes or {})
    if outcome_id not in outcomes:
        raise UserError(f"outcome {outcome_id!r} not found", kind="NotFound")

    require_complete_audit(state, audit_id)

    now = datetime.now(UTC)
    prior = outcomes[outcome_id]
    updated = prior.model_copy(
        update={
            "value": value,
            "status": status,
            "audit_id": audit_id,
            "updated_at": now,
        }
    )
    outcomes[outcome_id] = updated
    state.outcomes = outcomes
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-outcome-set-{outcome_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="outcome.set",
        actor="cli",
        command="outcome set",
        args={
            "outcome_id": outcome_id,
            "value": value,
            "status": status.value,
            "audit_id": audit_id,
        },
        summary=f"outcome {outcome_id} set value={value} status={status.value}",
    )
