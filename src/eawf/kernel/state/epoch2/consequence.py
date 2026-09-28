"""What a native lifecycle verb will do to one record, stated before anyone asks for it.

A lifecycle verb is not a button that acts; it is a move whose consequence is shown and
then accepted. This module derives that consequence from the same transition table the
daemon's transaction evaluates, so the console card and the command line state one
answer: the edge the verb takes from the record's current status, the fields the target
status makes facts, the guards the edge carries, and what the verb deliberately leaves
alone.

A guard is stated in one of two ways. A guard the caller's own request decides -- an
observation only the outside world supplies, a reason or evidence the path does not
collect, a field the target status needs -- refuses the preview with the guard's stable
code and remediation, because sending it could only be refused. A guard the daemon reads
off its locked document is stated as a condition the effect holds under and left to the
daemon, because the preview does not hold that document and guessing it would let the
card and the commit disagree.

The verb table spells the daemon's method names rather than importing them: importing the
daemon's method module registers its handlers in the importing process. A contract test
holds the two tables equal, so a verb renamed or re-edged on one side reds rather than
drifts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.track import TrackStatus
from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    GUARD_DENIALS,
    OBSERVED_GUARD_FACTS,
    DenialCode,
    LifecycleEntity,
    LifecycleStatus,
    TransitionGuard,
    TransitionRow,
    row_for,
)

#: The fields a confirming surface stamps itself, at the moment of confirmation, rather
#: than asking the operator for. Every other field a target status makes a fact is left
#: to a path that collects it.
CLOCK_FIELDS: Final = frozenset({"started_at", "ended_at"})

#: The guards the daemon computes from its locked document before it commits. Spelled
#: here for the same reason the verb table is; the contract test holds it equal to the
#: daemon's own guard table. A guard in neither this set nor the observed set is not
#: evaluated by the daemon at all, so a preview says nothing about it.
DAEMON_GUARDS: Final = frozenset(
    {
        TransitionGuard.NO_OPEN_MILESTONES,
        TransitionGuard.TRACK_ACTIVE,
        TransitionGuard.REQUIRED_BATCHES_COMPLETED,
        TransitionGuard.TASKS_READY_TO_INTEGRATE,
        TransitionGuard.TARGET_BRANCH_PINNED,
        TransitionGuard.HEAD_BINDING_PINNED,
        TransitionGuard.PROMOTION_CONTRACT_COMPLETE,
        TransitionGuard.RUN_BOUND,
        TransitionGuard.REASON_RECORDED,
        TransitionGuard.CRITERIA_EVIDENCE_BOUND,
        TransitionGuard.HOST_MERGE_OBSERVED,
        TransitionGuard.RECONCILIATION_MATCHED,
    }
)

#: The guards met only by something the request sends, which a preview path sends none of.
_REQUEST_GUARDS: Final = frozenset(
    {
        TransitionGuard.REASON_RECORDED,
        TransitionGuard.CRITERIA_EVIDENCE_BOUND,
        TransitionGuard.PROMOTION_CONTRACT_COMPLETE,
        TransitionGuard.RUN_BOUND,
        TransitionGuard.TARGET_BRANCH_PINNED,
    }
)

#: What each guard requires, as the condition an effect holds under.
GUARD_CONDITIONS: Final[Mapping[TransitionGuard, str]] = MappingProxyType(
    {
        TransitionGuard.ACCEPTANCE_JOURNEY_PASSED: "every acceptance step has a passing result",
        TransitionGuard.APPROVAL_FRESH: "the approval is against the exact manifest digest",
        TransitionGuard.CLEARING_FACT_OBSERVED: "the fact the suspension names is observed",
        TransitionGuard.CRITERIA_EVIDENCE_BOUND: "evidence is bound to every criterion",
        TransitionGuard.GATES_GREEN: "every required readiness signal is green",
        TransitionGuard.HEAD_BINDING_PINNED: "the exact head the batch was checked at is pinned",
        TransitionGuard.HOST_MERGE_OBSERVED: "the host's merge is read back",
        TransitionGuard.HOST_MERGE_REFUSED: "the host's refusal is read back",
        TransitionGuard.IDEMPOTENT_RETRY: "the retry carries an idempotency proof",
        TransitionGuard.INTEGRATED_BINDING_PINNED: "the integrated revision is pinned",
        TransitionGuard.LEASE_HELD: "the task's lease is held",
        TransitionGuard.MANIFEST_COMPLETE: "the release manifest is complete",
        TransitionGuard.NO_EXTERNAL_EFFECT: "no externally visible effect has landed",
        TransitionGuard.NO_OPEN_MILESTONES: "no milestone under the track is still open",
        TransitionGuard.OBSERVED_PRERELEASE: "the prerelease is read back from every target",
        TransitionGuard.OBSERVED_STABLE: "the stable release is read back from every target",
        TransitionGuard.PROMOTION_CONTRACT_COMPLETE: "a batch, criteria and a due scope are set",
        TransitionGuard.PUBLICATION_ADOPTED: "the publication is adopted",
        TransitionGuard.REASON_RECORDED: "a transition reason is recorded",
        TransitionGuard.RECONCILIATION_MATCHED: "the merge read-back matches the pinned head",
        TransitionGuard.RECOVERY_EXHAUSTED: "the recovery budget is spent",
        TransitionGuard.REQUIRED_BATCHES_COMPLETED: "every required batch is closed",
        TransitionGuard.RUN_BOUND: "the run that will do the work exists",
        TransitionGuard.RUN_REPORT_BOUND: "the run's role report is bound",
        TransitionGuard.SUSPENSION_REASON_NAMED: "the suspension reason is named",
        TransitionGuard.TARGET_BRANCH_PINNED: "the branch the batch integrates into is pinned",
        TransitionGuard.TARGET_RESULTS_COMPLETE: "every publication leg carries a result",
        TransitionGuard.TASKS_READY_TO_INTEGRATE: "every task in the batch is settled",
        TransitionGuard.TRACK_ACTIVE: "the milestone's track is active",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalMutation:
    """One native lifecycle verb, and what it deliberately leaves alone.

    Attributes:
        method: The daemon's dotted verb name.
        entity: The entity whose machine the verb moves.
        from_statuses: The statuses the verb moves a record out of.
        to_status: The status it lands the record on.
        action: The verb as an operator reads it.
        not_effects: What the verb does not do, each stated outright.
        authority: The authority class the write is taken under.
        approval_required: Whether the request must cite a sealed approval.
        binds_integration: Whether the daemon derives an integrated binding first.
    """

    method: str
    entity: LifecycleEntity
    from_statuses: tuple[LifecycleStatus, ...]
    to_status: LifecycleStatus
    action: str
    not_effects: tuple[str, ...]
    authority: str = "control"
    approval_required: bool = False
    binds_integration: bool = False

    def row_from(self, status: str) -> TransitionRow | None:
        """Return the edge this verb takes out of ``status``, or ``None`` when it takes none."""
        if status not in {str(frm) for frm in self.from_statuses}:
            return None
        frm = next(frm for frm in self.from_statuses if str(frm) == status)
        return row_for(self.entity, frm, self.to_status)


_E = LifecycleEntity

#: Every native lifecycle verb the daemon registers, in its registration order.
CANONICAL_MUTATIONS: Final[tuple[CanonicalMutation, ...]] = (
    CanonicalMutation(
        "domain.track.retire",
        _E.TRACK,
        (TrackStatus.ACTIVE,),
        TrackStatus.RETIRED,
        "retire",
        ("no milestone, batch or task under it moves", "its history stays addressable"),
        authority="admin",
    ),
    CanonicalMutation(
        "domain.milestone.activate",
        _E.MILESTONE,
        (MilestoneStatus.PLANNED,),
        MilestoneStatus.ACTIVE,
        "activate",
        ("no batch or task under it is started", "no run is dispatched"),
    ),
    CanonicalMutation(
        "domain.milestone.open_review",
        _E.MILESTONE,
        (MilestoneStatus.ACTIVE,),
        MilestoneStatus.ACCEPTANCE_REVIEW,
        "open review",
        ("it does not accept the milestone", "no batch or task moves"),
    ),
    CanonicalMutation(
        "domain.milestone.accept",
        _E.MILESTONE,
        (MilestoneStatus.ACCEPTANCE_REVIEW,),
        MilestoneStatus.COMPLETED,
        "accept",
        ("no batch or task is reopened or re-run", "no release is approved"),
        authority="accept",
        approval_required=True,
    ),
    CanonicalMutation(
        "domain.milestone.cancel",
        _E.MILESTONE,
        (MilestoneStatus.PLANNED, MilestoneStatus.ACTIVE, MilestoneStatus.ACCEPTANCE_REVIEW),
        MilestoneStatus.CANCELLED,
        "cancel",
        ("no completed batch is undone", "no run is stopped by it"),
    ),
    CanonicalMutation(
        "domain.batch.activate",
        _E.DELIVERY_BATCH,
        (BatchStatus.PLANNED,),
        BatchStatus.ACTIVE,
        "activate",
        ("no task is claimed or dispatched", "nothing is merged"),
    ),
    CanonicalMutation(
        "domain.batch.ready",
        _E.DELIVERY_BATCH,
        (BatchStatus.ACTIVE,),
        BatchStatus.READY_TO_MERGE,
        "declare ready",
        ("nothing is merged", "no check is re-run"),
    ),
    CanonicalMutation(
        "domain.batch.merge",
        _E.DELIVERY_BATCH,
        (BatchStatus.READY_TO_MERGE,),
        BatchStatus.MERGING,
        "authorize merge",
        (
            "it does not observe the merge; the host still has to land it",
            "no task becomes COMPLETED by it",
            "it does not accept the milestone",
        ),
        authority="merge",
    ),
    CanonicalMutation(
        "domain.batch.observe_merge",
        _E.DELIVERY_BATCH,
        (BatchStatus.MERGING,),
        BatchStatus.MERGED_PENDING_RECONCILIATION,
        "record the merge",
        ("the batch is not completed until reconciliation matches", "no task moves"),
        authority="merge",
    ),
    CanonicalMutation(
        "domain.batch.complete",
        _E.DELIVERY_BATCH,
        (BatchStatus.MERGED_PENDING_RECONCILIATION,),
        BatchStatus.COMPLETED,
        "complete",
        ("it does not accept the milestone", "no other batch is touched"),
        authority="merge",
    ),
    CanonicalMutation(
        "domain.task.promote",
        _E.TASK,
        (TaskStatus.DRAFT,),
        TaskStatus.PLANNED,
        "promote",
        ("no run is dispatched", "the batch does not move"),
    ),
    CanonicalMutation(
        "domain.task.claim",
        _E.TASK,
        (TaskStatus.PLANNED,),
        TaskStatus.CLAIMED,
        "claim",
        ("no run is started", "the batch does not move"),
    ),
    CanonicalMutation(
        "domain.task.start",
        _E.TASK,
        (TaskStatus.CLAIMED,),
        TaskStatus.RUNNING,
        "start",
        ("no other task is claimed", "the batch does not move"),
    ),
    CanonicalMutation(
        "domain.task.ready",
        _E.TASK,
        (TaskStatus.RUNNING,),
        TaskStatus.READY_TO_INTEGRATE,
        "declare ready",
        ("nothing is merged", "the batch does not move"),
    ),
    CanonicalMutation(
        "domain.task.complete",
        _E.TASK,
        (TaskStatus.READY_TO_INTEGRATE,),
        TaskStatus.COMPLETED,
        "complete",
        ("the batch is not merged by it", "the milestone is not accepted"),
        binds_integration=True,
    ),
    CanonicalMutation(
        "domain.run.start",
        _E.RUN,
        (RunStatus.QUEUED,),
        RunStatus.RUNNING,
        "start",
        ("the task does not move", "the move is recorded only; no process is launched by it"),
    ),
    CanonicalMutation(
        "domain.run.finish",
        _E.RUN,
        (RunStatus.RUNNING,),
        RunStatus.COMPLETED,
        "finish",
        ("the task is not completed by it", "nothing is merged"),
    ),
    CanonicalMutation(
        "domain.run.fail",
        _E.RUN,
        (RunStatus.RUNNING,),
        RunStatus.FAILED,
        "fail",
        ("the task is not failed by it", "no other run is touched"),
    ),
)

#: Each verb by its daemon method name.
MUTATIONS_BY_METHOD: Final[Mapping[str, CanonicalMutation]] = MappingProxyType(
    {mutation.method: mutation for mutation in CANONICAL_MUTATIONS}
)


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a preview refuses before anything is sent.

    Attributes:
        code: The stable denial code the daemon would answer with.
        reason: What is missing, naming the evidence.
        remediation: What to do about it.
    """

    code: str
    reason: str
    remediation: str


@dataclass(frozen=True, slots=True)
class Consequence:
    """What one verb will change on one record at one revision, and what it will not.

    Attributes:
        mutation: The verb.
        key: The record's public key.
        revision: The revision the preview is bound to.
        status: The status the record was read in; ``None`` when it was not read.
        effects: What the verb will change, each a sentence.
        not_effects: What it will not change.
        if_stale: What happens if the bound revision moves before confirmation.
        clock_fields: The fields the confirming surface stamps at confirmation.
        refusal: Why the preview refuses; ``None`` when it may be confirmed.
    """

    mutation: CanonicalMutation
    key: str
    revision: int
    status: str | None
    effects: tuple[str, ...]
    not_effects: tuple[str, ...]
    if_stale: str
    clock_fields: tuple[str, ...] = ()
    refusal: Refusal | None = None


def if_stale(revision: int) -> str:
    """Return the reload rule, stated before confirmation, for a card bound to ``revision``."""
    return (
        f"if revision {revision} moves before you confirm, this card reloads at the "
        "current revision and the authorization is withdrawn, never re-targeted"
    )


def _refusal(code: DenialCode, reason: str) -> Refusal:
    return Refusal(code=code.value, reason=reason, remediation=DENIAL_REMEDIATION[code])


def _guard_refusal(guard: TransitionGuard, reason: str) -> Refusal:
    return _refusal(GUARD_DENIALS[guard], reason)


def _decided_refusal(
    mutation: CanonicalMutation, key: str, status: str, row: TransitionRow | None
) -> Refusal | None:
    """Return the refusal the request decides for the edge out of ``status``, if any."""
    if row is None:
        froms = ", ".join(str(frm) for frm in mutation.from_statuses)
        return _refusal(
            DenialCode.ILLEGAL_TRANSITION,
            f"{mutation.action} moves a {mutation.entity.value} out of {froms}, but {key} is "
            f"{status}",
        )
    if mutation.approval_required:
        return Refusal(
            code="protected_approval_required",
            reason="needs a sealed approval receipt and the bundle it approved",
            remediation=(
                "Seal the acceptance approval as a PendingAction against the exact bundle "
                "revision, and accept with its receipt reference."
            ),
        )
    if mutation.binds_integration:
        return _guard_refusal(
            TransitionGuard.INTEGRATED_BINDING_PINNED,
            "needs the completion assessment of the batch head, which this path does not run",
        )
    missing = [name for name in row.required_updates if name not in CLOCK_FIELDS]
    if missing:
        return _refusal(
            DenialCode.MISSING_TRANSITION_FIELDS,
            f"needs {', '.join(missing)}, which this path does not collect",
        )
    for guard in row.guards:
        if guard in _REQUEST_GUARDS:
            return _guard_refusal(
                guard,
                f"needs proof that {GUARD_CONDITIONS[guard]}, which this path does not collect",
            )
        if guard in OBSERVED_GUARD_FACTS and guard not in DAEMON_GUARDS:
            return _guard_refusal(
                guard,
                f"needs proof that {GUARD_CONDITIONS[guard]}, which only an observation supplies",
            )
    return None


def _checked_at_commit(rows: tuple[TransitionRow, ...]) -> tuple[str, ...]:
    """Return the conditions the daemon decides at commit, in the edges' guard order."""
    seen: list[TransitionGuard] = []
    for row in rows:
        seen.extend(guard for guard in row.guards if guard not in seen)
    return tuple(
        f"only if {GUARD_CONDITIONS[guard]}, which the daemon checks at commit"
        for guard in seen
        if guard in DAEMON_GUARDS
    )


def consequence(
    mutation: CanonicalMutation, *, key: str, revision: int, status: str | None
) -> Consequence:
    """Return what ``mutation`` will do to the record ``key`` at ``revision``.

    Args:
        mutation: The verb.
        key: The record's public key.
        revision: The revision the caller read the record at.
        status: The status the caller read it in; ``None`` when the caller has not read
            it, which states every edge the verb may take and leaves the choice to the
            daemon.

    Returns:
        The consequence, carrying a refusal when the request itself decides one.

    Raises:
        ValueError: ``revision`` is not a positive revision.
    """
    if revision < 1:
        raise ValueError(f"revision must be positive, got {revision}")
    entity = mutation.entity.value
    if status is None:
        rows = tuple(
            row
            for frm in mutation.from_statuses
            if (row := row_for(mutation.entity, frm, mutation.to_status)) is not None
        )
        froms = " or ".join(str(frm) for frm in mutation.from_statuses)
        refusal = None
    else:
        row = mutation.row_from(status)
        rows = (row,) if row is not None else ()
        froms = status
        refusal = _decided_refusal(mutation, key, status, row)
    clock = tuple(
        dict.fromkeys(name for row in rows for name in row.required_updates if name in CLOCK_FIELDS)
    )
    verb = rows[0].verb.value if rows else mutation.action.replace(" ", "_")
    effects = (
        f"{entity} {key} moves {froms} → {mutation.to_status}",
        f"revision {revision} becomes {revision + 1} · {entity}.{verb} is recorded",
        *(f"{name} is set to the moment you confirm" for name in clock),
        *_checked_at_commit(rows),
    )
    return Consequence(
        mutation=mutation,
        key=key,
        revision=revision,
        status=status,
        effects=effects,
        not_effects=mutation.not_effects,
        if_stale=if_stale(revision),
        clock_fields=clock,
        refusal=refusal,
    )


__all__ = [
    "CANONICAL_MUTATIONS",
    "CLOCK_FIELDS",
    "DAEMON_GUARDS",
    "GUARD_CONDITIONS",
    "MUTATIONS_BY_METHOD",
    "CanonicalMutation",
    "Consequence",
    "Refusal",
    "consequence",
    "if_stale",
]
