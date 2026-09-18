"""Whether a retry keeps the Run it is retrying, or links a new one to it.

Retry is the identity half of dispatch. Before the provider accepted an
attempt there is nothing to preserve, so a retry is the same queued Run
and no lineage is written. After acceptance the Run's identity is
immutable, and the only thing that can carry it into a second episode is
proof: every member of :class:`ResumeGuard` must hold, and a single unmet
guard makes the retry a new Run that names its predecessor.

The guards are compiled total at import and every one of them fails
closed. An absent provider session, an unreadable lease, an event stream
with a hole in it and a Run that already stopped are all unmet guards
rather than guards nobody could evaluate, because the thing being
preserved -- one attempt identity across two episodes -- is exactly what
the missing fact was supposed to establish.

What this module does not do is admit the successor Run. A linked retry
names a Run that has already been admitted as ``QUEUED`` and binds it to
its predecessor; minting the record itself belongs to whatever admits
Runs, so a lineage line never points at a Run nothing else knows about.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.runtime.provider import Digest, SessionPolicy
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictNonNegativeInt
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.native_dispatch import (
    ACCEPTANCE_STAGE,
    LINEAGE_KEY_PREFIX,
    AttemptId,
    BoundedRef,
    DispatchAttempt,
    DispatchRefusal,
    active_lease_of,
    append_attempt,
    refused,
    run_ledger,
    stage_reached,
    standing_attempt,
    stored_run,
)
from eawf.runtime.daemon.run_events import reduce_run_events, run_events_of

logger = logging.getLogger(__name__)


class ResumeGuard(StrEnum):
    """What a retry must prove to keep the Run it is retrying.

    Each member is a fact about the world that a durable record can
    answer. A member nobody can answer is not a weaker guard: it is an
    unmet one, because the identity being preserved is the whole reason
    the proof is asked for.
    """

    SESSION_REUSE_PERMITTED = "session_reuse_permitted"
    PROVIDER_SESSION_CONTINUITY = "provider_session_continuity"
    EVENT_ORDERING_INTACT = "event_ordering_intact"
    CONTRACT_UNCHANGED = "contract_unchanged"
    LEASE_STILL_ACTIVE = "lease_still_active"
    WITHIN_RESUME_WINDOW = "within_resume_window"
    CONTINUITY_BUDGET_REMAINING = "continuity_budget_remaining"
    RUN_NOT_TERMINAL = "run_not_terminal"


class RetryDisposition(StrEnum):
    """What one retry does to the Run's identity."""

    SAME_QUEUED_RUN = "same_queued_run"
    RESUME_SAME_RUN = "resume_same_run"
    LINKED_RUN = "linked_run"


class ContinuityProof(BaseModel):
    """What only the live world knows about a retry's continuity.

    Everything else a resume guard reads is durable, so the proof carries
    exactly the two facts the ledger cannot hold: the provider session a
    retry believes it can reattach to, and the contract digest a fresh
    compile produced for it.

    Attributes:
        provider_session_ref: The provider session the retry offers.
        compiled_spec_digest: The digest the recompile produced.
        observed_at: When the offer was observed, which is what the
            resume window is measured against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_session_ref: BoundedRef | None = None
    compiled_spec_digest: Digest | None = None
    observed_at: UtcDatetime | None = None


class RetryLineage(BaseModel):
    """The durable link between a retried Run and the Run that replaced it.

    Attributes:
        payload_kind: The discriminator separating a lineage line from
            every other line of the run ledger.
        run_ref: The successor, which is the Run the lineage is filed
            under.
        retry_of_run_ref: The predecessor it replaces.
        predecessor_attempt_ref: The attempt whose acceptance made the
            predecessor's identity immutable.
        unmet_guards: Every resume guard that did not hold, which is why
            a new Run exists at all. Never empty: a lineage line with
            nothing unmet would claim a link the guards did not require.
        actor: Who asked for the retry.
        recorded_at: When the daemon appended the line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["retry_lineage"] = "retry_lineage"
    run_ref: RunUrn
    retry_of_run_ref: RunUrn
    predecessor_attempt_ref: AttemptId
    unmet_guards: Annotated[tuple[ResumeGuard, ...], Field(min_length=1)]
    actor: PrincipalKey
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _lineage_is_not_a_cycle(self) -> Self:
        """Refuse a successor that names itself as the Run it replaces.

        Raises:
            ValueError: The two references are the same Run.
        """
        if self.run_ref == self.retry_of_run_ref:
            raise ValueError("a retry lineage links two different Runs")
        return self


class RetryParams(BaseModel):
    """Params of :data:`RUN_RETRY_METHOD`.

    Attributes:
        urn: The Run being retried.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        continuity: What the caller offers as proof of continuity.
        successor_urn: The already-admitted QUEUED Run a linked retry
            runs as. Required exactly when a guard fails.
        session_policy: The session policy the predecessor compiled
            under, whose window and continuity budget the guards read.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    actor: PrincipalKey
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    continuity: ContinuityProof = ContinuityProof()
    successor_urn: RunUrn | None = None
    session_policy: SessionPolicy = SessionPolicy()


class RetryAnswer(BaseModel):
    """What one retry decision answers with.

    Attributes:
        disposition: What the retry does to the Run's identity.
        unmet_guards: Every resume guard that did not hold, in the
            enumeration's own order.
        run_ref: The Run that was retried.
        successor_run_ref: The Run a linked retry runs as, or ``None``.
        attempt_ref: The predecessor's attempt identity, or ``None`` when
            no attempt was ever recorded.
        continuity_attempts: How many resumes the attempt has now been
            granted.
        lineage: The lineage line a linked retry wrote, or ``None``.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: RetryDisposition
    unmet_guards: tuple[ResumeGuard, ...] = ()
    run_ref: RunUrn
    successor_run_ref: RunUrn | None = None
    attempt_ref: AttemptId | None = None
    continuity_attempts: StrictNonNegativeInt = 0
    lineage: dict[str, Any] | None = None
    reason: str


def lineage_of(records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> RetryLineage | None:
    """Return the lineage filed for one successor Run, or ``None``."""
    for item in records:
        if item.payload.get("payload_kind") != "retry_lineage":
            continue
        lineage = RetryLineage.model_validate(item.payload)
        if lineage.run_ref == urn:
            return lineage
    return None


@dataclass(frozen=True, slots=True)
class ResumeInputs:
    """Everything a resume guard is allowed to read.

    Attributes:
        attempt: The predecessor's standing attempt.
        proof: What the caller offers about the live world.
        policy: The session policy the predecessor compiled under.
        lease: The Run's live lease, or ``None``.
        run_status: The status the Run's confirmed effects support.
        derivation_stopped: Whether a hole sits inside the event stream.
        last_activity_at: The last event the Run recorded, if any.
        now: The instant the retry is judged at.
    """

    attempt: DispatchAttempt
    proof: ContinuityProof
    policy: SessionPolicy
    lease: WorkLease | None
    run_status: RunStatus
    derivation_stopped: bool
    last_activity_at: datetime | None
    now: datetime


GuardEvaluator = Callable[[ResumeInputs], bool]


def _session_reuse_permitted(inputs: ResumeInputs) -> bool:
    """Return whether the compiled session policy admits reuse at all."""
    return inputs.policy.reuse == "same_run_continuity"


def _provider_session_continuity(inputs: ResumeInputs) -> bool:
    """Return whether the offered session is the one the attempt started."""
    offered = inputs.proof.provider_session_ref
    return offered is not None and offered == inputs.attempt.provider_session_ref


def _event_ordering_intact(inputs: ResumeInputs) -> bool:
    """Return whether the Run's event stream has no hole inside it."""
    return not inputs.derivation_stopped


def _contract_unchanged(inputs: ResumeInputs) -> bool:
    """Return whether a fresh compile produced the attempt's contract."""
    offered = inputs.proof.compiled_spec_digest
    return offered is not None and offered == inputs.attempt.compiled_spec_digest


def _lease_still_active(inputs: ResumeInputs) -> bool:
    """Return whether the workspace the attempt wrote in is still leased."""
    lease = inputs.lease
    return lease is not None and lease.lease_id == inputs.attempt.lease_id


def _within_resume_window(inputs: ResumeInputs) -> bool:
    """Return whether the Run fell quiet recently enough to reattach.

    The window is measured from the last thing the daemon recorded about
    the Run: an event when the stream has one, and the attempt line
    otherwise. A Run whose provider emitted nothing is not therefore
    outside every window -- the attempt line is a durable stamp of when
    the daemon last knew the Run to be alive.
    """
    last = inputs.last_activity_at or inputs.attempt.recorded_at
    elapsed = (inputs.now - last).total_seconds()
    return 0 <= elapsed <= inputs.policy.resume_window_seconds


def _continuity_budget_remaining(inputs: ResumeInputs) -> bool:
    """Return whether the attempt may be resumed one more time."""
    return inputs.attempt.continuity_attempts < inputs.policy.max_continuity_attempts


def _run_not_terminal(inputs: ResumeInputs) -> bool:
    """Return whether the Run has not already stopped."""
    return inputs.run_status not in TERMINAL_RUN_STATUSES


def compile_resume_guards(
    evaluators: Mapping[ResumeGuard, GuardEvaluator],
) -> Mapping[ResumeGuard, GuardEvaluator]:
    """Return *evaluators* once it answers every declared resume guard.

    Args:
        evaluators: One predicate per guard.

    Returns:
        The table, unchanged.

    Raises:
        ValueError: A guard has no evaluator, which would let a retry
            preserve a Run's identity without the proof that guard names.
    """
    missing = sorted(guard.value for guard in ResumeGuard if guard not in evaluators)
    if missing:
        raise ValueError(f"no evaluator declared for resume guard {', '.join(missing)}")
    return evaluators


#: The total guard table, compiled at import. Each predicate fails closed:
#: an absent fact is an unmet guard, because the identity being preserved
#: is exactly what the proof is asked for.
RESUME_GUARDS: Final[Mapping[ResumeGuard, GuardEvaluator]] = compile_resume_guards(
    {
        ResumeGuard.SESSION_REUSE_PERMITTED: _session_reuse_permitted,
        ResumeGuard.PROVIDER_SESSION_CONTINUITY: _provider_session_continuity,
        ResumeGuard.EVENT_ORDERING_INTACT: _event_ordering_intact,
        ResumeGuard.CONTRACT_UNCHANGED: _contract_unchanged,
        ResumeGuard.LEASE_STILL_ACTIVE: _lease_still_active,
        ResumeGuard.WITHIN_RESUME_WINDOW: _within_resume_window,
        ResumeGuard.CONTINUITY_BUDGET_REMAINING: _continuity_budget_remaining,
        ResumeGuard.RUN_NOT_TERMINAL: _run_not_terminal,
    }
)


def unmet_resume_guards(inputs: ResumeInputs) -> tuple[ResumeGuard, ...]:
    """Return every resume guard that does not hold, in declaration order."""
    return tuple(guard for guard in ResumeGuard if not RESUME_GUARDS[guard](inputs))


def _lineage_record(session: RootSession, lineage: RetryLineage) -> None:
    """Append one retry lineage line to the run ledger."""
    append_ledger_record(
        run_ledger(session),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"{LINEAGE_KEY_PREFIX}{lineage.run_ref.entity_key}",
            status=RetryDisposition.LINKED_RUN.value,
            recorded_at=lineage.recorded_at,
            payload=lineage.model_dump(mode="json"),
        ),
    )


def retry_run(context: Epoch2RootContext, args: RetryParams, *, now: datetime) -> RetryAnswer:
    """Decide what one retry does to a Run's identity, and record it.

    Before the provider accepted the attempt there is nothing to
    preserve and the same queued Run is retried. After acceptance the
    identity is immutable unless every resume guard holds: one unmet
    guard makes the retry a new Run carrying ``retry_of_run_ref``.

    Args:
        context: The native context of the canary the Run lives in.
        args: The validated retry request.
        now: The stamp the decision is taken at.

    Returns:
        The decision, naming every guard that did not hold.

    Raises:
        DaemonValidationError: The Run holds no record or no attempt, a
            linked retry names no admitted successor, or the successor is
            not a Run a retry may run as.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, args.urn)
        attempt = standing_attempt(records, args.urn)
        events = run_events_of(records, args.urn)
    if attempt is None:
        raise refused(
            DispatchRefusal.ATTEMPT_ABSENT,
            f"run {args.urn.entity_key!r} has no dispatch attempt, so there is nothing to retry",
        )
    if not stage_reached(attempt.stage, ACCEPTANCE_STAGE):
        return _same_queued_run(args, attempt=attempt)
    state = reduce_run_events(events)
    inputs = ResumeInputs(
        attempt=attempt,
        proof=args.continuity,
        policy=args.session_policy,
        lease=active_lease_of(context, run_ref=str(args.urn), now=now),
        run_status=run.status,
        derivation_stopped=state.derivation_stopped,
        last_activity_at=state.last_activity_at,
        now=now,
    )
    unmet = unmet_resume_guards(inputs)
    if not unmet:
        return _resume_same_run(context, args, attempt=attempt, now=now)
    return _link_successor(context, args, attempt=attempt, unmet=unmet, now=now)


def _same_queued_run(args: RetryParams, *, attempt: DispatchAttempt) -> RetryAnswer:
    """Answer a retry taken before the provider accepted anything.

    Raises:
        DaemonValidationError: The caller named a successor, which would
            link a Run to a predecessor whose identity never became
            immutable.
    """
    if args.successor_urn is not None:
        raise refused(
            DispatchRefusal.SUCCESSOR_REFUSED,
            f"attempt {attempt.attempt_ref} is {attempt.stage.value}: the provider accepted "
            "nothing, so the same queued Run is retried and no successor is linked",
        )
    return RetryAnswer(
        disposition=RetryDisposition.SAME_QUEUED_RUN,
        run_ref=args.urn,
        attempt_ref=attempt.attempt_ref,
        continuity_attempts=attempt.continuity_attempts,
        reason=(
            f"attempt {attempt.attempt_ref} stopped at {attempt.stage.value}, before the "
            "provider accepted it, so the queued Run is retried unchanged"
        ),
    )


def _resume_same_run(
    context: Epoch2RootContext, args: RetryParams, *, attempt: DispatchAttempt, now: datetime
) -> RetryAnswer:
    """Answer a retry whose every resume guard held, spending one attempt.

    Raises:
        DaemonValidationError: The caller named a successor, which a
            proven resume does not use.
    """
    if args.successor_urn is not None:
        raise refused(
            DispatchRefusal.SUCCESSOR_REFUSED,
            "every resume guard holds, so the Run keeps its identity and the named successor "
            "would be a second Run nothing needs",
        )
    moved = attempt.model_copy(
        update={"continuity_attempts": attempt.continuity_attempts + 1, "recorded_at": now}
    )
    with context.session([args.urn]) as session:
        append_attempt(session, moved)
    logger.info(
        f"retry_run resumed run={args.urn.entity_key!r} attempt={attempt.attempt_ref} "
        f"continuity_attempts={moved.continuity_attempts}"
    )
    return RetryAnswer(
        disposition=RetryDisposition.RESUME_SAME_RUN,
        run_ref=args.urn,
        attempt_ref=attempt.attempt_ref,
        continuity_attempts=moved.continuity_attempts,
        reason=(
            f"every resume guard holds, so attempt {attempt.attempt_ref} keeps this Run's "
            "identity and spends one continuity attempt"
        ),
    )


def _link_successor(
    context: Epoch2RootContext,
    args: RetryParams,
    *,
    attempt: DispatchAttempt,
    unmet: tuple[ResumeGuard, ...],
    now: datetime,
) -> RetryAnswer:
    """Link a new Run to the predecessor a guard would not let it keep.

    Raises:
        DaemonValidationError: No successor was named, the named Run holds
            no record, it is not queued, or it already carries a lineage
            naming another predecessor.
    """
    successor = args.successor_urn
    if successor is None:
        raise refused(
            DispatchRefusal.SUCCESSOR_REQUIRED,
            f"resume guards {', '.join(guard.value for guard in unmet)} do not hold, so this "
            "retry runs as a new Run and the request names none",
        )
    with context.session([args.urn, successor]) as session:
        records = read_ledger_records(run_ledger(session))
        standing = lineage_of(records, successor)
        if standing is not None:
            return _lineage_answer(args, lineage=standing, attempt=attempt, replayed=True)
        run = stored_run(session, records, successor)
        if run.status is not RunStatus.QUEUED:
            raise refused(
                DispatchRefusal.SUCCESSOR_REFUSED,
                f"run {successor.entity_key!r} is {run.status.value}; a linked retry runs as a "
                "Run that has not started",
            )
        try:
            lineage = RetryLineage(
                run_ref=successor,
                retry_of_run_ref=args.urn,
                predecessor_attempt_ref=attempt.attempt_ref,
                unmet_guards=unmet,
                actor=args.actor,
                recorded_at=now,
            )
        except ValidationError as error:
            raise refused(
                DispatchRefusal.SUCCESSOR_REFUSED,
                f"the successor is not a Run this retry may link: {error.error_count()} field(s) "
                "do not satisfy the lineage record",
            ) from error
        _lineage_record(session, lineage)
    logger.info(
        f"retry_run linked run={successor.entity_key!r} "
        f"retry_of={args.urn.entity_key!r} unmet={len(unmet)}"
    )
    return _lineage_answer(args, lineage=lineage, attempt=attempt, replayed=False)


def _lineage_answer(
    args: RetryParams, *, lineage: RetryLineage, attempt: DispatchAttempt, replayed: bool
) -> RetryAnswer:
    """Build the answer a linked retry returns for one lineage line."""
    named = ", ".join(guard.value for guard in lineage.unmet_guards)
    return RetryAnswer(
        disposition=RetryDisposition.LINKED_RUN,
        unmet_guards=lineage.unmet_guards,
        run_ref=args.urn,
        successor_run_ref=lineage.run_ref,
        attempt_ref=attempt.attempt_ref,
        continuity_attempts=attempt.continuity_attempts,
        lineage=lineage.model_dump(mode="json"),
        reason=(
            f"this Run's lineage already names {lineage.run_ref.entity_key}"
            if replayed
            else f"resume guards {named} do not hold, so the retry runs as a new linked Run"
        ),
    )


__all__ = [
    "RESUME_GUARDS",
    "ContinuityProof",
    "GuardEvaluator",
    "ResumeGuard",
    "ResumeInputs",
    "RetryAnswer",
    "RetryDisposition",
    "RetryLineage",
    "RetryParams",
    "compile_resume_guards",
    "lineage_of",
    "retry_run",
    "unmet_resume_guards",
]
