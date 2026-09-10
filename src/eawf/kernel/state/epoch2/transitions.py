"""The guarded transition registry of the six lifecycle entities.

Every legal status move of a Track, a Milestone, a DeliveryBatch, a Task,
a Run and a Release is one :class:`TransitionRow` here. An edge that is
not a row does not exist: a caller asking for it is told
``illegal_transition`` rather than being handed a record in a status
nothing downstream knows how to read.

A row carries the guards its move depends on rather than a boolean. The
guard is a name, the name maps to a stable :class:`DenialCode`, and the
code maps to one remediation sentence, so a denied move tells an operator
what to do next instead of only that it failed.

Some guards cannot be computed from the record. Whether a host really
merged a branch, and whether a publication is really visible, are facts
about the outside world; :data:`OBSERVED_GUARD_FACTS` names which guards
those are, and their edges are satisfied only by presenting the
:class:`ObservedFact`. That is what keeps ``MERGING`` and a run that
stopped answering honest: the registry has no edge that turns either of
them into a successful outcome without the observation, and neither
carries a fabricated terminal status to hide the ambiguity behind.
:data:`AMBIGUOUS_STATES` labels them so a surface can list them as
unresolved.

The diagrams under ``tests/fixtures/epoch2/transitions`` are the
human-readable rendering of this table. :func:`render_state_diagram`
produces them and the parity test refuses any difference in either
direction, so an edge cannot be added to the code without appearing in
the diagram a reader reviews.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.track import TrackStatus

#: Any status of any registered entity. The six enums stay separate -- a
#: Task never holds a Batch status -- and this alias is only what the
#: registry rows and the lookup helpers are generic over.
LifecycleStatus = (
    TrackStatus | MilestoneStatus | BatchStatus | TaskStatus | RunStatus | ReleaseStatus
)


class LifecycleEntity(StrEnum):
    """The six entities whose status machines this registry owns.

    The values are the entity tokens a domain event name carries, so
    ``batch`` is the one spelling of a DeliveryBatch that a reader, a
    subscriber and a stored event all share.
    """

    TRACK = "track"
    MILESTONE = "milestone"
    DELIVERY_BATCH = "batch"
    TASK = "task"
    RUN = "run"
    RELEASE = "release"


class TransitionVerb(StrEnum):
    """The closed verb vocabulary a transition may be named by.

    A verb names what happened, in the past tense, from the entity's own
    point of view. It is the third segment of the event name, so adding a
    verb here is what makes a new event name exist at all.
    """

    ACTIVATED = "activated"
    APPROVED = "approved"
    BAKED = "baked"
    BURNED = "burned"
    CANCELLED = "cancelled"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    DEFERRED = "deferred"
    DROPPED = "dropped"
    FAILED = "failed"
    INVALIDATED = "invalidated"
    LEASE_RELEASED = "lease_released"
    MERGE_OBSERVED = "merge_observed"
    MERGE_REFUSED = "merge_refused"
    MERGE_STARTED = "merge_started"
    PINNED = "pinned"
    PREFLIGHT_FAILED = "preflight_failed"
    PROMOTED = "promoted"
    PUBLICATION_REPORTED = "publication_reported"
    PUBLICATION_RETRIED = "publication_retried"
    PUBLICATION_STARTED = "publication_started"
    PUBLICATION_TIMED_OUT = "publication_timed_out"
    READY = "ready"
    REACTIVATED = "reactivated"
    RECOVERY_OPENED = "recovery_opened"
    RELEASED = "released"
    REOPENED = "reopened"
    REPLANNED = "replanned"
    RESUMED = "resumed"
    RETIRED = "retired"
    REVIEW_OPENED = "review_opened"
    REVIEW_REOPENED = "review_reopened"
    STARTED = "started"
    SUSPENDED = "suspended"


class TransitionGuard(StrEnum):
    """The named predicates a guarded edge may attach.

    A guard is a question the caller answers, never a field the registry
    reads: the registry is a pure table, and the caller is the one that
    owns the Milestone index, the lease ledger and the host client that
    can answer it.
    """

    ACCEPTANCE_JOURNEY_PASSED = "acceptance_journey_passed"
    APPROVAL_FRESH = "approval_fresh"
    CLEARING_FACT_OBSERVED = "clearing_fact_observed"
    CRITERIA_EVIDENCE_BOUND = "criteria_evidence_bound"
    GATES_GREEN = "gates_green"
    HEAD_BINDING_PINNED = "head_binding_pinned"
    HOST_MERGE_OBSERVED = "host_merge_observed"
    HOST_MERGE_REFUSED = "host_merge_refused"
    IDEMPOTENT_RETRY = "idempotent_retry"
    INTEGRATED_BINDING_PINNED = "integrated_binding_pinned"
    LEASE_HELD = "lease_held"
    MANIFEST_COMPLETE = "manifest_complete"
    NO_EXTERNAL_EFFECT = "no_external_effect"
    NO_OPEN_MILESTONES = "no_open_milestones"
    OBSERVED_PRERELEASE = "observed_prerelease"
    OBSERVED_STABLE = "observed_stable"
    PROMOTION_CONTRACT_COMPLETE = "promotion_contract_complete"
    REASON_RECORDED = "reason_recorded"
    RECONCILIATION_MATCHED = "reconciliation_matched"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    REQUIRED_BATCHES_COMPLETED = "required_batches_completed"
    RUN_BOUND = "run_bound"
    RUN_REPORT_BOUND = "run_report_bound"
    SUSPENSION_REASON_NAMED = "suspension_reason_named"
    TARGET_BRANCH_PINNED = "target_branch_pinned"
    TARGET_RESULTS_COMPLETE = "target_results_complete"
    TASKS_READY_TO_INTEGRATE = "tasks_ready_to_integrate"
    TRACK_ACTIVE = "track_active"


class DenialCode(StrEnum):
    """The stable code a denied transition returns.

    Four of these are structural -- they describe the request rather than
    a predicate -- and the rest are the codes the named guards raise. A
    consumer groups by this value, so the values are part of the public
    contract and are never re-spelled to read better.
    """

    ILLEGAL_TRANSITION = "illegal_transition"
    TERMINAL_STATE = "terminal_state"
    LEGACY_ORIGIN_IMMUTABLE = "legacy_origin_immutable"
    MISSING_TRANSITION_FIELDS = "missing_transition_fields"

    ACCEPTANCE_JOURNEY_INCOMPLETE = "acceptance_journey_incomplete"
    APPROVAL_STALE = "approval_stale"
    BATCH_HEAD_BINDING_MISSING = "batch_head_binding_missing"
    BATCH_RECONCILIATION_PENDING = "batch_reconciliation_pending"
    BATCH_TARGET_BRANCH_UNSET = "batch_target_branch_unset"
    BATCH_TASKS_OPEN = "batch_tasks_open"
    HOST_MERGE_NOT_REFUSED = "host_merge_not_refused"
    HOST_MERGE_UNOBSERVED = "host_merge_unobserved"
    MILESTONE_BATCHES_OPEN = "milestone_batches_open"
    MILESTONE_TRACK_RETIRED = "milestone_track_retired"
    PUBLICATION_NOT_OBSERVED = "publication_not_observed"
    RECOVERY_BUDGET_AVAILABLE = "recovery_budget_available"
    RELEASE_EFFECT_ALREADY_STARTED = "release_effect_already_started"
    RELEASE_MANIFEST_INCOMPLETE = "release_manifest_incomplete"
    RELEASE_NOT_READY = "release_not_ready"
    RUN_CLEARING_FACT_UNOBSERVED = "run_clearing_fact_unobserved"
    RUN_REPORT_UNBOUND = "run_report_unbound"
    RUN_SUSPENSION_REASON_MISSING = "run_suspension_reason_missing"
    TARGET_RESULTS_INCOMPLETE = "target_results_incomplete"
    TASK_EVIDENCE_UNBOUND = "task_evidence_unbound"
    TASK_INTEGRATION_UNPROVEN = "task_integration_unproven"
    TASK_LEASE_UNHELD = "task_lease_unheld"
    TASK_PROMOTION_INCOMPLETE = "task_promotion_incomplete"
    TASK_RUN_UNBOUND = "task_run_unbound"
    TRACK_HAS_OPEN_MILESTONES = "track_has_open_milestones"
    TRANSITION_REASON_MISSING = "transition_reason_missing"
    UNSAFE_RELEASE_RETRY = "unsafe_release_retry"


class ObservedFact(StrEnum):
    """A fact about the outside world that only an observation supplies.

    None of these is derivable from a stored record. An adapter reads
    them back from the host or the package index and presents them; until
    it does, the edges that need them stay shut.
    """

    HOST_MERGE_OBSERVED = "host_merge_observed"
    HOST_MERGE_REFUSED = "host_merge_refused"
    RUN_CLEARING_FACT_OBSERVED = "run_clearing_fact_observed"
    RUN_REPORT_BOUND = "run_report_bound"
    PUBLICATION_PRERELEASE_OBSERVED = "publication_prerelease_observed"
    PUBLICATION_STABLE_OBSERVED = "publication_stable_observed"


class AmbiguityLabel(StrEnum):
    """How a surface names a state whose real outcome is not known yet.

    A label is not a status. Adding ``LOST`` to :class:`RunStatus` would
    make an unknown outcome look like a decided one and would need an
    exit edge that no observation backs, so the ambiguity is carried
    beside the status instead of inside it.
    """

    MERGING = "merging"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class TransitionRow:
    """One legal status move, with what it is called and what it needs.

    Attributes:
        entity: The entity whose machine this edge belongs to.
        frm: Status the record is in.
        to: Status the move lands on.
        verb: Past-tense name of the move; the event name's last segment.
        guards: Named predicates, evaluated in declaration order, so the
            surfaced denial is the first thing an operator has to fix.
        required_updates: Successor fields the caller must supply because
            the target status makes them facts. A move missing one is
            denied rather than attempted, so a record is never handed to
            a validator that would reject it.
    """

    entity: LifecycleEntity
    frm: LifecycleStatus
    to: LifecycleStatus
    verb: TransitionVerb
    guards: tuple[TransitionGuard, ...] = ()
    required_updates: tuple[str, ...] = ()


#: Which status enum each entity's rows are drawn from. The parity check
#: and the recovery walk enumerate an entity's states through this map,
#: so a state that no row mentions is still a state they see.
ENTITY_STATUS_ENUM: Final[Mapping[LifecycleEntity, type[StrEnum]]] = {
    LifecycleEntity.TRACK: TrackStatus,
    LifecycleEntity.MILESTONE: MilestoneStatus,
    LifecycleEntity.DELIVERY_BATCH: BatchStatus,
    LifecycleEntity.TASK: TaskStatus,
    LifecycleEntity.RUN: RunStatus,
    LifecycleEntity.RELEASE: ReleaseStatus,
}


#: The stable code each guard raises when its predicate is unmet.
GUARD_DENIALS: Final[Mapping[TransitionGuard, DenialCode]] = {
    TransitionGuard.ACCEPTANCE_JOURNEY_PASSED: DenialCode.ACCEPTANCE_JOURNEY_INCOMPLETE,
    TransitionGuard.APPROVAL_FRESH: DenialCode.APPROVAL_STALE,
    TransitionGuard.CLEARING_FACT_OBSERVED: DenialCode.RUN_CLEARING_FACT_UNOBSERVED,
    TransitionGuard.CRITERIA_EVIDENCE_BOUND: DenialCode.TASK_EVIDENCE_UNBOUND,
    TransitionGuard.GATES_GREEN: DenialCode.RELEASE_NOT_READY,
    TransitionGuard.HEAD_BINDING_PINNED: DenialCode.BATCH_HEAD_BINDING_MISSING,
    TransitionGuard.HOST_MERGE_OBSERVED: DenialCode.HOST_MERGE_UNOBSERVED,
    TransitionGuard.HOST_MERGE_REFUSED: DenialCode.HOST_MERGE_NOT_REFUSED,
    TransitionGuard.IDEMPOTENT_RETRY: DenialCode.UNSAFE_RELEASE_RETRY,
    TransitionGuard.INTEGRATED_BINDING_PINNED: DenialCode.TASK_INTEGRATION_UNPROVEN,
    TransitionGuard.LEASE_HELD: DenialCode.TASK_LEASE_UNHELD,
    TransitionGuard.MANIFEST_COMPLETE: DenialCode.RELEASE_MANIFEST_INCOMPLETE,
    TransitionGuard.NO_EXTERNAL_EFFECT: DenialCode.RELEASE_EFFECT_ALREADY_STARTED,
    TransitionGuard.NO_OPEN_MILESTONES: DenialCode.TRACK_HAS_OPEN_MILESTONES,
    TransitionGuard.OBSERVED_PRERELEASE: DenialCode.PUBLICATION_NOT_OBSERVED,
    TransitionGuard.OBSERVED_STABLE: DenialCode.PUBLICATION_NOT_OBSERVED,
    TransitionGuard.PROMOTION_CONTRACT_COMPLETE: DenialCode.TASK_PROMOTION_INCOMPLETE,
    TransitionGuard.REASON_RECORDED: DenialCode.TRANSITION_REASON_MISSING,
    TransitionGuard.RECONCILIATION_MATCHED: DenialCode.BATCH_RECONCILIATION_PENDING,
    TransitionGuard.RECOVERY_EXHAUSTED: DenialCode.RECOVERY_BUDGET_AVAILABLE,
    TransitionGuard.REQUIRED_BATCHES_COMPLETED: DenialCode.MILESTONE_BATCHES_OPEN,
    TransitionGuard.RUN_BOUND: DenialCode.TASK_RUN_UNBOUND,
    TransitionGuard.RUN_REPORT_BOUND: DenialCode.RUN_REPORT_UNBOUND,
    TransitionGuard.SUSPENSION_REASON_NAMED: DenialCode.RUN_SUSPENSION_REASON_MISSING,
    TransitionGuard.TARGET_BRANCH_PINNED: DenialCode.BATCH_TARGET_BRANCH_UNSET,
    TransitionGuard.TARGET_RESULTS_COMPLETE: DenialCode.TARGET_RESULTS_INCOMPLETE,
    TransitionGuard.TASKS_READY_TO_INTEGRATE: DenialCode.BATCH_TASKS_OPEN,
    TransitionGuard.TRACK_ACTIVE: DenialCode.MILESTONE_TRACK_RETIRED,
}


#: One sentence per code saying what to do about it. A denial an operator
#: cannot act on is a dead end, so this map is total over
#: :class:`DenialCode` and the totality is a test.
DENIAL_REMEDIATION: Final[Mapping[DenialCode, str]] = {
    DenialCode.ILLEGAL_TRANSITION: (
        "Ask for a status the registry lists as reachable from the current one."
    ),
    DenialCode.TERMINAL_STATE: (
        "A terminal record is never reopened; create the successor record instead."
    ),
    DenialCode.LEGACY_ORIGIN_IMMUTABLE: (
        "A record projected from the previous epoch is read-only; drive the native record "
        "it maps to."
    ),
    DenialCode.MISSING_TRANSITION_FIELDS: (
        "Supply every field the target status makes a fact, then retry the move."
    ),
    DenialCode.ACCEPTANCE_JOURNEY_INCOMPLETE: (
        "Record a passing result for every acceptance step before completing the Milestone."
    ),
    DenialCode.APPROVAL_STALE: (
        "Re-approve against the exact manifest digest the publication will use."
    ),
    DenialCode.BATCH_HEAD_BINDING_MISSING: (
        "Record the exact head the Batch was checked at before claiming it is mergeable."
    ),
    DenialCode.BATCH_RECONCILIATION_PENDING: (
        "Match the merge authorisation against the host integration record, then complete."
    ),
    DenialCode.BATCH_TARGET_BRANCH_UNSET: (
        "Pin the branch the Batch integrates into before activating it."
    ),
    DenialCode.BATCH_TASKS_OPEN: (
        "Finish or cancel every Task in the Batch before declaring it ready to merge."
    ),
    DenialCode.HOST_MERGE_NOT_REFUSED: (
        "The host has neither landed nor refused the merge; read it back before deciding."
    ),
    DenialCode.HOST_MERGE_UNOBSERVED: (
        "Read the integration back from the host: a reported merge is not an observed one."
    ),
    DenialCode.MILESTONE_BATCHES_OPEN: (
        "Complete or cancel every required DeliveryBatch before opening acceptance review."
    ),
    DenialCode.MILESTONE_TRACK_RETIRED: (
        "Move the Milestone to a live Track, or reopen work under an active one."
    ),
    DenialCode.PUBLICATION_NOT_OBSERVED: (
        "Read every required target back independently; an adapter's own report never bakes."
    ),
    DenialCode.RECOVERY_BUDGET_AVAILABLE: (
        "Retry the publication while budget remains; burn the version only once it is spent."
    ),
    DenialCode.RELEASE_EFFECT_ALREADY_STARTED: (
        "An externally visible effect already landed; correct it with a new version."
    ),
    DenialCode.RELEASE_MANIFEST_INCOMPLETE: (
        "Complete the manifest -- membership, exact source and the full target set -- then pin."
    ),
    DenialCode.RELEASE_NOT_READY: (
        "Make every required readiness signal green before approving the release."
    ),
    DenialCode.RUN_CLEARING_FACT_UNOBSERVED: (
        "Wait for the fact the suspension reason names, then resume the Run."
    ),
    DenialCode.RUN_REPORT_UNBOUND: (
        "Bind the role report the Run produced before recording any outcome for it."
    ),
    DenialCode.RUN_SUSPENSION_REASON_MISSING: (
        "Name the suspension reason, which is also the fact that will clear it."
    ),
    DenialCode.TARGET_RESULTS_INCOMPLETE: (
        "Every configured publication leg must carry a result before verification opens."
    ),
    DenialCode.TASK_EVIDENCE_UNBOUND: (
        "Bind evidence to every success criterion before the Task may integrate."
    ),
    DenialCode.TASK_INTEGRATION_UNPROVEN: (
        "Record the exact revision binding the Task integrated at."
    ),
    DenialCode.TASK_LEASE_UNHELD: "Take the Task's lease before claiming it.",
    DenialCode.TASK_PROMOTION_INCOMPLETE: (
        "A promoted Task needs a Batch, at least one criterion and a due scope."
    ),
    DenialCode.TASK_RUN_UNBOUND: "Open the Run that will do the work, then start the Task.",
    DenialCode.TRACK_HAS_OPEN_MILESTONES: (
        "Complete or cancel every Milestone under the Track before retiring it."
    ),
    DenialCode.TRANSITION_REASON_MISSING: (
        "Supply a transition reason carrying its code, its message and its evidence."
    ),
    DenialCode.UNSAFE_RELEASE_RETRY: (
        "A retry needs an idempotency proof and unchanged source, tag and artifact digests."
    ),
}


#: Which guards only an observation can satisfy, and the fact each needs.
#: An edge behind one of these cannot be pushed through by a caller that
#: merely believes the thing happened.
OBSERVED_GUARD_FACTS: Final[Mapping[TransitionGuard, ObservedFact]] = {
    TransitionGuard.HOST_MERGE_OBSERVED: ObservedFact.HOST_MERGE_OBSERVED,
    TransitionGuard.HOST_MERGE_REFUSED: ObservedFact.HOST_MERGE_REFUSED,
    TransitionGuard.CLEARING_FACT_OBSERVED: ObservedFact.RUN_CLEARING_FACT_OBSERVED,
    TransitionGuard.RUN_REPORT_BOUND: ObservedFact.RUN_REPORT_BOUND,
    TransitionGuard.OBSERVED_PRERELEASE: ObservedFact.PUBLICATION_PRERELEASE_OBSERVED,
    TransitionGuard.OBSERVED_STABLE: ObservedFact.PUBLICATION_STABLE_OBSERVED,
}


_TRACK_ROWS: Final[tuple[TransitionRow, ...]] = (
    TransitionRow(
        entity=LifecycleEntity.TRACK,
        frm=TrackStatus.ACTIVE,
        to=TrackStatus.RETIRED,
        verb=TransitionVerb.RETIRED,
        guards=(TransitionGuard.NO_OPEN_MILESTONES,),
    ),
)


_MILESTONE_ROWS: Final[tuple[TransitionRow, ...]] = (
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.PLANNED,
        to=MilestoneStatus.ACTIVE,
        verb=TransitionVerb.ACTIVATED,
        guards=(TransitionGuard.TRACK_ACTIVE,),
    ),
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.PLANNED,
        to=MilestoneStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.ACTIVE,
        to=MilestoneStatus.ACCEPTANCE_REVIEW,
        verb=TransitionVerb.REVIEW_OPENED,
        guards=(TransitionGuard.REQUIRED_BATCHES_COMPLETED,),
        required_updates=("acceptance_bundle_revision",),
    ),
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.ACTIVE,
        to=MilestoneStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.ACCEPTANCE_REVIEW,
        to=MilestoneStatus.ACTIVE,
        verb=TransitionVerb.REVIEW_REOPENED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.ACCEPTANCE_REVIEW,
        to=MilestoneStatus.COMPLETED,
        verb=TransitionVerb.COMPLETED,
        guards=(TransitionGuard.ACCEPTANCE_JOURNEY_PASSED,),
        required_updates=("accepted_binding",),
    ),
    TransitionRow(
        entity=LifecycleEntity.MILESTONE,
        frm=MilestoneStatus.ACCEPTANCE_REVIEW,
        to=MilestoneStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
)


_BATCH_ROWS: Final[tuple[TransitionRow, ...]] = (
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.PLANNED,
        to=BatchStatus.ACTIVE,
        verb=TransitionVerb.ACTIVATED,
        guards=(TransitionGuard.TARGET_BRANCH_PINNED,),
        required_updates=("target_branch",),
    ),
    # A Batch may lack a target branch only while it is PLANNED, so a
    # cancellation records the branch the work was aimed at rather than
    # leaving the cancelled row unreadable about where it would have gone.
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.PLANNED,
        to=BatchStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("target_branch",),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.ACTIVE,
        to=BatchStatus.READY_TO_MERGE,
        verb=TransitionVerb.READY,
        guards=(
            TransitionGuard.TASKS_READY_TO_INTEGRATE,
            TransitionGuard.HEAD_BINDING_PINNED,
        ),
        required_updates=("current_head_binding",),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.ACTIVE,
        to=BatchStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.ACTIVE,
        to=BatchStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("failure",),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.READY_TO_MERGE,
        to=BatchStatus.MERGING,
        verb=TransitionVerb.MERGE_STARTED,
        guards=(TransitionGuard.HEAD_BINDING_PINNED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.READY_TO_MERGE,
        to=BatchStatus.ACTIVE,
        verb=TransitionVerb.REOPENED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.MERGING,
        to=BatchStatus.MERGED_PENDING_RECONCILIATION,
        verb=TransitionVerb.MERGE_OBSERVED,
        guards=(TransitionGuard.HOST_MERGE_OBSERVED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.MERGING,
        to=BatchStatus.READY_TO_MERGE,
        verb=TransitionVerb.MERGE_REFUSED,
        guards=(TransitionGuard.HOST_MERGE_REFUSED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.MERGING,
        to=BatchStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("failure",),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.MERGED_PENDING_RECONCILIATION,
        to=BatchStatus.COMPLETED,
        verb=TransitionVerb.COMPLETED,
        guards=(TransitionGuard.RECONCILIATION_MATCHED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.DELIVERY_BATCH,
        frm=BatchStatus.MERGED_PENDING_RECONCILIATION,
        to=BatchStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("failure",),
    ),
)


_TASK_ROWS: Final[tuple[TransitionRow, ...]] = (
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.DRAFT,
        to=TaskStatus.DEFERRED,
        verb=TransitionVerb.DEFERRED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.DRAFT,
        to=TaskStatus.DROPPED,
        verb=TransitionVerb.DROPPED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.DRAFT,
        to=TaskStatus.PLANNED,
        verb=TransitionVerb.PROMOTED,
        guards=(TransitionGuard.PROMOTION_CONTRACT_COMPLETE,),
        required_updates=("batch_ref", "criteria", "due_scope"),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.DEFERRED,
        to=TaskStatus.DRAFT,
        verb=TransitionVerb.REACTIVATED,
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.DEFERRED,
        to=TaskStatus.DROPPED,
        verb=TransitionVerb.DROPPED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.PLANNED,
        to=TaskStatus.CLAIMED,
        verb=TransitionVerb.CLAIMED,
        guards=(TransitionGuard.LEASE_HELD,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.PLANNED,
        to=TaskStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.CLAIMED,
        to=TaskStatus.RUNNING,
        verb=TransitionVerb.STARTED,
        guards=(TransitionGuard.RUN_BOUND,),
        required_updates=("active_run_ref",),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.CLAIMED,
        to=TaskStatus.PLANNED,
        verb=TransitionVerb.LEASE_RELEASED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.CLAIMED,
        to=TaskStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.RUNNING,
        to=TaskStatus.READY_TO_INTEGRATE,
        verb=TransitionVerb.READY,
        guards=(
            TransitionGuard.RUN_REPORT_BOUND,
            TransitionGuard.CRITERIA_EVIDENCE_BOUND,
        ),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.RUNNING,
        to=TaskStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(
            TransitionGuard.RUN_REPORT_BOUND,
            TransitionGuard.REASON_RECORDED,
        ),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.RUNNING,
        to=TaskStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.READY_TO_INTEGRATE,
        to=TaskStatus.COMPLETED,
        verb=TransitionVerb.COMPLETED,
        guards=(TransitionGuard.INTEGRATED_BINDING_PINNED,),
        required_updates=("integrated_binding",),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.READY_TO_INTEGRATE,
        to=TaskStatus.PLANNED,
        verb=TransitionVerb.REPLANNED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
    TransitionRow(
        entity=LifecycleEntity.TASK,
        frm=TaskStatus.READY_TO_INTEGRATE,
        to=TaskStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(TransitionGuard.REASON_RECORDED,),
    ),
)


_RUN_ROWS: Final[tuple[TransitionRow, ...]] = (
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.QUEUED,
        to=RunStatus.RUNNING,
        verb=TransitionVerb.STARTED,
        required_updates=("started_at",),
    ),
    # Only a QUEUED Run may lack a start stamp, so a Run cancelled out of
    # the queue records when it was taken up as well as when it stopped.
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.QUEUED,
        to=RunStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("started_at", "ended_at"),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.RUNNING,
        to=RunStatus.SUSPENDED,
        verb=TransitionVerb.SUSPENDED,
        guards=(TransitionGuard.SUSPENSION_REASON_NAMED,),
        required_updates=("suspension_reason",),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.RUNNING,
        to=RunStatus.COMPLETED,
        verb=TransitionVerb.COMPLETED,
        guards=(TransitionGuard.RUN_REPORT_BOUND,),
        required_updates=("ended_at",),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.RUNNING,
        to=RunStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(
            TransitionGuard.RUN_REPORT_BOUND,
            TransitionGuard.REASON_RECORDED,
        ),
        required_updates=("ended_at", "failure"),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.RUNNING,
        to=RunStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("ended_at",),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.SUSPENDED,
        to=RunStatus.RUNNING,
        verb=TransitionVerb.RESUMED,
        guards=(TransitionGuard.CLEARING_FACT_OBSERVED,),
        required_updates=("suspension_reason",),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.SUSPENDED,
        to=RunStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("suspension_reason", "ended_at"),
    ),
    TransitionRow(
        entity=LifecycleEntity.RUN,
        frm=RunStatus.SUSPENDED,
        to=RunStatus.FAILED,
        verb=TransitionVerb.FAILED,
        guards=(TransitionGuard.REASON_RECORDED,),
        required_updates=("suspension_reason", "ended_at", "failure"),
    ),
)


_RELEASE_ROWS: Final[tuple[TransitionRow, ...]] = (
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.DRAFT,
        to=ReleaseStatus.CANDIDATE,
        verb=TransitionVerb.PINNED,
        guards=(TransitionGuard.MANIFEST_COMPLETE,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.CANDIDATE,
        to=ReleaseStatus.PREFLIGHT_FAILED,
        verb=TransitionVerb.PREFLIGHT_FAILED,
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.CANDIDATE,
        to=ReleaseStatus.DRAFT,
        verb=TransitionVerb.INVALIDATED,
        guards=(TransitionGuard.NO_EXTERNAL_EFFECT,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.CANDIDATE,
        to=ReleaseStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.NO_EXTERNAL_EFFECT,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.CANDIDATE,
        to=ReleaseStatus.APPROVED,
        verb=TransitionVerb.APPROVED,
        guards=(TransitionGuard.GATES_GREEN,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PREFLIGHT_FAILED,
        to=ReleaseStatus.DRAFT,
        verb=TransitionVerb.INVALIDATED,
        guards=(TransitionGuard.NO_EXTERNAL_EFFECT,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PREFLIGHT_FAILED,
        to=ReleaseStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.NO_EXTERNAL_EFFECT,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.APPROVED,
        to=ReleaseStatus.DRAFT,
        verb=TransitionVerb.INVALIDATED,
        guards=(TransitionGuard.NO_EXTERNAL_EFFECT,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.APPROVED,
        to=ReleaseStatus.CANCELLED,
        verb=TransitionVerb.CANCELLED,
        guards=(TransitionGuard.NO_EXTERNAL_EFFECT,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.APPROVED,
        to=ReleaseStatus.PUBLISHING,
        verb=TransitionVerb.PUBLICATION_STARTED,
        guards=(TransitionGuard.APPROVAL_FRESH,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PUBLISHING,
        to=ReleaseStatus.VERIFYING,
        verb=TransitionVerb.PUBLICATION_REPORTED,
        guards=(TransitionGuard.TARGET_RESULTS_COMPLETE,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PUBLISHING,
        to=ReleaseStatus.PUBLISH_TIMEOUT,
        verb=TransitionVerb.PUBLICATION_TIMED_OUT,
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PUBLISHING,
        to=ReleaseStatus.RECOVERING,
        verb=TransitionVerb.RECOVERY_OPENED,
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PUBLISH_TIMEOUT,
        to=ReleaseStatus.PUBLISHING,
        verb=TransitionVerb.PUBLICATION_RETRIED,
        guards=(TransitionGuard.IDEMPOTENT_RETRY,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.PUBLISH_TIMEOUT,
        to=ReleaseStatus.RECOVERING,
        verb=TransitionVerb.RECOVERY_OPENED,
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.VERIFYING,
        to=ReleaseStatus.BAKED,
        verb=TransitionVerb.BAKED,
        guards=(TransitionGuard.OBSERVED_PRERELEASE,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.VERIFYING,
        to=ReleaseStatus.RELEASED,
        verb=TransitionVerb.RELEASED,
        guards=(TransitionGuard.OBSERVED_STABLE,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.VERIFYING,
        to=ReleaseStatus.RECOVERING,
        verb=TransitionVerb.RECOVERY_OPENED,
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.RECOVERING,
        to=ReleaseStatus.PUBLISHING,
        verb=TransitionVerb.PUBLICATION_RETRIED,
        guards=(TransitionGuard.IDEMPOTENT_RETRY,),
    ),
    TransitionRow(
        entity=LifecycleEntity.RELEASE,
        frm=ReleaseStatus.RECOVERING,
        to=ReleaseStatus.PARTIALLY_RELEASED,
        verb=TransitionVerb.BURNED,
        guards=(TransitionGuard.RECOVERY_EXHAUSTED,),
    ),
)


#: Every registered edge, in entity order. This tuple is the registry;
#: the maps below are indexes over it.
TRANSITION_ROWS: Final[tuple[TransitionRow, ...]] = (
    *_TRACK_ROWS,
    *_MILESTONE_ROWS,
    *_BATCH_ROWS,
    *_TASK_ROWS,
    *_RUN_ROWS,
    *_RELEASE_ROWS,
)


def index_rows(
    rows: tuple[TransitionRow, ...],
) -> Mapping[tuple[LifecycleEntity, str, str], TransitionRow]:
    """Return *rows* keyed by the edge each one declares.

    Args:
        rows: The registry rows to index.

    Returns:
        A mapping from ``(entity, from-status, to-status)`` to its row.

    Raises:
        ValueError: Two rows declare the same edge. One edge with two
            guard sets would make the answer depend on iteration order,
            so the ambiguity is refused where it is written rather than
            resolved arbitrarily where it is read.
    """
    indexed: dict[tuple[LifecycleEntity, str, str], TransitionRow] = {}
    for row in rows:
        key = (row.entity, str(row.frm), str(row.to))
        if key in indexed:
            raise ValueError(
                f"duplicate transition row for {row.entity.value} {row.frm!s} -> {row.to!s}"
            )
        indexed[key] = row
    return indexed


_ROWS_BY_EDGE: Final = index_rows(TRANSITION_ROWS)

_ROWS_BY_SOURCE: Final[Mapping[tuple[LifecycleEntity, str], tuple[TransitionRow, ...]]] = {
    (entity, str(status)): tuple(
        row for row in TRANSITION_ROWS if row.entity is entity and str(row.frm) == str(status)
    )
    for entity, status_enum in ENTITY_STATUS_ENUM.items()
    for status in status_enum
}


#: The states with no outgoing row. A terminal record is finished: it is
#: corrected by a successor record, never by a move back out.
TERMINAL_STATUSES: Final[Mapping[LifecycleEntity, frozenset[str]]] = {
    entity: frozenset(
        str(status) for status in status_enum if not _ROWS_BY_SOURCE[(entity, str(status))]
    )
    for entity, status_enum in ENTITY_STATUS_ENUM.items()
}


#: Where an operator may land a record whose real outcome is unknown.
#: Reaching one of these is not a claim about what happened; it records
#: that the work was abandoned or the version burned, which is the honest
#: answer when nothing can be observed.
RECOVERY_STATES: Final[Mapping[LifecycleEntity, frozenset[str]]] = {
    LifecycleEntity.TRACK: frozenset(),
    LifecycleEntity.MILESTONE: frozenset({str(MilestoneStatus.CANCELLED)}),
    LifecycleEntity.DELIVERY_BATCH: frozenset(
        {str(BatchStatus.CANCELLED), str(BatchStatus.FAILED)}
    ),
    LifecycleEntity.TASK: frozenset({str(TaskStatus.CANCELLED), str(TaskStatus.DROPPED)}),
    LifecycleEntity.RUN: frozenset({str(RunStatus.CANCELLED)}),
    LifecycleEntity.RELEASE: frozenset(
        {str(ReleaseStatus.CANCELLED), str(ReleaseStatus.PARTIALLY_RELEASED)}
    ),
}


#: The terminal states that assert the work succeeded. No edge out of an
#: ambiguous state may reach one of these without an observation, which
#: is the rule that stops a stalled merge or a silent run from being
#: written up as a success.
SUCCESS_TERMINALS: Final[Mapping[LifecycleEntity, frozenset[str]]] = {
    LifecycleEntity.TRACK: frozenset(),
    LifecycleEntity.MILESTONE: frozenset({str(MilestoneStatus.COMPLETED)}),
    LifecycleEntity.DELIVERY_BATCH: frozenset({str(BatchStatus.COMPLETED)}),
    LifecycleEntity.TASK: frozenset({str(TaskStatus.COMPLETED)}),
    LifecycleEntity.RUN: frozenset({str(RunStatus.COMPLETED)}),
    LifecycleEntity.RELEASE: frozenset({str(ReleaseStatus.BAKED), str(ReleaseStatus.RELEASED)}),
}


#: The states whose outcome is genuinely unknown while the record sits in
#: them, and the label a surface lists them under. Both are ordinary
#: nonterminal states: the ambiguity is a fact about the world, so it is
#: reported rather than resolved by inventing a status for it.
AMBIGUOUS_STATES: Final[Mapping[tuple[LifecycleEntity, str], AmbiguityLabel]] = {
    (LifecycleEntity.DELIVERY_BATCH, str(BatchStatus.MERGING)): AmbiguityLabel.MERGING,
    (LifecycleEntity.RUN, str(RunStatus.RUNNING)): AmbiguityLabel.LOST,
}


def statuses_of(entity: LifecycleEntity) -> tuple[LifecycleStatus, ...]:
    """Return every status of *entity*, in declaration order.

    The six status enums are separate types, so the shared table maps to
    ``type[StrEnum]`` and loses that; this is the one place the narrowing
    back to *entity*'s own statuses is asserted.

    Args:
        entity: The entity whose vocabulary is wanted.

    Returns:
        The entity's statuses.
    """
    return cast("tuple[LifecycleStatus, ...]", tuple(ENTITY_STATUS_ENUM[entity]))


def row_for(
    entity: LifecycleEntity, frm: LifecycleStatus, to: LifecycleStatus
) -> TransitionRow | None:
    """Return the row declaring ``frm -> to`` for *entity*, or ``None``.

    Args:
        entity: The entity whose machine is consulted.
        frm: Status the record is in.
        to: Status the caller intends to move to.

    Returns:
        The registry row, or ``None`` when the edge is not registered.
    """
    return _ROWS_BY_EDGE.get((entity, str(frm), str(to)))


def rows_from(entity: LifecycleEntity, frm: LifecycleStatus) -> tuple[TransitionRow, ...]:
    """Return every row leaving *frm*, in declaration order.

    Args:
        entity: The entity whose machine is consulted.
        frm: Source status.

    Returns:
        The outgoing rows; empty for a terminal status.

    Raises:
        KeyError: *frm* is not a status of *entity*.
    """
    return _ROWS_BY_SOURCE[(entity, str(frm))]


def is_terminal(entity: LifecycleEntity, status: LifecycleStatus) -> bool:
    """Return whether *status* has no outgoing edge for *entity*.

    Args:
        entity: The entity whose machine is consulted.
        status: The status to classify.

    Returns:
        ``True`` when nothing may leave *status*.
    """
    return str(status) in TERMINAL_STATUSES[entity]


def ambiguity_label(entity: LifecycleEntity, status: LifecycleStatus) -> AmbiguityLabel | None:
    """Return how a surface should label *status*, or ``None``.

    Args:
        entity: The entity whose machine is consulted.
        status: The status to classify.

    Returns:
        The label of an unresolved outcome, or ``None`` when the status
        says everything there is to know.
    """
    return AMBIGUOUS_STATES.get((entity, str(status)))


def render_guards(guards: tuple[TransitionGuard, ...]) -> str:
    """Return the diagram spelling of an edge's guard list.

    Args:
        guards: The guards attached to one edge, in evaluation order.

    Returns:
        The guard names joined by ``+``, or ``none`` for an unguarded
        edge. An unguarded edge renders a word rather than an empty
        bracket so a reader can tell it from a truncated line.
    """
    if not guards:
        return "none"
    return "+".join(guard.value for guard in guards)


def render_state_diagram(entity: LifecycleEntity) -> str:
    """Return the mermaid state diagram of *entity*'s machine.

    Every status is declared on its own line before the edges, so a
    terminal state and a state that merely has no edge yet are both
    visible instead of being implied by their absence.

    Args:
        entity: The entity to render.

    Returns:
        The diagram text, newline-terminated.
    """
    statuses = statuses_of(entity)
    lines = [f"%% {entity.value} lifecycle", "stateDiagram-v2"]
    lines.extend(f"    {status!s}" for status in statuses)
    for status in statuses:
        lines.extend(
            f"    {row.frm!s} --> {row.to!s}: {row.verb.value} [{render_guards(row.guards)}]"
            for row in rows_from(entity, status)
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "AMBIGUOUS_STATES",
    "DENIAL_REMEDIATION",
    "ENTITY_STATUS_ENUM",
    "GUARD_DENIALS",
    "OBSERVED_GUARD_FACTS",
    "RECOVERY_STATES",
    "SUCCESS_TERMINALS",
    "TERMINAL_STATUSES",
    "TRANSITION_ROWS",
    "AmbiguityLabel",
    "DenialCode",
    "LifecycleEntity",
    "LifecycleStatus",
    "ObservedFact",
    "TransitionGuard",
    "TransitionRow",
    "TransitionVerb",
    "ambiguity_label",
    "index_rows",
    "is_terminal",
    "render_guards",
    "render_state_diagram",
    "row_for",
    "rows_from",
    "statuses_of",
]
