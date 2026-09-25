"""The ``runtime.run.*`` verbs: bind a Run, control it, watch it, rebuild it.

Nine verbs sit behind the epoch-2 fence, and between them they are the
only producer and the only consumer of a Run's control ledger, its event
stream and its worker handshake.

``runtime.run.bind`` appends the Run's contract binding once, at
dispatch: the compiled-spec and authority-capsule digests plus the route
revision they were resolved under. Nothing else writes them, so the
contract cannot drift from what the provider was actually handed.

The three control verbs append one fact each. ``request`` records that a
principal asked and moves nothing. ``acknowledge`` decides the Run's one
control lease under the root's entity lock, so two principals racing the
same Run produce one holder and one ``superseded`` row -- and the losing
row carries no receipt, because it was neither applied nor refused.
``effect`` records what was observed, and only there can the Run
terminalize: the fact is appended first and the canonical transition
follows it, so a process lost between the two leaves a confirmed effect
over a record that has not caught up, which the reducer reads correctly,
rather than a moved record no fact explains.

``runtime.run.contract.read`` rebuilds the whole contract from those
durable records and nothing else. It opens no session state, consults no
conversation, and answers the same thing on a tree whose daemon was
killed mid-flight as on one that shut down cleanly.

``runtime.run.worker.hello`` is the gate in front of everything a worker
can do. It records what the worker claims about the contract it holds and
what the daemon decided about that claim, and ``runtime.run.tools.grant``
opens nothing until an accepted claim is on the ledger: with no hello, or
with a mismatched one, the grant is empty and says which.

``runtime.run.event.append`` orders the episode. It reads terminality
from the control ledger's effect truth rather than from the stored Run
record, because those two legitimately disagree between a confirmed
effect and the transition that follows it, and an event arriving inside
that window must be quarantined rather than admitted.
``runtime.run.events.read`` reports the ordered stream, the holes in it
and how long the Run has been quiet.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.control import (
    TERMINAL_RUN_STATUSES,
    ControlDisposition,
    ControlEffectId,
    ControlFact,
    ControlPhase,
    ControlRequestId,
    ReconciliationReceiptId,
    RunBinding,
)
from eawf.kernel.runtime.events import EventGapId, RunEventKind, RunEventRecord
from eawf.kernel.runtime.handshake import (
    RUNTIME_HANDSHAKE_MISMATCH,
    HandshakeDisposition,
    WorkerHello,
    WorkerHelloFact,
    assess_worker_hello,
    decide_tool_grant,
)
from eawf.kernel.runtime.provider import ControlKind, Digest, reject_repeats
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    read_ledger_records,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import (
    RunContract,
    decide_control_lease,
    project_disposition,
    reconstruct_run_contract,
    reduce_run_control,
)
from eawf.runtime.daemon.epoch2_recovery import PROJECTION_DEGRADED, publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusedError,
    TransitionRequest,
    commit_ledger_append,
    run_transaction,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.native_dispatch import (
    RUN_DISPATCH_METHOD,
    RUN_RETRY_METHOD,
    DispatchParams,
    dispatch_run,
    run_binding_of,
    stored_run,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator, require_native_call
from eawf.runtime.daemon.native_retry import RetryParams, retry_run
from eawf.runtime.daemon.run_events import (
    AppendDisposition,
    RunEventAppend,
    RunEventsRead,
    RunLiveness,
    assess_stall,
    hello_facts_of,
    next_hello_sequence,
    plan_event_append,
    reduce_run_events,
    run_events_of,
)

logger = logging.getLogger(__name__)


#: The verb that records a Run's durable contract binding.
RUN_BIND_METHOD: Final = "runtime.run.bind"

#: The three control verbs, one per phase of one control.
RUN_CONTROL_REQUEST_METHOD: Final = "runtime.run.control.request"
RUN_CONTROL_ACKNOWLEDGE_METHOD: Final = "runtime.run.control.acknowledge"
RUN_CONTROL_EFFECT_METHOD: Final = "runtime.run.control.effect"

#: The read that rebuilds a whole Run contract from durable records.
RUN_CONTRACT_READ_METHOD: Final = "runtime.run.contract.read"

#: The handshake a worker announces itself with, and the grant it gates.
RUN_WORKER_HELLO_METHOD: Final = "runtime.run.worker.hello"
RUN_TOOLS_GRANT_METHOD: Final = "runtime.run.tools.grant"

#: The two verbs of a Run's ordered event stream.
RUN_EVENT_APPEND_METHOD: Final = "runtime.run.event.append"
RUN_EVENTS_READ_METHOD: Final = "runtime.run.events.read"

#: The ledger-line key a Run's contract binding is filed under. It is
#: prefixed rather than spelled as the Run key, because the run ledger
#: also holds compacted Run records and a shared key would make one
#: record look like it was in the document and the ledger at once.
_BINDING_KEY_PREFIX: Final = "BND-"

#: The status a binding line records. Control lines record their phase.
_BINDING_STATUS: Final = "bound"

#: The stable reason a Run terminalized by a confirmed control effect.
_EFFECT_REASON_CODE: Final = "control-effect-confirmed"

#: The ledger-line key prefix a handshake fact is filed under, for the
#: same reason a binding carries one: the run collection holds compacted
#: Run records too, and a shared key would make one record look like it
#: was in the document and the ledger at once.
_HELLO_KEY_PREFIX: Final = "HLO-"

#: How many random bytes a minted gap identity carries.
_GAP_ENTROPY_BYTES: Final = 8


class _RunParams(BaseModel):
    """The Run every ``runtime.run.*`` verb addresses."""

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn


class _BindParams(_RunParams):
    """Params of :data:`RUN_BIND_METHOD`."""

    compiled_spec_digest: Digest
    authority_capsule_digest: Digest
    route_policy_revision: StrictPositiveInt


class _RequestParams(_RunParams):
    """Params of :data:`RUN_CONTROL_REQUEST_METHOD`."""

    control_request_ref: ControlRequestId
    control: ControlKind
    actor: PrincipalKey


class _AcknowledgeParams(_RunParams):
    """Params of :data:`RUN_CONTROL_ACKNOWLEDGE_METHOD`."""

    control_request_ref: ControlRequestId
    actor: PrincipalKey
    decision: Literal["accepted", "rejected", "invalidated"] = "accepted"


class _EffectParams(_RunParams):
    """Params of :data:`RUN_CONTROL_EFFECT_METHOD`."""

    control_request_ref: ControlRequestId
    actor: PrincipalKey
    disposition: Literal["confirmed", "unknown", "recovery"]
    effect_ref: ControlEffectId | None = None
    receipt_ref: ReconciliationReceiptId | None = None
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


class _HelloParams(_RunParams):
    """Params of :data:`RUN_WORKER_HELLO_METHOD`."""

    hello: WorkerHello
    actor: PrincipalKey


class _ToolGrantParams(_RunParams):
    """Params of :data:`RUN_TOOLS_GRANT_METHOD`.

    The capsule's grants are submitted rather than looked up, because the
    capsule is digest-bound and never stored: what the daemon holds is
    the digest the worker must echo, and what it decides here is whether
    anything at all opens behind that echo.
    """

    tools: Annotated[tuple[SemanticToolId, ...], AfterValidator(reject_repeats)] = ()


class WorkerHelloAnswer(BaseModel):
    """What the handshake verb answers with.

    Attributes:
        fact: The handshake fact that now stands in the ledger.
        disposition: Accepted or mismatched.
        mismatched_fields: The contract fields that differed, empty on an
            acceptance. Field names only, never their values.
        refusal_code: Why no tool grant opens, or ``None`` on an
            acceptance.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact: dict[str, Any]
    disposition: HandshakeDisposition
    mismatched_fields: tuple[str, ...] = ()
    refusal_code: str | None = None
    reason: str


class ToolGrantAnswer(BaseModel):
    """What the tool-grant verb answers with.

    Attributes:
        granted: The tools that open. It is empty whenever the handshake
            has not earned them, which is the whole point of the verb.
        refusal_code: Why nothing opened, or ``None`` on a grant.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    granted: tuple[SemanticToolId, ...]
    refusal_code: str | None = None
    reason: str


class RunEventAnswer(BaseModel):
    """What one event append answers with.

    Attributes:
        event: The line that now stands for the request.
        disposition: What the append did to the stream.
        run_status: The status the Run's confirmed effects support. An
            append never moves it.
        last_contiguous_sequence: The highest sequence with nothing
            missing in front of it.
        next_sequence: The sequence the next event is expected to carry.
        gap: The hole this append recorded, or ``None``.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: dict[str, Any]
    disposition: AppendDisposition
    run_status: RunStatus
    last_contiguous_sequence: StrictNonNegativeInt
    next_sequence: StrictPositiveInt
    gap: dict[str, Any] | None = None
    reason: str


class RunStallAnswer(BaseModel):
    """How long a Run has been quiet, and what clears it.

    Attributes:
        verdict: Live, stalled, or unknown for a Run with no activity.
        last_activity_at: The activity the silence is measured from.
        last_activity_kind: What that activity was.
        elapsed_seconds: How long the Run has produced nothing.
        interval_seconds: The interval it was measured against.
        resume_method: The verb a principal resumes it through. A stall
            never terminates the Run, so this is an offer and not a
            record of something the daemon did.
        resume_control: The control that resume request asks for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: RunLiveness
    last_activity_at: str | None = None
    last_activity_kind: RunEventKind | None = None
    elapsed_seconds: float
    interval_seconds: StrictNonNegativeInt
    resume_method: str
    resume_control: ControlKind


class RunEventsAnswer(BaseModel):
    """What a read of one Run's stream answers with.

    Attributes:
        events: The derived stream, in sequence order.
        quarantined: The lines retained as diagnostics and derived from
            by nothing.
        gaps: Every recorded hole, in stream order.
        last_contiguous_sequence: Where derivation is sound up to.
        next_sequence: The sequence the next event is expected to carry.
        derivation_stopped: Whether a hole sits inside the stream.
        run_status: The status the Run's confirmed effects support.
        stall: The Run's liveness at the moment of the read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[dict[str, Any], ...]
    quarantined: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]
    last_contiguous_sequence: StrictNonNegativeInt
    next_sequence: StrictPositiveInt
    derivation_stopped: bool
    run_status: RunStatus
    stall: RunStallAnswer


class RunControlAnswer(BaseModel):
    """What one control verb answers with.

    Attributes:
        fact: The control fact that now stands in the ledger.
        disposition: That fact's rendered outcome.
        run_status: The status the Run's confirmed effects support.
        control_cursor: The last applied control-ledger sequence.
        receipt: The canonical mutation receipt, present only where a
            confirmed effect terminalized the Run and the transition
            committed.
        warnings: Non-fatal notes about an answer that still stands.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact: dict[str, Any]
    disposition: ControlDisposition
    run_status: RunStatus
    control_cursor: StrictNonNegativeInt
    receipt: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _EffectOutcome:
    """The effect verb's answer beside the envelope waiting to publish."""

    answer: RunControlAnswer
    envelope: Envelope | None


def _params[ParamsT: BaseModel](model: type[ParamsT], params: dict[str, Any]) -> ParamsT:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
    """
    try:
        return model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _ledger(session: RootSession) -> Path:
    """Return the run collection's append-only ledger in this generation."""
    return session.ledger_path(Epoch2Collection.RUN)


def _control_facts(records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> tuple[ControlFact, ...]:
    """Return one Run's control facts from the run ledger, in sequence order.

    Args:
        records: Every line the run ledger holds.
        urn: The Run to select.

    Returns:
        The Run's control facts, ordered by their own sequence.

    Raises:
        ValidationError: A line claims to be a control fact and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    facts = [
        ControlFact.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "control"
    ]
    selected = (fact for fact in facts if fact.run_ref == urn)
    return tuple(sorted(selected, key=lambda fact: fact.sequence))


#: The two durable readers the dispatch path and these verbs share. They
#: live with the dispatcher because it is the writer of both records, and
#: a second copy here would let the reader of a binding disagree with
#: whoever wrote it.
_binding_of = run_binding_of
_stored_run = stored_run


def _append_fact(session: RootSession, fact: ControlFact, *, now: datetime) -> None:
    """Append one control fact as a line of the run ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=fact.control_request_ref,
            status=fact.phase.value,
            recorded_at=now,
            payload=fact.model_dump(mode="json"),
        ),
    )


def _answer(
    fact: ControlFact, facts: tuple[ControlFact, ...], status: RunStatus
) -> RunControlAnswer:
    """Build the answer one control verb returns."""
    state = reduce_run_control(status=status, facts=facts)
    return RunControlAnswer(
        fact=fact.model_dump(mode="json"),
        disposition=fact.disposition,
        run_status=state.status,
        control_cursor=state.control_cursor,
    )


def _existing_fact(
    facts: tuple[ControlFact, ...], *, ref: ControlRequestId, phase: ControlPhase
) -> ControlFact | None:
    """Return the fact a retry of this phase is already answered by."""
    for fact in facts:
        if fact.control_request_ref == ref and fact.phase is phase:
            return fact
    return None


def _requested_control(facts: tuple[ControlFact, ...], ref: ControlRequestId) -> ControlKind:
    """Return which control *ref* asked for.

    Raises:
        DaemonValidationError: The request was never recorded, so there
            is no control to acknowledge or to effect.
    """
    for fact in facts:
        if fact.control_request_ref == ref and fact.phase is ControlPhase.REQUESTED:
            return fact.control
    raise DaemonValidationError(
        f"validation_failed: identity_not_found: control request {ref!r} has no requested fact "
        "on this Run"
    )


def _bind(context: Epoch2RootContext, args: _BindParams, *, now: datetime) -> dict[str, Any]:
    """Append one Run's contract binding, or answer the one it already has."""
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        existing = _binding_of(records, args.urn)
        if existing is not None:
            return existing.model_dump(mode="json")
        binding = RunBinding(
            run_ref=args.urn,
            compiled_spec_digest=args.compiled_spec_digest,
            authority_capsule_digest=args.authority_capsule_digest,
            route_policy_revision=args.route_policy_revision,
            bound_at=now,
        )
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=f"{_BINDING_KEY_PREFIX}{args.urn.entity_key}",
                status=_BINDING_STATUS,
                recorded_at=now,
                payload=binding.model_dump(mode="json"),
            ),
        )
        logger.info(
            f"_bind run={args.urn.entity_key!r} "
            f"route_policy_revision={binding.route_policy_revision}"
        )
        return binding.model_dump(mode="json")


def _request(
    context: Epoch2RootContext, args: _RequestParams, *, now: datetime
) -> RunControlAnswer:
    """Record that a principal asked, and move the Run not at all."""
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        facts = _control_facts(records, args.urn)
        run = _stored_run(session, records, args.urn)
        standing = _existing_fact(facts, ref=args.control_request_ref, phase=ControlPhase.REQUESTED)
        if standing is not None:
            return _answer(standing, facts, run.status)
        fact = ControlFact(
            control_request_ref=args.control_request_ref,
            run_ref=args.urn,
            control=args.control,
            phase=ControlPhase.REQUESTED,
            disposition=ControlDisposition.REQUESTING,
            actor=args.actor,
            recorded_at=now,
            sequence=len(facts) + 1,
        )
        _append_fact(session, fact, now=now)
        return _answer(fact, (*facts, fact), run.status)


def _acknowledge(
    context: Epoch2RootContext, args: _AcknowledgeParams, *, now: datetime
) -> RunControlAnswer:
    """Decide the Run's one control lease and record what was decided.

    Raises:
        DaemonValidationError: The request was never recorded, or it has
            already been acknowledged.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        facts = _control_facts(records, args.urn)
        run = _stored_run(session, records, args.urn)
        standing = _existing_fact(
            facts, ref=args.control_request_ref, phase=ControlPhase.ACKNOWLEDGED
        )
        if standing is not None:
            return _answer(standing, facts, run.status)
        control = _requested_control(facts, args.control_request_ref)
        disposition = _acknowledged_disposition(args, facts=facts)
        fact = ControlFact(
            control_request_ref=args.control_request_ref,
            run_ref=args.urn,
            control=control,
            phase=ControlPhase.ACKNOWLEDGED,
            disposition=disposition,
            actor=args.actor,
            recorded_at=now,
            sequence=len(facts) + 1,
        )
        _append_fact(session, fact, now=now)
        return _answer(fact, (*facts, fact), run.status)


def _acknowledged_disposition(
    args: _AcknowledgeParams, *, facts: tuple[ControlFact, ...]
) -> ControlDisposition:
    """Return the disposition this acknowledgement records.

    A caller that refuses or invalidates the request says so and the
    lease is not consulted; only an acceptance competes for it.

    Raises:
        DaemonValidationError: The lease decision refuses the request.
    """
    if args.decision != "accepted":
        return ControlDisposition(args.decision)
    try:
        decision = decide_control_lease(control_request_ref=args.control_request_ref, facts=facts)
    except ValueError as error:
        raise DaemonValidationError(f"validation_failed: illegal_transition: {error}") from error
    logger.info(
        f"_acknowledged_disposition request={args.control_request_ref!r} "
        f"disposition={decision.disposition.value} holder={decision.holder!r}"
    )
    return decision.disposition


def _effect(context: Epoch2RootContext, args: _EffectParams, *, now: datetime) -> _EffectOutcome:
    """Record what was observed, then move the record the observation ends.

    Raises:
        DaemonValidationError: The request was never recorded, was never
            accepted, or the proof does not match the disposition.
        TransactionRefusedError: The canonical transition was refused.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        facts = _control_facts(records, args.urn)
        run = _stored_run(session, records, args.urn)
        standing = _existing_fact(facts, ref=args.control_request_ref, phase=ControlPhase.EFFECTED)
        fact = standing if standing is not None else _effect_fact(args, facts=facts, now=now)
        if standing is None:
            _append_fact(session, fact, now=now)
            facts = (*facts, fact)
    return _terminalize(context, args, run=run, fact=fact, facts=facts, now=now)


def _effect_fact(
    args: _EffectParams, *, facts: tuple[ControlFact, ...], now: datetime
) -> ControlFact:
    """Build the effected fact, refusing one no accepted request earned.

    Raises:
        DaemonValidationError: The request never held the control lease,
            so an effect attributed to it would credit a principal whose
            control was superseded, rejected or never acknowledged; or
            the proof the disposition needs is absent.
    """
    control = _requested_control(facts, args.control_request_ref)
    own = [fact for fact in facts if fact.control_request_ref == args.control_request_ref]
    if project_disposition(own) is not ControlDisposition.ACCEPTED:
        raise DaemonValidationError(
            f"validation_failed: illegal_transition: control request "
            f"{args.control_request_ref!r} does not hold this Run's control lease, so no "
            "effect is recorded for it"
        )
    try:
        return ControlFact(
            control_request_ref=args.control_request_ref,
            run_ref=args.urn,
            control=control,
            phase=ControlPhase.EFFECTED,
            disposition=ControlDisposition(args.disposition),
            effect_ref=args.effect_ref,
            receipt_ref=args.receipt_ref,
            actor=args.actor,
            recorded_at=now,
            sequence=len(facts) + 1,
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: the effect does not carry the proof "
            f"its disposition requires; check {', '.join(fields) or 'effect_ref, receipt_ref'}"
        ) from error


def _terminalize(
    context: Epoch2RootContext,
    args: _EffectParams,
    *,
    run: Run,
    fact: ControlFact,
    facts: tuple[ControlFact, ...],
    now: datetime,
) -> _EffectOutcome:
    """Commit the transition a confirmed effect makes a fact, if any.

    The reducer decides, not the caller: an undetermined effect leaves the
    record where it is, and a confirmed effect of a control that ends no
    Run does the same.
    """
    state = reduce_run_control(status=run.status, facts=facts)
    answer = RunControlAnswer(
        fact=fact.model_dump(mode="json"),
        disposition=fact.disposition,
        run_status=state.status,
        control_cursor=state.control_cursor,
    )
    if state.status is run.status:
        return _EffectOutcome(answer=answer, envelope=None)
    committed = run_transaction(
        context=context,
        request=TransitionRequest.model_validate(
            {
                "urn": args.urn,
                "to_status": str(state.status),
                "expected_revision": run.revision,
                "idempotency_key": args.idempotency_key,
                "actor": args.actor,
                "updates": _terminal_updates(run, now=now),
                "reason_code": _EFFECT_REASON_CODE,
                "binding_refs": (args.control_request_ref,),
            }
        ),
        now=now,
    )
    return _EffectOutcome(
        answer=answer.model_copy(update={"receipt": committed.receipt.model_dump(mode="json")}),
        envelope=committed.envelope,
    )


def _terminal_updates(run: Run, *, now: datetime) -> dict[str, Any]:
    """Return the fields the Run's terminal edge makes facts.

    A Run cancelled out of the queue never started, so the edge requires
    the start stamp as well: the duration of a stopped Run is a recorded
    fact rather than a difference against now.
    """
    updates: dict[str, Any] = {"ended_at": now.isoformat()}
    if run.status is RunStatus.QUEUED:
        updates["started_at"] = now.isoformat()
    if run.status is RunStatus.SUSPENDED:
        updates["suspension_reason"] = None
    return updates


def _append_event_line(session: RootSession, event: RunEventRecord, *, now: datetime) -> None:
    """Append one Run event as a line of the run ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=event.event_ref,
            status=event.event_kind.value,
            recorded_at=now,
            payload=event.model_dump(mode="json"),
        ),
    )


def _mint_gap_ref() -> EventGapId:
    """Return a fresh identity for a hole the daemon is about to record."""
    return f"GAP-{secrets.token_hex(_GAP_ENTROPY_BYTES)}"


def _hello(context: Epoch2RootContext, args: _HelloParams, *, now: datetime) -> WorkerHelloAnswer:
    """Record what a worker claims and what the daemon decided about it.

    Raises:
        DaemonValidationError: The Run has no contract binding to compare
            the claim against, or the announcement repeats a sequence the
            Run has already moved past, which would let an older claim
            overwrite a newer decision.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        binding = _binding_of(records, args.urn)
        if binding is None:
            raise DaemonValidationError(
                f"validation_failed: identity_not_found: run {args.urn.entity_key!r} has no "
                "contract binding, so there is nothing to check the announcement against"
            )
        facts = hello_facts_of(records, args.urn)
        standing = _standing_hello(facts, args.hello.hello_sequence)
        if standing is not None:
            return _hello_answer(standing)
        expected = next_hello_sequence(facts)
        if args.hello.hello_sequence < expected:
            raise DaemonValidationError(
                f"validation_failed: illegal_transition: hello_sequence "
                f"{args.hello.hello_sequence} does not reach {expected}, so the announcement "
                "restates a claim this Run has already moved past"
            )
        outcome = assess_worker_hello(
            hello=args.hello,
            run_ref=args.urn,
            compiled_spec_digest=binding.compiled_spec_digest,
            authority_capsule_digest=binding.authority_capsule_digest,
        )
        fact = WorkerHelloFact(
            run_ref=args.urn,
            hello=args.hello,
            disposition=outcome.disposition,
            mismatched_fields=outcome.mismatched_fields,
            actor=args.actor,
            recorded_at=now,
        )
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=f"{_HELLO_KEY_PREFIX}{args.hello.hello_sequence}",
                status=outcome.disposition.value,
                recorded_at=now,
                payload=fact.model_dump(mode="json"),
            ),
        )
    logger.info(
        f"_hello run={args.urn.entity_key!r} disposition={outcome.disposition.value} "
        f"hello_sequence={args.hello.hello_sequence}"
    )
    return _hello_answer(fact, reason=outcome.reason)


def _standing_hello(
    facts: tuple[WorkerHelloFact, ...], hello_sequence: int
) -> WorkerHelloFact | None:
    """Return the fact a retry of this announcement is already answered by."""
    for fact in facts:
        if fact.hello.hello_sequence == hello_sequence:
            return fact
    return None


def _hello_answer(fact: WorkerHelloFact, *, reason: str | None = None) -> WorkerHelloAnswer:
    """Build the answer the handshake verb returns for one recorded fact."""
    accepted = fact.disposition is HandshakeDisposition.ACCEPTED
    return WorkerHelloAnswer(
        fact=fact.model_dump(mode="json"),
        disposition=fact.disposition,
        mismatched_fields=fact.mismatched_fields,
        refusal_code=None if accepted else RUNTIME_HANDSHAKE_MISMATCH,
        reason=reason
        if reason is not None
        else f"this Run's announcement {fact.hello.hello_sequence} is already recorded",
    )


def _grant(context: Epoch2RootContext, args: _ToolGrantParams) -> ToolGrantAnswer:
    """Decide which semantic tools this Run's handshake has earned."""
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        facts = hello_facts_of(records, args.urn)
    decision = decide_tool_grant(requested=args.tools, facts=facts)
    return ToolGrantAnswer(
        granted=decision.granted, refusal_code=decision.refusal_code, reason=decision.reason
    )


def _append_event(
    context: Epoch2RootContext, args: RunEventAppend, *, now: datetime
) -> RunEventAnswer:
    """Order one observed event into the Run's stream.

    Raises:
        DaemonValidationError: The Run holds no record; the event kind
            carries no payload model; an event identity is repeated with
            different content; or the sequence falls inside a recorded
            gap, which the replay path negotiates rather than this one.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        run = _stored_run(session, records, args.urn)
        status = reduce_run_control(
            status=run.status, facts=_control_facts(records, args.urn)
        ).status
        events = run_events_of(records, args.urn)
        try:
            plan = plan_event_append(
                request=args,
                events=events,
                terminal=status in TERMINAL_RUN_STATUSES,
                gap_ref=_mint_gap_ref(),
                now=now,
            )
        except ValueError as error:
            raise DaemonValidationError(
                f"validation_failed: illegal_transition: {error}"
            ) from error
        for line in plan.lines:
            _append_event_line(session, line, now=now)
    state = reduce_run_events((*events, *plan.lines))
    return RunEventAnswer(
        event=plan.event.model_dump(mode="json"),
        disposition=plan.disposition,
        run_status=status,
        last_contiguous_sequence=state.last_contiguous_sequence,
        next_sequence=state.next_sequence,
        gap=None if plan.gap is None else plan.gap.model_dump(mode="json"),
        reason=plan.reason,
    )


def _read_events(
    context: Epoch2RootContext, args: RunEventsRead, *, now: datetime
) -> RunEventsAnswer:
    """Report one Run's ordered stream, its holes, and its liveness."""
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        run = _stored_run(session, records, args.urn)
        status = reduce_run_control(
            status=run.status, facts=_control_facts(records, args.urn)
        ).status
        events = run_events_of(records, args.urn)
    state = reduce_run_events(events)
    stall = assess_stall(state=state, now=now, interval_seconds=args.stall_interval_seconds)
    derived = sorted(
        (event for event in events if event.quarantine is None),
        key=lambda item: item.run_sequence,
    )
    return RunEventsAnswer(
        events=tuple(event.model_dump(mode="json") for event in derived),
        quarantined=tuple(event.model_dump(mode="json") for event in state.quarantined),
        gaps=tuple(gap.model_dump(mode="json") for gap in state.gaps),
        last_contiguous_sequence=state.last_contiguous_sequence,
        next_sequence=state.next_sequence,
        derivation_stopped=state.derivation_stopped,
        run_status=status,
        stall=RunStallAnswer(
            verdict=stall.verdict,
            last_activity_at=(
                None if stall.last_activity_at is None else stall.last_activity_at.isoformat()
            ),
            last_activity_kind=stall.last_activity_kind,
            elapsed_seconds=stall.elapsed_seconds,
            interval_seconds=stall.interval_seconds,
            resume_method=RUN_CONTROL_REQUEST_METHOD,
            resume_control=stall.resume_control,
        ),
    )


def _contract(context: Epoch2RootContext, args: _RunParams) -> RunContract:
    """Rebuild one Run's contract from the document and the run ledger.

    Raises:
        DaemonValidationError: The Run has no contract binding, so the
            digests it ran under were never recorded and reporting any
            would be a fabrication.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        binding = _binding_of(records, args.urn)
        if binding is None:
            raise DaemonValidationError(
                f"validation_failed: identity_not_found: run {args.urn.entity_key!r} has no "
                "contract binding, so its compiled spec and authority capsule are unrecorded"
            )
        run = _stored_run(session, records, args.urn)
        facts = _control_facts(records, args.urn)
    return reconstruct_run_contract(run=run, binding=binding, facts=facts)


@native_mutator(RUN_BIND_METHOD)
async def _bind_run(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record the compiled-spec and capsule digests one Run was dispatched under."""
    args = _params(_BindParams, params)
    context = ctx.native_root_context(authority.root)
    return await asyncio.to_thread(_bind, context, args, now=datetime.now(UTC))


@native_mutator(RUN_CONTROL_REQUEST_METHOD)
async def _request_control(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Append the requested fact of one control; the Run status is untouched."""
    args = _params(_RequestParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_request, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(RUN_CONTROL_ACKNOWLEDGE_METHOD)
async def _acknowledge_control(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Append the acknowledged fact, granting or superseding the control lease."""
    args = _params(_AcknowledgeParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_acknowledge, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(RUN_CONTROL_EFFECT_METHOD)
async def _effect_control(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Append the effected fact and commit the transition it makes a fact.

    A retry the transaction answered from its receipt store carries no
    envelope, and nothing is published for it: the original commit
    already published, and publishing again would replay one mutation's
    event on every retry.
    """
    args = _params(_EffectParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        outcome = await asyncio.to_thread(_effect, context, args, now=datetime.now(UTC))
    except TransactionRefusedError as refusal:
        logger.info(f"_effect_control refused code={refusal.code.value}")
        raise DaemonValidationError(
            f"validation_failed: {refusal.code.value}: {refusal.detail}"
        ) from refusal
    answer = outcome.answer
    if outcome.envelope is not None and not publish_projection(ctx.bus, outcome.envelope):
        answer = answer.model_copy(update={"warnings": (PROJECTION_DEGRADED,)})
    return answer.model_dump(mode="json")


@register(RUN_CONTRACT_READ_METHOD)
async def _read_run_contract(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one Run's whole contract from durable records alone."""
    authority = require_native_call(ctx, params)
    args = _params(_RunParams, params)
    context = ctx.native_root_context(authority.root)
    contract = await asyncio.to_thread(_contract, context, args)
    return contract.model_dump(mode="json")


@native_mutator(RUN_WORKER_HELLO_METHOD)
async def _announce_worker(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record a worker's announcement and the daemon's decision about it."""
    args = _params(_HelloParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_hello, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@register(RUN_TOOLS_GRANT_METHOD)
async def _grant_tools(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Open this Run's semantic tools, or say which handshake fact refused them."""
    authority = require_native_call(ctx, params)
    args = _params(_ToolGrantParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_grant, context, args)
    return answer.model_dump(mode="json")


@native_mutator(RUN_EVENT_APPEND_METHOD)
async def _append_run_event(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Order one observed event into the Run's stream.

    The append writes ledger lines and commits no canonical transition:
    an observation of a Run is not a change to one, and a late event that
    could move a status is exactly the thing the quarantine exists to
    stop. Nothing is published for it either, because nothing moved.
    """
    args = _params(RunEventAppend, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_append_event, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@register(RUN_EVENTS_READ_METHOD)
async def _read_run_events(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Report one Run's ordered stream, its recorded holes and its liveness."""
    authority = require_native_call(ctx, params)
    args = _params(RunEventsRead, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_read_events, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(RUN_DISPATCH_METHOD)
async def _dispatch_run(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Compile, lease, spawn and accept the announcement, in that order.

    The driver is awaited rather than threaded: it has to await the
    launcher, and each of its locked passes over the tree is opened and
    closed around that await rather than held across it.
    """
    args = _params(DispatchParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await dispatch_run(context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(RUN_RETRY_METHOD)
async def _retry_run(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Decide whether a retry keeps this Run or links a new one to it."""
    args = _params(RetryParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(retry_run, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "RUN_BIND_METHOD",
    "RUN_CONTRACT_READ_METHOD",
    "RUN_CONTROL_ACKNOWLEDGE_METHOD",
    "RUN_CONTROL_EFFECT_METHOD",
    "RUN_CONTROL_REQUEST_METHOD",
    "RUN_EVENTS_READ_METHOD",
    "RUN_EVENT_APPEND_METHOD",
    "RUN_TOOLS_GRANT_METHOD",
    "RUN_WORKER_HELLO_METHOD",
    "RunControlAnswer",
    "RunEventAnswer",
    "RunEventsAnswer",
    "RunStallAnswer",
    "ToolGrantAnswer",
    "WorkerHelloAnswer",
]
