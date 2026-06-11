"""``eawf outcome define`` and ``eawf outcome set`` mutators.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.surfaces.cli._mutation.state_transaction` to serialise
load + mutate + write under ``portalock(state.json)``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum

from eawf.kernel.state.enums import OutcomeDirection, OutcomeStatus
from eawf.kernel.state.models import Outcome, State
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence import _io
from eawf.workflow.evidence.guards import require_complete_audit

logger = logging.getLogger(__name__)


class OutcomeVerdict(StrEnum):
    """Three-way comparator verdict for a measured outcome.

    Distinct from the persisted :class:`OutcomeStatus`: the comparator's
    ``REGRESSED`` verdict captures a sample that both misses the threshold and
    is strictly worse than a previously-achieved ``best_value`` -- a regression
    the persisted ``met / missed`` status cannot express on its own.
    """

    MET = "met"
    UNMET = "unmet"
    REGRESSED = "regressed"


# Comparator verdict -> persisted OutcomeStatus. A regression is still a miss
# of the threshold, so it persists as MISSED; the REGRESSED nuance lives on the
# returned verdict, surfaced into the event payload by ``set_outcome``.
_VERDICT_STATUS: dict[OutcomeVerdict, OutcomeStatus] = {
    OutcomeVerdict.MET: OutcomeStatus.MET,
    OutcomeVerdict.UNMET: OutcomeStatus.MISSED,
    OutcomeVerdict.REGRESSED: OutcomeStatus.MISSED,
}


def compute_outcome_status(
    *,
    threshold: float,
    sample: float,
    direction: OutcomeDirection,
    best_value: float | None = None,
) -> OutcomeVerdict:
    """Derive the three-way comparator verdict for a measured outcome.

    The favorable :class:`OutcomeDirection` decides which way the threshold and
    the prior best are compared: ``MAX`` is higher-is-better, ``MIN`` is
    lower-is-better. A sample that satisfies the threshold in the favorable
    direction is :attr:`OutcomeVerdict.MET`. A sample that misses the threshold
    is :attr:`OutcomeVerdict.REGRESSED` when a prior ``best_value`` exists that
    the sample is strictly worse than, and :attr:`OutcomeVerdict.UNMET`
    otherwise (no prior best, or the sample held its prior best).

    Args:
        threshold: The target value the metric is compared against.
        sample: The latest observed value of the metric.
        direction: The favorable direction (``MAX`` higher-is-better, ``MIN``
            lower-is-better).
        best_value: The best value seen so far, or ``None`` when this is the
            first measurement.

    Returns:
        The :class:`OutcomeVerdict` for the sample.

    Raises:
        ValueError: When ``direction`` is not ``MAX`` or ``MIN`` (the
            ``EQUAL`` / ``RANGE`` directions are not derivable by this
            higher/lower-is-better comparator).
    """
    if direction is OutcomeDirection.MAX:
        met = sample >= threshold
        worse_than_best = best_value is not None and sample < best_value
    elif direction is OutcomeDirection.MIN:
        met = sample <= threshold
        worse_than_best = best_value is not None and sample > best_value
    else:
        raise ValueError(f"non-comparable outcome direction: {direction.value!r}")

    if met:
        return OutcomeVerdict.MET
    if worse_than_best:
        return OutcomeVerdict.REGRESSED
    return OutcomeVerdict.UNMET


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


def _better(direction: OutcomeDirection, sample: float, prior_best: float | None) -> float:
    """Return the better of *sample* and *prior_best* under *direction*.

    Higher is better under ``MAX``, lower under ``MIN``. ``prior_best`` of
    ``None`` makes *sample* the best by default.
    """
    if prior_best is None:
        return sample
    if direction is OutcomeDirection.MAX:
        return max(sample, prior_best)
    return min(sample, prior_best)


def set_outcome(
    state: State,
    *,
    outcome_id: str,
    sample: float,
    audit_id: str,
    evidence_refs: list[str],
) -> Envelope:
    """Record an outcome measurement in place, deriving the status.

    The status is *derived* by :func:`compute_outcome_status` from the
    outcome's threshold, the observed *sample*, and the outcome's favorable
    direction -- it is never hand-set, so a caller cannot claim ``met`` on a
    sample that misses. The running :attr:`Outcome.best_value` is advanced when
    the sample improves on it, which lets the comparator flag a regression off
    a previously-achieved best.

    Calls :func:`require_complete_audit` *before* mutating so the verdict-
    bearing rule fails fast with ``VALIDATION_FAILED`` even when the current
    outcome would otherwise be untouched. The measured outcome's status claim
    must cite at least one *evidence_ref*; the empty case is rejected before the
    mutation lands.

    Args:
        state: The candidate state holding the outcome and its audit.
        outcome_id: Id of the outcome to measure.
        sample: The observed value of the outcome metric.
        audit_id: A complete audit that ratifies the measurement.
        evidence_refs: Repo-relative paths / Eawf URNs / external URLs that
            resolve the status claim. Must be non-empty.

    Returns:
        The ``outcome.set`` event envelope.

    Raises:
        UserError: When *outcome_id* is unknown, or *evidence_refs* is empty.
        ValidationError: When *audit_id* is not a complete audit.
    """
    outcomes: dict[str, Outcome] = dict(state.outcomes or {})
    if outcome_id not in outcomes:
        raise UserError(f"outcome {outcome_id!r} not found", kind="NotFound")
    if not evidence_refs:
        raise UserError(
            f"outcome {outcome_id!r} measurement cites no evidence ref",
            kind="InvalidInput",
        )

    require_complete_audit(state, audit_id)

    now = datetime.now(UTC)
    prior = outcomes[outcome_id]
    verdict = compute_outcome_status(
        threshold=prior.threshold,
        sample=sample,
        direction=prior.direction,
        best_value=prior.best_value,
    )
    status = _VERDICT_STATUS[verdict]
    best_value = _better(prior.direction, sample, prior.best_value)
    updated = prior.model_copy(
        update={
            "value": sample,
            "sample": sample,
            "best_value": best_value,
            "status": status,
            "audit_id": audit_id,
            "evidence_refs": list(evidence_refs),
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
            "sample": sample,
            "verdict": verdict.value,
            "status": status.value,
            "best_value": best_value,
            "audit_id": audit_id,
            "evidence_refs": list(evidence_refs),
        },
        summary=(
            f"outcome {outcome_id} set sample={sample} "
            f"verdict={verdict.value} status={status.value}"
        ),
    )
