"""Deciding what a read-back of the target branch says about a merging Batch.

``MERGING`` is the state in which the daemon asked a host to integrate and
does not yet know what the host did. The registry gives it no way out that
a caller can assert: both edges leaving it sit behind
:data:`~eawf.kernel.state.epoch2.transitions.OBSERVED_GUARD_FACTS`, so
neither a landing nor a refusal exists until somebody reads the target
branch back. This module is where that read-back is turned into a
decision, and it decides exactly three things.

The host refused. A refusal is a fact about the host, and a Batch pushed
back into work without saying why is a Batch nobody can act on, so the
cause is required rather than defaulted: the return path runs
``MERGING -> READY_TO_MERGE -> ACTIVE`` and the second of those edges is
the registry's reason-recorded one. Both edges are looked up in the
registry rather than spelled here, so this module cannot claim a move the
table does not have.

The host landed it. Landing is not the host saying so either: it is the
Batch's own pinned head being reachable from the target branch the
observation actually read. That comparison is what
:data:`~eawf.kernel.state.epoch2.transitions.TransitionGuard.RECONCILIATION_MATCHED`
means, and it is arithmetic over two recorded shas rather than a report
anyone authored.

Nothing is known. A branch that could not be read, and a branch whose
head does not carry the Batch's commit, are the same answer: not yet. The
Batch stays ``MERGING`` and keeps its
:data:`~eawf.kernel.state.epoch2.transitions.AmbiguityLabel.MERGING`
label. There is deliberately no edge for this case -- inventing a status
for "we do not know" is how an unresolved merge becomes a decided one.

Nothing here writes a record. Filing the decision is the daemon's job.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from itertools import pairwise
from typing import Final, Self

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.state.epoch2.base import BranchName, Epoch2Model, ShaStr
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.transitions import (
    AmbiguityLabel,
    DenialCode,
    LifecycleEntity,
    LifecycleStatus,
    ObservedFact,
    TransitionVerb,
    ambiguity_label,
    row_for,
)
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.state.epoch2.values import TransitionReason
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The most commits one observation may carry back. A read-back names the
#: recent history of the target branch, not its whole history: past this
#: bound the observation is a pasted log rather than the answer to "is
#: this Batch's commit on the branch".
MAX_OBSERVED_COMMITS: Final = 1000

#: The prefix every reconciliation line is filed under.
RECONCILIATION_KEY_PREFIX: Final = "BMR-"


class _FrozenModel(Epoch2Model):
    """Strict and immutable: an observation edited afterwards is not an observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MergeOutcome(StrEnum):
    """What a read-back of the target branch established.

    ``UNKNOWN`` is an outcome and not a missing value. A branch nobody
    could read and a branch that does not carry the commit are both
    "not yet", and folding either into a landing or a refusal is exactly
    the fabrication ``MERGING`` exists to prevent.
    """

    LANDED = "landed"
    REFUSED = "refused"
    UNKNOWN = "unknown"


#: Which observed fact each settled outcome supplies to the registry. An
#: unknown outcome supplies none, which is why its edges stay shut.
OUTCOME_FACTS: Final[dict[MergeOutcome, tuple[ObservedFact, ...]]] = {
    MergeOutcome.LANDED: (ObservedFact.HOST_MERGE_OBSERVED,),
    MergeOutcome.REFUSED: (ObservedFact.HOST_MERGE_REFUSED,),
    MergeOutcome.UNKNOWN: (),
}


class ReconciliationRefusal(StrEnum):
    """The stable codes a merge reconciliation is refused with.

    ``EDGE_UNREGISTERED`` is bound to the transition registry's own
    denial code rather than re-spelled: a move the table does not declare
    is the fact the registry already names, and a caller routing on that
    code must keep routing when the refusal arrives from a read-back
    instead of from a move.

    A refusal with no named cause has no code here because it is not a
    reachable request: the outcome is only produced from an observation
    that carries one, and :class:`MergeReconciliation` refuses a refusing
    row without one.
    """

    BATCH_NOT_MERGING = "reconcile_batch_not_merging"
    OBSERVATION_MISBOUND = "reconcile_observation_misbound"
    EDGE_UNREGISTERED = DenialCode.ILLEGAL_TRANSITION


class ReconciliationRefusedError(ValueError):
    """One merge reconciliation refused, with the code that says which rule.

    Attributes:
        code: The stable refusal code a caller routes on.
    """

    def __init__(self, code: ReconciliationRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads.

        Args:
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class HostMergeObservation(_FrozenModel):
    """What one read-back of the target branch found, and nothing more.

    The two halves are independent facts. ``refusal`` is the host having
    said no, in its own words, with a code a consumer can branch on;
    ``target_head_sha`` and ``contained_shas`` are what the branch
    actually holds. An observation that carries both a refusal and a
    branch carrying the Batch's work is self-contradictory, so the pair
    is checked against the Batch at reconciliation rather than trusted.
    """

    batch_ref: BatchUrn
    target_branch: BranchName
    observed_at: UtcDatetime
    target_head_sha: ShaStr | None = None
    contained_shas: tuple[ShaStr, ...] = Field(default=(), max_length=MAX_OBSERVED_COMMITS)
    refusal: TransitionReason | None = None

    @property
    def branch_read(self) -> bool:
        """Return whether the observation managed to read the target branch."""
        return self.target_head_sha is not None

    def carries(self, head_sha: str) -> bool:
        """Return whether the observed branch holds *head_sha*.

        Args:
            head_sha: The commit the Batch pinned as its exact head.

        Returns:
            ``True`` when the read-back found that commit on the branch.
        """
        return head_sha in self.contained_shas

    @model_validator(mode="after")
    def _observation_is_coherent(self) -> Self:
        """Require read commits to belong to a read branch, without repeats.

        Raises:
            ValueError: Commits are reported for a branch that could not
                be read, the observed head is not among them, or a commit
                is listed twice.
        """
        if not self.branch_read and self.contained_shas:
            raise ValueError(
                f"branch {self.target_branch} could not be read, so it carries no commits to report"
            )
        if len(set(self.contained_shas)) != len(self.contained_shas):
            raise ValueError(f"branch {self.target_branch} reports the same commit twice")
        head = self.target_head_sha
        if head is not None and head not in self.contained_shas:
            raise ValueError(
                f"the observed head of {self.target_branch} is not among the commits read back "
                "from it"
            )
        return self


class MergeReconciliation(_FrozenModel):
    """What one read-back decided, and the registered path it takes.

    ``path`` is the verbs of the registry edges the decision walks, in
    order. An unknown outcome walks none: it is the record of a question
    still open, so it names the commit it is still waiting to see rather
    than a move.
    """

    batch_ref: BatchUrn
    outcome: MergeOutcome
    from_status: BatchStatus
    to_status: BatchStatus
    path: tuple[TransitionVerb, ...] = ()
    observed_facts: tuple[ObservedFact, ...] = ()
    cause: TransitionReason | None = None
    awaited_head_sha: ShaStr | None = None
    matched_head_sha: ShaStr | None = None
    target_branch: BranchName
    observed_at: UtcDatetime
    reason: str

    @property
    def resolved(self) -> bool:
        """Return whether the read-back settled what the host did."""
        return self.outcome is not MergeOutcome.UNKNOWN

    @property
    def label(self) -> AmbiguityLabel | None:
        """Return how a surface labels the status this decision lands on."""
        return ambiguity_label(LifecycleEntity.DELIVERY_BATCH, self.to_status)

    @model_validator(mode="after")
    def _outcome_carries_what_it_claims(self) -> Self:
        """Require each outcome's own evidence, and nobody else's.

        Raises:
            ValueError: A refusal names no cause, a landing names no
                matched commit, an unresolved decision moves the Batch or
                names a match, or the observed facts are not the ones the
                outcome supplies.
        """
        if self.observed_facts != OUTCOME_FACTS[self.outcome]:
            raise ValueError(
                f"outcome {self.outcome.value} supplies "
                f"{[item.value for item in OUTCOME_FACTS[self.outcome]]}"
            )
        if self.outcome is MergeOutcome.REFUSED and self.cause is None:
            raise ValueError("a refused merge returns the Batch to work with a named cause")
        if self.outcome is MergeOutcome.LANDED and self.matched_head_sha is None:
            raise ValueError("a landed merge names the commit the target branch was found to hold")
        if self.outcome is MergeOutcome.UNKNOWN:
            if self.to_status is not BatchStatus.MERGING or self.path:
                raise ValueError(
                    "an unknown outcome takes no edge: the Batch stays in "
                    f"{BatchStatus.MERGING.value}"
                )
            if self.matched_head_sha is not None or self.awaited_head_sha is None:
                raise ValueError("an unknown outcome awaits a commit rather than matching one")
        return self


def _verb_of(frm: LifecycleStatus, to: LifecycleStatus) -> TransitionVerb:
    """Return the registered verb of one Batch edge.

    Raises:
        ReconciliationRefusedError: The registry declares no such edge, so
            the decision would be claiming a move that does not exist.
    """
    row = row_for(LifecycleEntity.DELIVERY_BATCH, frm, to)
    if row is None:
        raise ReconciliationRefusedError(
            ReconciliationRefusal.EDGE_UNREGISTERED,
            f"the registry declares no batch edge {frm!s} -> {to!s}",
        )
    return row.verb


def _path_to(*statuses: BatchStatus) -> tuple[TransitionVerb, ...]:
    """Return the verbs of the consecutive edges walking *statuses*.

    Raises:
        ReconciliationRefusedError: One of the edges is unregistered.
    """
    return tuple(_verb_of(frm, to) for frm, to in pairwise(statuses))


def _require_merging(batch: DeliveryBatch, observation: HostMergeObservation) -> str:
    """Return the exact head *batch* is merging, refusing anything else.

    The pinned head needs no check of its own: a Batch only reaches
    ``MERGING`` through :class:`~eawf.kernel.state.epoch2.batch.DeliveryBatch`,
    which requires the binding from the moment the Batch claims to be
    mergeable, so a merging Batch without one is not a record that exists.

    Raises:
        ReconciliationRefusedError: The Batch is not merging, or the
            observation is of another Batch or another branch.
    """
    if observation.batch_ref != batch.urn:
        raise ReconciliationRefusedError(
            ReconciliationRefusal.OBSERVATION_MISBOUND,
            f"the observation is of {observation.batch_ref.entity_key}, not of "
            f"{batch.urn.entity_key}",
        )
    if batch.status is not BatchStatus.MERGING:
        raise ReconciliationRefusedError(
            ReconciliationRefusal.BATCH_NOT_MERGING,
            f"batch {batch.key} is {batch.status.value}, so there is no merge in flight to "
            "reconcile",
        )
    if observation.target_branch != batch.target_branch:
        raise ReconciliationRefusedError(
            ReconciliationRefusal.OBSERVATION_MISBOUND,
            f"batch {batch.key} merges into {batch.target_branch}, but the observation read "
            f"{observation.target_branch}",
        )
    binding = batch.current_head_binding
    assert binding is not None, "a merging Batch always carries its exact head binding"
    return binding.head_sha


def _refused(
    batch: DeliveryBatch, observation: HostMergeObservation, *, cause: TransitionReason
) -> MergeReconciliation:
    """Return the decision that returns a refused Batch to work.

    The cause is a parameter rather than something read back off the
    observation, so a refusal without one is not a call this module can
    make. :class:`MergeReconciliation` refuses the same thing from the
    other side, which is what makes "returns to work with a named cause"
    a shape rather than a rule.

    Raises:
        ReconciliationRefusedError: One of the two edges back to work is
            unregistered.
    """
    return MergeReconciliation(
        batch_ref=batch.urn,
        outcome=MergeOutcome.REFUSED,
        from_status=BatchStatus.MERGING,
        to_status=BatchStatus.ACTIVE,
        path=_path_to(BatchStatus.MERGING, BatchStatus.READY_TO_MERGE, BatchStatus.ACTIVE),
        observed_facts=OUTCOME_FACTS[MergeOutcome.REFUSED],
        cause=cause,
        target_branch=observation.target_branch,
        observed_at=observation.observed_at,
        reason=(
            f"the host refused batch {batch.key} on {observation.target_branch} because "
            f"{cause.code}, so it returns to {BatchStatus.ACTIVE.value}"
        ),
    )


def _landed(
    batch: DeliveryBatch, observation: HostMergeObservation, *, head_sha: str
) -> MergeReconciliation:
    """Return the decision that completes a Batch the branch was found to carry.

    Raises:
        ReconciliationRefusedError: One of the two edges to completion is
            unregistered.
    """
    return MergeReconciliation(
        batch_ref=batch.urn,
        outcome=MergeOutcome.LANDED,
        from_status=BatchStatus.MERGING,
        to_status=BatchStatus.COMPLETED,
        path=_path_to(
            BatchStatus.MERGING,
            BatchStatus.MERGED_PENDING_RECONCILIATION,
            BatchStatus.COMPLETED,
        ),
        observed_facts=OUTCOME_FACTS[MergeOutcome.LANDED],
        matched_head_sha=head_sha,
        target_branch=observation.target_branch,
        observed_at=observation.observed_at,
        reason=(
            f"branch {observation.target_branch} was read at "
            f"{observation.target_head_sha} and carries the exact head batch {batch.key} "
            "pinned, so the merge is reconciled"
        ),
    )


def _unknown(
    batch: DeliveryBatch, observation: HostMergeObservation, *, head_sha: str
) -> MergeReconciliation:
    """Return the decision that leaves an unresolved Batch exactly where it is."""
    read = (
        f"branch {observation.target_branch} was read at {observation.target_head_sha} and does "
        "not carry"
        if observation.branch_read
        else f"branch {observation.target_branch} could not be read, so nothing confirms"
    )
    return MergeReconciliation(
        batch_ref=batch.urn,
        outcome=MergeOutcome.UNKNOWN,
        from_status=BatchStatus.MERGING,
        to_status=BatchStatus.MERGING,
        observed_facts=OUTCOME_FACTS[MergeOutcome.UNKNOWN],
        awaited_head_sha=head_sha,
        target_branch=observation.target_branch,
        observed_at=observation.observed_at,
        reason=(
            f"{read} the exact head batch {batch.key} pinned, so it stays in "
            f"{BatchStatus.MERGING.value} until an observation does"
        ),
    )


def reconcile_merge(batch: DeliveryBatch, observation: HostMergeObservation) -> MergeReconciliation:
    """Decide what one read-back of the target branch says about a merging Batch.

    The decision is a pure function of the Batch's pinned head and what
    the observation read, so the same observation presented twice decides
    the same thing and files under the same derived key.

    Args:
        batch: The Batch whose merge is in flight.
        observation: What reading the target branch back found.

    Returns:
        The outcome, the registered path it takes, and the sentence an
        operator reads.

    Raises:
        ReconciliationRefusedError: The Batch is not merging, the
            observation is of another Batch or branch, it claims both a
            refusal and a branch carrying the work, or an edge the
            decision would take is not in the registry.
    """
    head_sha = _require_merging(batch, observation)
    if observation.carries(head_sha):
        if observation.refusal is not None:
            raise ReconciliationRefusedError(
                ReconciliationRefusal.OBSERVATION_MISBOUND,
                f"the observation says the host refused batch {batch.key} and that "
                f"{observation.target_branch} carries its head, which cannot both be true",
            )
        decision = _landed(batch, observation, head_sha=head_sha)
    elif observation.refusal is not None:
        decision = _refused(batch, observation, cause=observation.refusal)
    else:
        decision = _unknown(batch, observation, head_sha=head_sha)
    logger.info(
        f"reconcile_merge batch={batch.key} outcome={decision.outcome.value} "
        f"to={decision.to_status.value} branch_read={observation.branch_read}"
    )
    return decision


def reconciliation_record_key(decision: MergeReconciliation) -> str:
    """Return the ledger key one reconciliation is filed under.

    The key is derived from the Batch, the head it pinned and the branch
    state that was read, so presenting the same observation again writes
    the same key rather than a second outcome beside the first. A branch
    that has moved is a different read-back and gets its own line.

    Args:
        decision: The reconciliation to file.

    Returns:
        A ``BMR-``-prefixed key, unique per Batch and observed branch
        state.

    Raises:
        TypeError: The decision holds a value JSON cannot encode.
    """
    subject = [
        str(decision.batch_ref),
        decision.target_branch,
        decision.matched_head_sha or decision.awaited_head_sha or "",
        decision.outcome.value,
    ]
    body = canonical_digest(subject).removeprefix("sha256:")
    return f"{RECONCILIATION_KEY_PREFIX}{body[:12]}-{decision.batch_ref.entity_key}"


__all__ = [
    "MAX_OBSERVED_COMMITS",
    "OUTCOME_FACTS",
    "RECONCILIATION_KEY_PREFIX",
    "HostMergeObservation",
    "MergeOutcome",
    "MergeReconciliation",
    "ReconciliationRefusal",
    "ReconciliationRefusedError",
    "reconcile_merge",
    "reconciliation_record_key",
]
