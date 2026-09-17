"""Integration attempt transitions and generation selection.

The serial integrator drives an attempt through its states and, when one
succeeds, makes its generation the Batch head. Both steps are pure here:
:func:`transition_attempt` returns the moved attempt or refuses an edge the
state machine does not declare, and :func:`select_generation` returns the
ledger with the new head selected and the previous head released. Every
result is re-validated, so a caller cannot hold a record the kernel models
would refuse to load.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import JsonValue

from eawf.kernel.delivery.integration import (
    INTEGRATION_ATTEMPT_EDGES,
    INTEGRATION_TERMINAL_STATUSES,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    IntegrationFailureKind,
    IntegrationGeneration,
    IntegrationGenerationLedger,
)

logger = logging.getLogger(__name__)


class IllegalIntegrationTransitionError(ValueError):
    """An attempt was asked to take an edge its state machine does not declare.

    Attributes:
        attempt_id: The attempt that refused to move.
        current: The status it holds.
        requested: The status it was asked to enter.
    """

    def __init__(
        self,
        attempt_id: str,
        current: IntegrationAttemptStatus,
        requested: IntegrationAttemptStatus,
    ) -> None:
        """Keep the refused edge.

        Args:
            attempt_id: The attempt's key.
            current: The status the attempt holds.
            requested: The status it was asked to enter.
        """
        self.attempt_id = attempt_id
        self.current = current
        self.requested = requested
        super().__init__(f"attempt {attempt_id!r} cannot move {current.value} -> {requested.value}")


def transition_attempt(
    attempt: IntegrationAttempt,
    *,
    to: IntegrationAttemptStatus,
    at: datetime,
    failure_kind: IntegrationFailureKind | None = None,
    diagnostic_ref: str | None = None,
) -> IntegrationAttempt:
    """Return *attempt* moved to *to* at *at*.

    Entering a terminal state stamps ``terminal_at`` with *at*; the failure
    kind and diagnostic are validated against the target state by the
    record itself.

    Args:
        attempt: The attempt to move.
        to: The status to enter.
        at: When the move happened; timezone-aware.
        failure_kind: Why the attempt ended, for a stale, blocked or failed
            target.
        diagnostic_ref: The evidence URN of the outcome detail; required for
            a blocked target.

    Returns:
        A new, validated attempt in *to*.

    Raises:
        IllegalIntegrationTransitionError: ``attempt.status -> to`` is not a
            declared edge.
        ValueError: *at* precedes the attempt's last update.
        pydantic.ValidationError: The failure kind or diagnostic does not
            fit *to*.
    """
    if to not in INTEGRATION_ATTEMPT_EDGES[attempt.status]:
        raise IllegalIntegrationTransitionError(attempt.id, attempt.status, to)
    if at < attempt.updated_at:
        raise ValueError(f"attempt {attempt.id!r} cannot move back in time to {at.isoformat()}")
    payload: dict[str, JsonValue] = attempt.model_dump(mode="json")
    payload.update(
        status=to.value,
        failure_kind=failure_kind.value if failure_kind is not None else None,
        diagnostic_ref=diagnostic_ref,
        updated_at=at.isoformat(),
        terminal_at=at.isoformat() if to in INTEGRATION_TERMINAL_STATUSES else None,
    )
    moved = IntegrationAttempt.model_validate(payload)
    logger.debug(
        f"transition_attempt attempt_id={attempt.id} from_status={attempt.status.value} "
        f"to_status={to.value}"
    )
    return moved


def select_generation(
    ledger: IntegrationGenerationLedger,
    generation: IntegrationGeneration,
) -> IntegrationGenerationLedger:
    """Return *ledger* with *generation* appended as the selected head.

    The previous head is kept as history with its selection released, so
    exactly one generation stays selected.

    Args:
        ledger: The Batch's current generation history.
        generation: The newly integrated generation; must be selected.

    Returns:
        A new, validated ledger whose head is *generation*.

    Raises:
        ValueError: *generation* is not marked selected.
        pydantic.ValidationError: *generation* belongs to another Batch, does
            not parent on and apply over the current head, or does not grow
            the ordinal.
    """
    if not generation.selected:
        raise ValueError(f"generation {generation.id!r} must be selected to become the head")
    history = [item.model_dump(mode="json") for item in ledger.generations]
    if history:
        history[-1]["selected"] = False
    history.append(generation.model_dump(mode="json"))
    selected = IntegrationGenerationLedger.model_validate(
        {"batch_ref": ledger.batch_ref, "generations": history}
    )
    logger.debug(f"select_generation generation_id={generation.id} ordinal={generation.generation}")
    return selected


__all__ = [
    "IllegalIntegrationTransitionError",
    "select_generation",
    "transition_attempt",
]
