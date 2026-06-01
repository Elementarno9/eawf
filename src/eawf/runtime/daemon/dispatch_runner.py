"""Daemon-side dispatch runner — emits C09 dispatch event payloads.

The runner is the daemon-internal component that drives a single wave
dispatch attempt and emits the C09 typed ``EventPayload`` sub-classes
(``runtime_switched`` on a V5 fallback, ``dispatch_cost`` post-dispatch)
defined in :mod:`eawf.kernel.store.kinds.events`.

Every event the runner produces is routed through the **daemon canonical
writer** for ``event.jsonl`` — :func:`eawf.kernel.store.append.append_envelope`
under the per-file portalock + fsync — and then published to the
subscription bus via :meth:`eawf.runtime.daemon.bus.EventBus.publish`. This
mirrors the :func:`eawf.runtime.daemon.methods.state.mutate` persistence path so
subscribers cannot tell a dispatch-runner event apart from a mutator
event: both converge on the same on-disk row. The runner never opens
``event.jsonl`` directly nor calls ``atomic_write_json`` — persistence
authority for the event store stays with the canonical append helper
(per the daemon-as-sole-mutator rule).

The typed payload is validated through :data:`C09EventPayloadUnion`
*before* it is folded into the generic
:class:`eawf.kernel.store.envelope.Envelope` ``payload`` dict, so a payload
whose body does not match its ``event_type`` discriminator fails fast
with :class:`pydantic.ValidationError` at emit time rather than at
projection time (the §5.11 discriminator-emit invariant).

On dispatch completion the runner also emits a typed ``agent_end``
executor report through the canonical agent-report writer
:func:`eawf.workflow.agent_report.store.append_agent_report` (the same writer the
operator-facing ``eawf hook event`` AGENT_END path uses). The report
uses the dispatched wave's executor :class:`~eawf.kernel.state.models.AgentSession`
as authority — role, scope, attempt, and store kind are derived from the
session — so the persisted row passes
:func:`eawf.kernel.validate.invariants.check_agent_report_invariants`. Report
emission is opt-in: it fires only when the caller supplies the
``session_id`` of the executor session AND the daemon context is wired to
an on-disk ``state.json`` (plan-only and stateless unit-test contexts
skip it).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.io import state_version
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    AuditorReportBody,
    DomainSpecialistReportBody,
    ExecutorReportBody,
    OperatorReportBody,
    PlannerReportBody,
    PolisherReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
)
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.events import (
    C09EventPayloadUnion,
    DispatchCostPayload,
    RuntimeSwitchedPayload,
)
from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.runtime.budget.policy import DEFAULT_ENFORCE, DEFAULT_MULTIPLIER
from eawf.runtime.budget.service import record_consumption
from eawf.runtime.daemon.budget_interlock import enforce_token_cap
from eawf.runtime.lock import portalock
from eawf.workflow.agent_report.store import append_agent_report
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle.wave import start_wave
from eawf.workflow.verify.dispatch_close import (
    DispatchCloseBlockedError,
    verify_close_readiness,
)

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods import MethodContext

logger = logging.getLogger(__name__)

#: Adapter used to validate a typed payload through the discriminated
#: union before it is folded into the envelope ``payload`` dict. Building
#: the adapter once at import keeps the per-emit cost to a dict round-trip.
_PAYLOAD_ADAPTER: TypeAdapter[Any] = TypeAdapter(C09EventPayloadUnion)


@dataclass(frozen=True)
class DispatchTokens:
    """Per-invocation token tally returned by a runtime dispatch.

    Attributes:
        input_tokens: Non-cached input tokens billed.
        output_tokens: Output tokens billed.
        cache_creation_input_tokens: Tokens written to the prompt cache.
        cache_read_input_tokens: Tokens served from the prompt cache.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @property
    def total(self) -> int:
        """Return the sum of every billed token field for this dispatch.

        The live burn gauge tracks ``Wave.tokens_consumed`` as a single
        scalar, so the per-invocation accrual folds all four billed
        tallies (non-cached input, output, cache-creation, cache-read)
        into one delta.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of :func:`run_dispatch`.

    Attributes:
        runtime: Runtime that ultimately served the dispatch.
        attempt_id: Dispatch-attempt id of the serving attempt.
        switched: ``True`` when a V5 fallback fired and the dispatch
            switched runtimes mid-flight.
        event_ids: Ids of the C09 *event*-store envelopes the runner
            emitted, in append order (``runtime_switched`` first when a
            fallback fired, then ``dispatch_cost``).
        report_id: Envelope id of the typed ``agent_end`` executor report
            persisted on dispatch completion, or ``None`` when the caller
            supplied no executor ``session_id`` (or the daemon context
            carries no ``state.json``) and report emission was skipped.
            The report lands in the role-specific ``executor_report``
            store, not the event store, so it is intentionally kept off
            :attr:`event_ids`.
    """

    runtime: RuntimeTriple
    attempt_id: str
    switched: bool
    event_ids: tuple[str, ...]
    report_id: str | None = None


def _event_path(ctx: MethodContext) -> Path:
    """Return the ``event.jsonl`` path the runner appends through.

    Args:
        ctx: Daemon method context.

    Returns:
        The configured ``event.jsonl`` path.

    Raises:
        RuntimeError: When ``ctx.event_path`` is not configured (the
            runner cannot route through the canonical writer without it).
    """
    if ctx.event_path is None:
        raise RuntimeError("event_path not configured on daemon context")
    return Path(ctx.event_path)


def _emit(
    ctx: MethodContext,
    payload: TracedEventPayload,
    *,
    scope_id: str | None,
    summary: str,
) -> str:
    """Persist *payload* as a ``StoreKind.EVENT`` envelope via the canonical writer.

    Validates *payload* through :data:`C09EventPayloadUnion` (fail-fast on
    a discriminator/body mismatch), wraps the validated body in the generic
    :class:`Envelope`, appends it through
    :func:`eawf.kernel.store.append.append_envelope` (the canonical event-store
    writer), then publishes to the subscription bus when one is attached.

    Args:
        ctx: Daemon method context — supplies ``event_path`` + ``bus``.
        payload: C09 typed event payload to persist.
        scope_id: Scope id stamped on the envelope (typically a wave id).
        summary: One-line human-readable envelope summary.

    Returns:
        The id of the appended envelope.

    Raises:
        RuntimeError: When ``ctx.event_path`` is not configured.
        pydantic.ValidationError: When *payload* fails discriminated-union
            validation (body does not match its ``event_type`` tag).
    """
    event_path = _event_path(ctx)
    body = _PAYLOAD_ADAPTER.validate_python(payload).model_dump(mode="json")
    now = datetime.now(UTC)
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=body,
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(event_path, envelope)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id
    logger.info(
        f"emit event_type={body['event_type']!r} scope={scope_id!r} envelope_id={envelope.id!r}"
    )
    return envelope.id


def emit_runtime_switched(
    ctx: MethodContext,
    *,
    wave_id: str,
    attempt_id_from: str,
    attempt_id_to: str,
    runtime_from: RuntimeTriple,
    runtime_to: RuntimeTriple,
    cause: RuntimeErrorClass,
    error_detail: str,
    idempotency_key: str,
    trace_request_id: str | None = None,
) -> str:
    """Emit a ``runtime_switched`` event for a V5 runtime fallback.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave whose dispatch switched runtimes.
        attempt_id_from: Attempt id that failed / was superseded.
        attempt_id_to: Attempt id minted for the replacement runtime.
        runtime_from: Runtime switched away from.
        runtime_to: Runtime switched to.
        cause: Typed :class:`~eawf.observability.telemetry.models.RuntimeErrorClass`
            member that triggered the switch (scrubbed).
        error_detail: Scrubbed stderr / failure detail for diagnosis.
        idempotency_key: De-dup key so a retried emit does not
            double-count the switchover.
        trace_request_id: Optional daemon RPC request id for the §5.8
            correlation chain.

    Returns:
        The id of the appended envelope.
    """
    payload = RuntimeSwitchedPayload(
        timestamp=datetime.now(UTC),
        wave_id=wave_id,
        attempt_id_from=attempt_id_from,
        attempt_id_to=attempt_id_to,
        runtime_from=runtime_from,
        runtime_to=runtime_to,
        cause=cause,
        error_detail=error_detail,
        idempotency_key=idempotency_key,
        trace_request_id=trace_request_id,
        trace_wave_id=wave_id,
        trace_attempt_id=attempt_id_to,
    )
    summary = f"runtime_switched wave={wave_id} {runtime_from}->{runtime_to} cause={cause}"
    return _emit(ctx, payload, scope_id=wave_id, summary=summary)


def emit_dispatch_cost(
    ctx: MethodContext,
    *,
    wave_id: str | None,
    attempt_id: str | None,
    runtime: RuntimeTriple,
    model: str,
    tokens: DispatchTokens,
    cost_usd: Decimal,
    pricing_version: str,
    trace_request_id: str | None = None,
) -> str:
    """Emit a ``dispatch_cost`` event after a dispatch attempt completes.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave the dispatch served, or ``None`` for an
            interactive (non-wave) CLI session.
        attempt_id: Attempt id of the serving attempt, or ``None`` for an
            interactive session.
        runtime: Runtime that incurred the cost.
        model: Model identifier the cost is priced against.
        tokens: Per-invocation token tally.
        cost_usd: Priced cost in USD.
        pricing_version: ``PRICING`` snapshot version used to compute
            ``cost_usd``.
        trace_request_id: Optional daemon RPC request id for the §5.8
            correlation chain.

    Returns:
        The id of the appended envelope.
    """
    payload = DispatchCostPayload(
        timestamp=datetime.now(UTC),
        wave_id=wave_id,
        attempt_id=attempt_id,
        runtime=runtime,
        model=model,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_creation_input_tokens=tokens.cache_creation_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
        cost_usd=cost_usd,
        pricing_version=pricing_version,
        trace_request_id=trace_request_id,
        trace_wave_id=wave_id,
        trace_attempt_id=attempt_id,
    )
    summary = f"dispatch_cost wave={wave_id} runtime={runtime} cost_usd={cost_usd}"
    return _emit(ctx, payload, scope_id=wave_id, summary=summary)


def _publish_state_revision(
    ctx: MethodContext,
    *,
    wave_id: str,
    before_version: str,
    after_version: str,
    tokens_consumed: int,
) -> None:
    """Publish a ``state_mutated`` revision envelope on the subscription bus.

    The accrual writes ``state.json`` directly under the runner's
    portalock (the daemon-internal canonical-writer path), so the
    mtime-poll STATE_REVISION feed advances on the next tick. This helper
    drives the **daemon-push** STATE_REVISION feed: it wakes live
    subscribers (the TUI burn gauge) immediately with a canonical
    ``state_mutated`` event envelope carrying the before/after state
    digests, mirroring the envelope :func:`eawf.runtime.daemon.methods.state.mutate`
    publishes so subscribers cannot tell the two paths apart.

    A bus-less context (stateless unit-test paths) is a no-op.

    Args:
        ctx: Daemon method context — supplies ``bus``.
        wave_id: ``W<NN>`` wave whose token burn advanced.
        before_version: State digest before the accrual write.
        after_version: State digest after the accrual write.
        tokens_consumed: The wave's post-accrual cumulative
            ``tokens_consumed`` value (carried on the envelope summary).
    """
    if ctx.bus is None or not hasattr(ctx.bus, "publish"):
        return
    now = datetime.now(UTC)
    summary = f"state_mutated wave={wave_id} tokens_consumed={tokens_consumed}"
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.wave_accrue_tokens",
        event_kind="state_mutated",
        actor="daemon",
        command="dispatch_runner.accrue_tokens_consumed",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def accrue_tokens_consumed(
    ctx: MethodContext,
    *,
    wave_id: str,
    tokens: DispatchTokens,
) -> bool:
    """Fold a dispatch's token tally into ``Wave.tokens_consumed``, live.

    Called once per ``dispatch_cost`` event so the live burn gauge on the
    TUI advances **during** execution rather than only on the manual
    ``eawf wave budget consume`` CLI. The increment runs under the same
    defense-in-depth ``portalock(state.json)`` + locked-atomic-write the
    daemon's ``state.mutate`` path uses (per the daemon-as-sole-mutator
    rule), and delegates to :func:`eawf.runtime.budget.service.record_consumption`
    so the accrual reuses the canonical consume semantics rather than a
    raw field edit.

    The atomic state write bumps the ``state.json`` mtime (the mtime-poll
    STATE_REVISION feed), and :func:`_publish_state_revision` wakes live
    subscribers on the bus (the daemon-push STATE_REVISION feed) so both
    revision feeds advance.

    After the accrual persists, the safety-floor token-cap interlock
    (:func:`eawf.runtime.daemon.budget_interlock.enforce_token_cap`) classifies
    the post-increment burn against the wave's budget and, under ``hard``
    enforce at the cap, reaps the wave's spawned process group via the kill
    ladder. The enforce mode + multiplier default to the documented config
    defaults (``soft`` / ``1.5``); ``soft`` never reaches HALT so the
    interlock is a no-op on the hot path under the default mode. The pgid is
    ``None`` today (the mutating dispatch spawn is dark -- no live pid
    registry yet, C6); a hard-cap breach is computed and logged but not
    signalled until I04-W03 threads the real pgid through.

    The accrual is opt-in and tolerant: it is skipped (returning
    ``False``) when ``ctx.state_path`` is unset (stateless unit-test
    contexts). The total delta is the sum of every billed token field
    (:attr:`DispatchTokens.total`); a zero delta still rewrites the state
    (bumping the revision) but leaves the counter unchanged.

    Args:
        ctx: Daemon method context — supplies ``state_path`` + ``bus``.
        wave_id: ``W<NN>`` wave the dispatch served.
        tokens: Per-invocation token tally from the dispatch.

    Returns:
        ``True`` when the accrual was persisted; ``False`` when it was
        skipped (no ``state_path``).

    Raises:
        KeyError: When *wave_id* is absent from ``state.json`` — the
            dispatch served a wave that no longer exists in state, which
            is a fail-fast inconsistency the caller must surface.
    """
    if ctx.state_path is None:
        return False
    delta = tokens.total
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        before_version = state_version(state.model_dump(mode="json"))
        wave, _tag = record_consumption(state, wave_id, delta)
        state.updated_at = datetime.now(UTC)
        new_payload = state.model_dump(mode="json")
        after_version = state_version(new_payload)
        atomic_write_json_locked(state_path, new_payload)
        tokens_consumed = wave.tokens_consumed
        token_budget = wave.token_budget
    _publish_state_revision(
        ctx,
        wave_id=wave_id,
        before_version=before_version,
        after_version=after_version,
        tokens_consumed=tokens_consumed,
    )
    # Safety-floor token-cap interlock: classify the post-increment burn
    # against the wave's budget and, on a hard-enforce HALT, reap the wave's
    # spawned process group. Run outside the state portalock so the kill
    # ladder never signals while holding the state lock. The enforce mode +
    # multiplier are the documented real defaults (soft never reaches HALT,
    # so this stays a no-op on the hot path under the default mode);
    # config-resolved enforce mode lands with the live-dispatch pgid feed
    # (I04-W03). pgid is None today because the mutating dispatch spawn is
    # dark (no live pid registry yet -- C6): a hard-cap breach is computed
    # and loudly logged but not signalled until I04-W03 threads the pgid.
    enforce_token_cap(
        consumed=tokens_consumed,
        base_budget=token_budget,
        enforce=DEFAULT_ENFORCE,
        multiplier=DEFAULT_MULTIPLIER,
        pgid=None,
    )
    logger.info(f"accrue_tokens_consumed wave={wave_id} delta={delta} consumed={tokens_consumed}")
    return True


def _completion_verdict(*, switched: bool) -> AgentReportVerdict:
    """Derive the executor report verdict from the dispatch outcome.

    A clean dispatch (no runtime switch) completes as ``pass``. A V5
    fallback means the primary runtime failed and a switch served the
    dispatch instead; the work still landed, so the verdict is
    ``pass-with-followups`` to flag the switchover for review rather than
    a hard ``fail``.

    Args:
        switched: ``True`` when a V5 fallback fired during the dispatch.

    Returns:
        The derived :class:`~eawf.kernel.state.enums.AgentReportVerdict`.
    """
    if switched:
        return AgentReportVerdict.PASS_WITH_FOLLOWUPS
    return AgentReportVerdict.PASS


def _build_completion_body(
    *,
    role: AgentSessionRole,
    wave_id: str,
    commit_sha: str,
    outcome: str,
    files_changed: list[str] | None,
    tests_run: list[str] | None,
    verdict: AgentReportVerdict,
    confidence: Confidence,
) -> AgentReportBody:
    """Return the typed completion body for a dispatched session *role*.

    Routes by the session's role so each role's dispatch lands a body
    of its own type — an auditor session lands an :class:`AuditorReportBody`,
    a reviewer lands a :class:`ReviewerReportBody`, and so on. The
    executor path keeps the rich body (``wave_id`` + ``commit_sha`` +
    ``files_changed`` + ``tests_run`` + ``outcome``); the other seven
    roles build a minimal completion body keyed by the role's
    required field (``target_id`` for auditor/reviewer, ``scope_id``
    for polisher, ``phase_id`` for operator, ``question`` /
    ``recommendation`` for researcher, ``objective`` for planner,
    ``domain`` / ``assessment`` for domain-specialist).

    The minimal-body strategy: use *wave_id* as the role-required id
    field and *outcome* as the role-required prose field, mirroring
    the dispatch runner's pre-W13 executor-only surface so the
    completion contract stays uniform across roles. Future waves
    under I02/I03 wire the rich per-role validators (criteria,
    coverage_refs, etc.); this seam opens the kind routing today
    without pre-building those validators.

    Args:
        role: The session role driving the body type selection.
        wave_id: Wave id used as the role-required id field for
            non-executor roles AND as the executor body's ``wave_id``.
        commit_sha: Executor commit SHA (used only when role is
            :attr:`AgentSessionRole.EXECUTOR`).
        outcome: One-line completion outcome (used by every body).
        files_changed: Repo-relative paths (executor only).
        tests_run: Test commands (executor only).
        verdict: Resolved report verdict.
        confidence: Report confidence.

    Returns:
        A typed :class:`AgentReportBody` discriminated-union member
        matching *role*.

    Raises:
        KeyError: When *role* has no mapped body class (cannot happen
            for a valid :class:`AgentSessionRole`).
    """
    if role is AgentSessionRole.EXECUTOR:
        return ExecutorReportBody(
            verdict=verdict,
            confidence=confidence,
            summary=outcome,
            wave_id=wave_id,
            files_changed=list(files_changed or []),
            tests_run=list(tests_run or []),
            commit_sha=commit_sha,
            outcome=outcome,
        )
    if role is AgentSessionRole.AUDITOR:
        return AuditorReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, target_id=wave_id
        )
    if role is AgentSessionRole.REVIEWER:
        return ReviewerReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, target_id=wave_id
        )
    if role is AgentSessionRole.POLISHER:
        return PolisherReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, scope_id=wave_id
        )
    if role is AgentSessionRole.OPERATOR:
        return OperatorReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, phase_id=wave_id
        )
    if role is AgentSessionRole.RESEARCHER:
        return ResearcherReportBody(
            verdict=verdict,
            confidence=confidence,
            summary=outcome,
            question=outcome,
            recommendation=outcome,
        )
    if role is AgentSessionRole.PLANNER:
        return PlannerReportBody(
            verdict=verdict, confidence=confidence, summary=outcome, objective=outcome
        )
    if role is AgentSessionRole.DOMAIN_SPECIALIST:
        return DomainSpecialistReportBody(
            verdict=verdict,
            confidence=confidence,
            summary=outcome,
            domain=wave_id,
            assessment=outcome,
        )
    raise KeyError(f"no completion body builder for role: {role.value!r}")


def emit_agent_end_report(
    ctx: MethodContext,
    *,
    session_id: str,
    wave_id: str,
    commit_sha: str,
    outcome: str,
    files_changed: list[str] | None = None,
    tests_run: list[str] | None = None,
    runtime: RuntimeTriple,
    verdict: AgentReportVerdict | None = None,
    confidence: Confidence = Confidence.HIGH,
    switched: bool = False,
) -> str:
    """Emit a typed ``agent_end`` report on dispatch completion.

    Builds the role-appropriate
    :class:`~eawf.kernel.store.kinds.agent_report.AgentReportCommonBody`
    subclass for the dispatched session and persists it through the
    canonical agent-report writer
    :func:`eawf.workflow.agent_report.store.append_agent_report`, using
    the :class:`~eawf.kernel.state.models.AgentSession` named by
    *session_id* as authority. The session's role drives both the body
    type (via :func:`_build_completion_body`) and the destination
    :class:`~eawf.kernel.state.enums.StoreKind` (via the writer's call
    to :func:`~eawf.kernel.store.kinds.agent_report.store_kind_for_role`),
    so the persisted envelope passes
    :func:`eawf.kernel.validate.invariants.check_agent_report_invariants`
    and lands in the per-role store (``auditor_report.jsonl``,
    ``reviewer_report.jsonl``, ...) rather than always
    ``executor_report.jsonl``.

    The dispatched *wave_id* is used as the report ``base_id`` so retried
    dispatches for the same wave append monotonic attempts under one
    ``(role, base_id)`` series.

    The session's :attr:`~eawf.kernel.state.models.AgentSession.agent_principal_id`
    (a v0.3-v0.5 placeholder for the per-repo Principal id of the agent
    that drove the dispatch) is copied by the canonical agent-report
    writer onto the persisted
    :attr:`AgentReportHeader.agent_principal_id` and logged here so the
    dispatch trace records the Principal binding for the served
    attempt.

    Args:
        ctx: Daemon method context — supplies ``state_path``.
        session_id: Id of the session that ran the dispatch; must
            exist in ``state.json``. The session's role decides the
            body type + destination store kind.
        wave_id: ``W<NN>`` wave the dispatch served. Must exist in
            ``state.waves`` for the role's wave-presence invariant
            (executor-wave / auditor-target / reviewer-target / etc.).
        commit_sha: Commit the session landed; required for the
            executor path so the executor-commit invariant holds.
            Non-executor roles ignore the value.
        outcome: One-line implementation outcome for the report body.
        files_changed: Repo-relative paths the dispatch changed
            (executor only).
        tests_run: Test commands the dispatch executed (executor only).
        runtime: Runtime that served the dispatch, recorded on the
            report header.
        verdict: Report verdict; derived from *switched* when ``None``.
        confidence: Report confidence (defaults to ``high``).
        switched: ``True`` when a V5 fallback fired; feeds the derived
            verdict when *verdict* is ``None``.

    Returns:
        The id of the appended role-specific report envelope.

    Raises:
        RuntimeError: When ``ctx.state_path`` is not configured (the
            writer needs state to resolve the session authority).
        KeyError: When *session_id* is absent from ``state.json``.
        eawf.workflow.agent_report.store.AgentReportRoleMismatchError: When the
            session role disagrees with the constructed body's role
            (cannot happen on this path — :func:`_build_completion_body`
            keys off the session role).
        eawf.workflow.agent_report.store.AgentReportScrubError: When the report
            body text contains local or sensitive tokens.
        DispatchCloseBlockedError: When the post-execution verify gate
            (:func:`~eawf.workflow.verify.dispatch_close.verify_close_readiness`)
            refuses the report — e.g. a ``FAIL`` / ``BLOCKED`` verdict
            or an executor body whose ``wave_id`` disagrees with the
            dispatched wave. The report has already been persisted at
            this point; the raise prevents the close path from
            advancing on an unverified attempt.
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    state = load_state(state_path)
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise KeyError(f"unknown agent session: {session_id!r}")
    resolved_verdict = verdict if verdict is not None else _completion_verdict(switched=switched)
    body = _build_completion_body(
        role=session.role,
        wave_id=wave_id,
        commit_sha=commit_sha,
        outcome=outcome,
        files_changed=files_changed,
        tests_run=tests_run,
        verdict=resolved_verdict,
        confidence=confidence,
    )
    result = append_agent_report(
        state=state,
        state_path=state_path,
        session_id=session_id,
        base_id=wave_id,
        body=body,
        runtime=runtime,
    )
    # Surface the Principal-linked identity of the dispatched session
    # alongside the runtime id. The canonical writer has already copied
    # this value into ``AgentReportHeader.agent_principal_id``.
    agent_principal_id = session.agent_principal_id
    logger.info(
        f"emit_agent_end_report wave={wave_id} session={session_id!r} "
        f"role={session.role.value} verdict={resolved_verdict.value} "
        f"store_kind={result.store_kind} report_id={result.envelope.id!r} "
        f"agent_principal_id={agent_principal_id!r}"
    )
    # Post-execution verify gate: the report is persisted (so the failed
    # attempt is recorded on disk), then the runner refuses to advance
    # the close path when the gate fires. A clean pass returns the
    # report id; a blocked close raises with the structured reasons.
    verify_result = verify_close_readiness(wave_id, body)
    if not verify_result.passed:
        raise DispatchCloseBlockedError(wave_id=wave_id, result=verify_result)
    return result.envelope.id


def _mark_wave_in_progress(ctx: MethodContext, *, wave_id: str) -> bool:
    """Flip *wave_id* CLAIMED -> IN_PROGRESS through the daemon canonical writer.

    Called at the head of :func:`run_dispatch` so a dispatched wave's
    ``state.json`` status reflects that the claim has been picked up and
    implementation has begun. The mutation runs under the same defense-in-
    depth ``portalock(state.json)`` + locked-atomic-write the daemon's
    ``state.mutate`` path uses (per the daemon-as-sole-mutator rule), and
    applies the pure-functional :func:`eawf.workflow.lifecycle.wave.start_wave`
    transition so the inline-start and dispatched-start paths converge on
    one transition.

    The flip is opt-in and tolerant: it is skipped (returning ``False``)
    when ``ctx.state_path`` is unset (stateless unit-test contexts) or the
    wave is absent from state. A wave that is already IN_PROGRESS is a
    no-op via ``start_wave``'s own idempotency. A wave in any other status
    (PENDING / terminal) is left untouched — the claim-gate upstream owns
    that precondition, so the runner does not abort a dispatch over it.

    Args:
        ctx: Daemon method context — supplies ``state_path``.
        wave_id: ``W<NN>`` wave being dispatched.

    Returns:
        ``True`` when the wave was flipped to (or already at) IN_PROGRESS
        and the new status was persisted; ``False`` when the flip was
        skipped (no ``state_path``, unknown wave, or a non-claimable
        status).
    """
    if ctx.state_path is None:
        return False
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        wave = state.waves.get(wave_id)
        if wave is None:
            logger.debug(f"_mark_wave_in_progress skip wave={wave_id!r} not in state")
            return False
        if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
            logger.debug(
                f"_mark_wave_in_progress skip wave={wave_id!r} status={wave.status.value!r}"
            )
            return False
        start_wave(state, wave_id=wave_id)
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(f"_mark_wave_in_progress wave={wave_id}")
    return True


def run_dispatch(
    ctx: MethodContext,
    *,
    wave_id: str,
    primary_runtime: RuntimeTriple,
    fallback_runtime: RuntimeTriple,
    model: str,
    pricing_version: str,
    primary_error: RuntimeErrorClass | None,
    tokens: DispatchTokens,
    cost_usd: Decimal,
    trace_request_id: str | None = None,
    session_id: str | None = None,
    commit_sha: str | None = None,
    outcome: str | None = None,
    files_changed: list[str] | None = None,
    tests_run: list[str] | None = None,
    verdict: AgentReportVerdict | None = None,
    confidence: Confidence = Confidence.HIGH,
) -> DispatchResult:
    """Drive one wave dispatch attempt, emitting the C09 dispatch events.

    The runner mints a fresh attempt id for the primary runtime. When
    *primary_error* is set, it simulates a V5 fallback: a fresh attempt id
    is minted for *fallback_runtime*, a ``runtime_switched`` event is
    emitted through the canonical writer, and the fallback runtime serves
    the dispatch. Once the serving attempt completes, a ``dispatch_cost``
    event is emitted with the token tally + priced cost, and the tally is
    folded into ``Wave.tokens_consumed`` via :func:`accrue_tokens_consumed`
    (triggering a STATE_REVISION) so the live burn gauge advances during
    execution.

    On completion the runner also emits a typed ``agent_end`` executor
    report through :func:`emit_agent_end_report` when *session_id* is
    supplied (and the daemon context carries a ``state.json``); the
    report's envelope id rides back on :attr:`DispatchResult.report_id`.
    Stateless contexts (no ``session_id`` or no ``state_path``) skip the
    report and leave ``report_id`` ``None``.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave being dispatched.
        primary_runtime: Runtime tried first.
        fallback_runtime: Runtime the V5 ladder falls through to.
        model: Model the serving runtime priced its cost against.
        pricing_version: ``PRICING`` snapshot version pinning the cost.
        primary_error: Typed
            :class:`~eawf.observability.telemetry.models.RuntimeErrorClass` member when
            the primary runtime fails (triggers a V5 fallback), or ``None``
            when the primary serves the dispatch with no switch.
        tokens: Token tally the serving attempt accrued.
        cost_usd: Priced cost in USD for the serving attempt.
        trace_request_id: Optional daemon RPC request id.
        session_id: Id of the executor session that ran the dispatch. When
            supplied (and ``ctx.state_path`` is configured) the runner
            emits the typed ``agent_end`` executor report on completion.
        commit_sha: Commit the executor landed; required for the
            ``agent_end`` report (defaults to the serving attempt id when
            omitted so the executor-commit invariant still holds).
        outcome: One-line implementation outcome for the report body;
            defaults to a generated summary when omitted.
        files_changed: Repo-relative paths the dispatch changed (report).
        tests_run: Test commands the dispatch executed (report).
        verdict: ``agent_end`` report verdict; derived from the fallback
            outcome when ``None``.
        confidence: ``agent_end`` report confidence (defaults to ``high``).

    Returns:
        A :class:`DispatchResult` naming the serving runtime, its attempt
        id, whether a fallback fired, the emitted C09 event ids, and the
        ``agent_end`` report id (``None`` when no report was emitted).

    Raises:
        DispatchCloseBlockedError: Propagated from
            :func:`emit_agent_end_report` when the post-execution
            verify gate refuses the persisted report (W57). The
            failure is fail-fast: the C09 events have already been
            persisted, the token tally has already accrued, and the
            report row is on disk; the raise stops the close path
            from advancing past an unverified attempt.
    """
    # Head transition: a dispatched wave moves CLAIMED -> IN_PROGRESS the
    # moment the runner starts driving it, so the wave's persisted status
    # reflects that implementation is underway before any event/report
    # lands. Skipped when the daemon context carries no state.json
    # (stateless unit-test contexts) or the wave is not in a claimable
    # status (the claim-gate upstream owns that precondition).
    _mark_wave_in_progress(ctx, wave_id=wave_id)

    primary_attempt = uuid.uuid4().hex
    event_ids: list[str] = []
    serving_runtime = primary_runtime
    serving_attempt = primary_attempt
    switched = False

    if primary_error is not None:
        fallback_attempt = uuid.uuid4().hex
        switched = True
        serving_runtime = fallback_runtime
        serving_attempt = fallback_attempt
        event_ids.append(
            emit_runtime_switched(
                ctx,
                wave_id=wave_id,
                attempt_id_from=primary_attempt,
                attempt_id_to=fallback_attempt,
                runtime_from=primary_runtime,
                runtime_to=fallback_runtime,
                cause=primary_error,
                error_detail=f"{primary_runtime} failed: {primary_error}",
                idempotency_key=uuid.uuid4().hex,
                trace_request_id=trace_request_id,
            )
        )

    event_ids.append(
        emit_dispatch_cost(
            ctx,
            wave_id=wave_id,
            attempt_id=serving_attempt,
            runtime=serving_runtime,
            model=model,
            tokens=tokens,
            cost_usd=cost_usd,
            pricing_version=pricing_version,
            trace_request_id=trace_request_id,
        )
    )

    # Fold the dispatch's token tally into Wave.tokens_consumed so the live
    # burn gauge advances during execution. Routes through the daemon
    # canonical state writer (portalock + atomic write) and triggers a
    # STATE_REVISION on both feeds (mtime-poll via the state.json write +
    # daemon-push via the bus). Skipped on a stateless context.
    accrue_tokens_consumed(ctx, wave_id=wave_id, tokens=tokens)

    report_id: str | None = None
    if session_id is not None and ctx.state_path is not None:
        report_id = emit_agent_end_report(
            ctx,
            session_id=session_id,
            wave_id=wave_id,
            commit_sha=commit_sha if commit_sha is not None else serving_attempt,
            outcome=outcome
            if outcome is not None
            else f"dispatch served by {serving_runtime} (switched={switched})",
            files_changed=files_changed,
            tests_run=tests_run,
            runtime=serving_runtime,
            verdict=verdict,
            confidence=confidence,
            switched=switched,
        )

    logger.info(
        f"run_dispatch wave={wave_id} serving_runtime={serving_runtime} "
        f"switched={switched} events={len(event_ids)} report_id={report_id!r}"
    )
    return DispatchResult(
        runtime=serving_runtime,
        attempt_id=serving_attempt,
        switched=switched,
        event_ids=tuple(event_ids),
        report_id=report_id,
    )
