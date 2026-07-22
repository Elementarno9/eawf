"""``agent.*`` JSON-RPC methods: dispatch / session / kill.

# noqa: EAWF010 cohesive agent RPC surface; split deferred until dispatch,
session, kill, and pause/resume eventing stop changing together.

The **skill -> adapter handshake** layers on top of the
**fresh-dispatch** path of ``agent.dispatch``: when a caller supplies a
:class:`~eawf.runtime.runtimes.plugin_manifest.SkillManifest` the dispatcher
runs :func:`eawf.runtime.runtimes.dispatch.resolve_adapter` to pick the
highest-preference runtime that the skill manifest can host *and* that
resolves to a concrete adapter (rejecting an off-manifest ``runtime``
override). The complementary V5 reactive switchover + V8 ``--continue``
fall-through *policy* lives in :mod:`eawf.runtime.runtimes.fallback`.

When the caller supplies a post-dispatch *outcome* (the model + token
tally + priced cost, plus an optional error-driven fallback runtime),
``agent.dispatch`` drives :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch`
so the C09 ``runtime_switched`` (on a V5 fallback) + ``dispatch_cost``
events land in the live ``event.jsonl`` through the daemon canonical
writer. This is the production caller for the dispatch runner. When no
outcome is supplied (or no ``event_path`` is configured, as in unit
tests) the method computes the plan and returns it without emitting
events.

The opt-in **live-spawn** path (``spawn=True``) closes the seam the
hand-fed-outcome path left open: instead of the caller hand-feeding a
metered outcome, the daemon registers a role-matched
:class:`~eawf.kernel.state.models.AgentSession` through the canonical
state writer, renders the dispatch prompt, resolves the runtime adapter,
and ``await``s its :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
(which jails the argv + scrubs the child env -- the safety floor). The
spawn's :class:`~eawf.runtime.runtimes.adapter.SpawnResult` is priced via
:func:`eawf.runtime.runtimes.metering.price_spawn_result`, then
:func:`run_dispatch` is driven with the **registered session id** so its
role-specific ``agent_end`` report emit fires. The returned
:class:`DispatchPlan` carries the real captured ``pid`` and the
registered session id. ``spawn=False`` (default) keeps the plan-only and
hand-fed-outcome paths byte-unchanged.

``agent.session`` is a read-only inspection helper that returns the
typed session table from ``state.json`` for a wave.

``agent.kill`` resolves the live fleet lane for ``(wave_id, attempt)``
off the :class:`~eawf.kernel.state.models.FleetRun` registry and signals
its process group: ``signal="kill"`` delivers SIGKILL (a hard stop),
``signal="halt"`` (or the legacy ``"term"`` alias) delivers SIGTERM (a
graceful stop). A signalled lane transitions to a killed terminal that
deregisters from the registry; a wave with no live lane returns a typed
not-found (``killed=false`` + ``reason``) rather than faking a kill.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import orjson
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from eawf.kernel.config.layered import merge_config, resolve_runtime_tier_models
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AgentSessionStatus,
    Confidence,
    DispatchNote,
    EffortBucket,
    ReportSource,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.io import state_version
from eawf.kernel.state.models import (
    AgentSession,
    DispatchAnnotation,
    RuntimeBaseline,
    RuntimeLatest,
    SessionAttempt,
    State,
    Wave,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    AgentReportEvidenceRef,
    AgentReportFollowup,
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    CriterionVerdict,
    OperatorReportBody,
    ReviewerReportBody,
    body_class_for_role,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.events.base import RuntimeTriple
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.invariants import check_agent_report_invariants
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.observability.telemetry.pricing import PRICING_VERSION
from eawf.platform.scrub.scan import rewrite_text
from eawf.runtime.budget.policy import DEFAULT_ENFORCE, EnforceMode
from eawf.runtime.daemon.dispatch_runner import (
    DispatchResult,
    DispatchTokens,
    _build_completion_body,
    _chunk_should_flush,
    emit_agent_output_chunk,
    run_dispatch,
)
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    note_cross_root_serve,
    register,
)
from eawf.runtime.daemon.methods.fleet import kill_lane
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.adapter import ErrorClass, RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.dispatch import resolve_adapter
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.runtime.runtimes.metering import price_spawn_result
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.runtime.runtimes.selector import select_adapter
from eawf.runtime.sandbox.policy import resolve_denied_tools
from eawf.runtime.session.store import build_event, commit_event, stage_session
from eawf.workflow.dispatch.llm_assist import LLMAssistError, assist_with_schema
from eawf.workflow.dispatch.renderer import render_dispatch_envelope, resolve_role_blocks
from eawf.workflow.dispatch.retry import spawn_with_retry
from eawf.workflow.dispatch.routing import resolve_routing, runtime_model_for_decision
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._claim_session import CLAIM_SESSION_NOT_FOUND
from eawf.workflow.lifecycle._errors import LifecycleError, LifecycleGuardError
from eawf.workflow.lifecycle.wave import claim_wave, validate_claim_session

logger = logging.getLogger(__name__)


class LiveSpawnError(RuntimeError):
    """Raised when a live-spawn dispatch is requested without on-disk stores.

    The live-spawn path registers an :class:`~eawf.kernel.state.models.AgentSession`
    and threads its id into the dispatch runner so the ``agent_end`` report
    emit fires; both steps require the daemon context to carry a ``state.json``
    (session registration + report authority) and an ``event.jsonl`` (the C09
    + session-start event sink). A live spawn requested without them fails
    fast rather than silently degrading to the plan-only path.
    """


@dataclass(frozen=True)
class _LiveClaim:
    """Session/runtime binding committed before a live spawn begins."""

    session_id: str
    runtime: str
    model: str
    role: AgentSessionRole
    scope_id: str
    started_at: datetime


#: Wave statuses that carry no out-edge -- a wave that has reached one of
#: these is done, and a same-wave dispatch still in flight (close-on-behalf
#: raced the spawn) must NOT persist a phantom attempt onto it. The live-spawn
#: persist re-reads ``wave.status`` under the state lock and drops the attempt
#: when it lands here (R3 concurrency fix).
_TERMINAL_WAVE_STATUSES: frozenset[WaveStatus] = frozenset(
    {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}
)


#: Maps a plugin-manifest :data:`~eawf.runtime.runtimes.manifest.RuntimeId`
#: (``"claude-code"`` / ``"codex"`` / ``"opencode"``) to the short
#: :data:`~eawf.kernel.store.kinds.events.base.RuntimeTriple` spelling
#: (``"claude"`` / ``"codex"`` / ``"opencode"``) the C09 event surface
#: keys on. Only ``"claude-code"`` differs between the two vocabularies.
_RUNTIME_TRIPLE: dict[RuntimeId, RuntimeTriple] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


#: Session-policy values accepted by :func:`dispatch`. Only ``"fresh"``
#: runs end-to-end; ``"continue"`` is rejected with -32602 invalid params
#: (the ``--continue`` resume + V5 fallback path is deferred), and ``"hybrid"``
#: falls through to the fresh path (since no prior attempts exist for the wave).
SessionPolicy = Literal["fresh", "continue", "hybrid"]

#: Signals accepted by :func:`kill`. ``"halt"`` (default) and the legacy
#: ``"term"`` alias both map to a graceful SIGTERM; ``"kill"`` maps to a hard
#: SIGKILL. The two SIGTERM spellings coexist so the autopilot TUI's existing
#: ``"term"`` halt signal keeps validating while the criterion-canonical
#: ``"halt"`` spelling is the default.
KillSignal = Literal["halt", "term", "kill"]

#: ``KillSignal`` values that select SIGKILL (the hard stop); every other
#: accepted value selects SIGTERM (the graceful stop).
_HARD_KILL_SIGNALS: frozenset[str] = frozenset({"kill"})


class DispatchOutcome(BaseModel):
    """Post-dispatch metering the caller hands :func:`dispatch`.

    This is the HAND-FED-OUTCOME path: a caller that already metered a dispatch
    out of band supplies the served model + token tally + priced cost (plus an
    optional error-driven fallback) so the dispatch runner emits the C09 events
    without spawning. The live-spawn path (``spawn=True``) derives its own metered
    outcome from a real subprocess instead and is mutually exclusive with this.
    When :attr:`primary_error` is set the dispatch runner emits a
    ``runtime_switched`` event and the :attr:`fallback_runtime` serves the
    dispatch; the cost is always billed against the serving attempt.

    Attributes:
        model: Model identifier the serving runtime priced its cost
            against.
        input_tokens: Non-cached input tokens billed.
        output_tokens: Output tokens billed.
        cache_creation_input_tokens: Tokens written to the prompt cache.
        cache_read_input_tokens: Tokens served from the prompt cache.
        cost_usd: Priced cost in USD (string-encoded ``Decimal`` for
            exact accounting on the wire).
        pricing_version: ``PRICING`` snapshot version pinning
            *cost_usd*. Defaults to the embedded
            :data:`~eawf.observability.telemetry.pricing.PRICING_VERSION`.
        primary_error: Typed
            :class:`~eawf.observability.telemetry.models.RuntimeErrorClass` member when
            the primary runtime failed (triggers a V5 fallback +
            ``runtime_switched`` event), or ``None`` when the primary
            served the dispatch with no switch.
        fallback_runtime: Runtime the V5 ladder falls through to when
            *primary_error* is set. Required whenever *primary_error* is
            set; ignored otherwise.
    """

    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    pricing_version: str = Field(default=PRICING_VERSION, min_length=1)
    primary_error: RuntimeErrorClass | None = None
    fallback_runtime: RuntimeId | None = None


class DispatchParams(BaseModel):
    """Params for :func:`dispatch`.

    Attributes:
        wave_id: Wave to dispatch against; must exist in ``state.json``.
        runtime: Optional runtime adapter override. When omitted, the
            dispatcher picks
            :class:`~eawf.kernel.state.models.Wave.runtime_preference[0]`
            (a daemon-config default also applies). When *skill_manifest*
            is supplied, this override is validated against the manifest
            ``runtime`` list.
        session_policy: V8 dispatch policy. Only ``"fresh"`` runs today
            (``"continue"`` / V5 fallback is deferred to a later phase).
        skill_manifest: Optional per-skill manifest
            (:class:`~eawf.runtime.runtimes.plugin_manifest.SkillManifest`). When
            present the dispatcher runs the skill -> adapter handshake —
            it picks the highest-preference runtime that is both hostable
            by the skill manifest *and* resolvable to an adapter, and
            rejects an off-manifest *runtime* override. When omitted the
            legacy override-or-preference pick (:func:`_pick_runtime`)
            runs unchanged.
        outcome: Optional post-dispatch metering
            (:class:`DispatchOutcome`). When supplied *and* the daemon
            context carries an ``event_path``, the dispatcher drives
            :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` so the C09
            ``runtime_switched`` (on a V5 fallback) + ``dispatch_cost``
            events land in the live event log. When omitted the method
            stays plan-only. Mutually exclusive with *spawn* -- a live
            spawn derives its own metered outcome, so passing both is a
            contradiction the dispatcher rejects.
        spawn: Opt-in live-spawn flag. When ``True`` (and the daemon
            context carries both ``state_path`` and ``event_path``) the
            dispatcher registers an executor
            :class:`~eawf.kernel.state.models.AgentSession`, renders the
            prompt, resolves the runtime adapter, and ``await``s its
            :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
            (jailed argv + scrubbed env -- the safety floor), then prices
            the spawn and drives the dispatch runner with the registered
            session id so the ``agent_end`` report emit fires. ``False``
            (default) keeps the plan-only / hand-fed-outcome surface
            byte-unchanged.
        model: Optional model id override for the live spawn. When unset,
            the live spawn resolves the model from the wave's
            ``(agent_role, effort_bucket)`` via
            :func:`eawf.workflow.dispatch.routing.resolve_routing`. Ignored
            unless *spawn* is ``True``.
        out_of_order: Internal fleet flag that relaxes sibling ordering only;
            all session guards still run in the canonical claim transaction.
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str = Field(min_length=1)
    runtime: str | None = None
    session_policy: SessionPolicy = "fresh"
    skill_manifest: SkillManifest | None = None
    outcome: DispatchOutcome | None = None
    spawn: bool = False
    model: str | None = None
    out_of_order: bool = False


class DispatchPlan(BaseModel):
    """Dispatch-plan result.

    On the plan-only / hand-fed-outcome paths the daemon returns this plan
    (attempt number, session id, typed annotation + attempt rows) without a
    subprocess. On the live-spawn path (``spawn=True``) the plan carries the real
    captured child ``pid`` + the registered session id off the actual spawn.

    When the caller supplies a :class:`DispatchOutcome` and the daemon
    context carries an ``event_path``, :attr:`event_ids` carries the ids
    of the C09 envelopes
    :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` emitted (the
    ``runtime_switched`` envelope first when a V5 fallback fired, then
    ``dispatch_cost``); it is empty otherwise.

    Attributes:
        session_id: Session id for this dispatch. On the plan-only and
            hand-fed-outcome paths this is the cosmetic UUID-v4 the plan
            mints; on the live-spawn path it is the registered
            :class:`~eawf.kernel.state.models.AgentSession` id so callers
            can correlate the plan against the persisted session row.
        attempt: 1-based attempt number for the wave.
        pid: Subprocess PID. ``0`` on the plan-only / hand-fed-outcome
            paths (no subprocess); the real child pid captured via the
            spawn's ``on_spawn`` callback on the live-spawn path.
        runtime: Resolved runtime adapter id (plugin spelling).
        annotation: Typed dispatch annotation for the attempt. ``None`` on the
            live-spawn drop path, where close-on-behalf closed the wave mid-flight
            and the attempt was dropped rather than persisted (R3 concurrency fix).
        session_attempt: Typed session-attempt row for the dispatch (the
            live-spawn path persists it on the registered session). ``None`` on
            the live-spawn drop path (see :attr:`annotation`).
        event_ids: Ids of the C09 envelopes emitted to the live event
            log, in append order; empty when no outcome was supplied and
            no live spawn ran. On the live-spawn path these are the
            runner's emitted ids, including the report-driven events; empty on
            the drop path (``run_dispatch`` is short-circuited so no cost /
            EU accrues onto the already-terminal wave).
    """

    model_config = ConfigDict(extra="forbid")
    session_id: str
    attempt: int
    pid: int
    runtime: str
    annotation: DispatchAnnotation | None = None
    session_attempt: SessionAttempt | None = None
    event_ids: tuple[str, ...] = ()


class SessionParams(BaseModel):
    """Params for :func:`session`."""

    model_config = ConfigDict(extra="forbid")
    wave_id: str = Field(min_length=1)
    attempt: int | None = Field(default=None, ge=1)


class SessionResult(BaseModel):
    """Result of :func:`session` — typed sessions map for a wave."""

    model_config = ConfigDict(extra="forbid")
    sessions: dict[int, SessionAttempt]


class KillParams(BaseModel):
    """Params for :func:`kill`."""

    model_config = ConfigDict(extra="forbid")
    wave_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    signal: KillSignal = "halt"


class KillResult(BaseModel):
    """Result of :func:`kill` -- whether a process group was signalled.

    Attributes:
        killed: ``True`` when a live fleet lane OR a single-wave dispatched
            session resolved for the ``(wave_id, attempt)`` pair and its process
            group was signalled (including the already-dead race, which still
            counts as reaped); ``False`` when neither resolved so nothing was
            signalled.
        signal: The :data:`KillSignal` the caller requested, echoed back so the
            response records which rung of the ladder was attempted.
        reason: A short not-found cause when *killed* is ``False`` (e.g.
            ``no-fleet-run`` / ``unkillable-lane`` for a lane, or
            ``no-session`` / ``unkillable-session`` for a dispatched session);
            ``None`` on a successful kill.
    """

    model_config = ConfigDict(extra="forbid")
    killed: bool
    signal: KillSignal
    reason: str | None = None


class PauseParams(BaseModel):
    """Params for :func:`pause` / :func:`resume`.

    Attributes:
        repo_root: The caller's intended repo root; the toggle persists
            into that repo's state (multi-root serve). ``None`` (the
            legacy shape) resolves to the daemon-bound boot root.
    """

    model_config = ConfigDict(extra="forbid")
    repo_root: str | None = None


class PauseResult(BaseModel):
    """Result of :func:`pause` / :func:`resume` — the persisted flag value.

    Attributes:
        paused: The durable
            :attr:`~eawf.kernel.state.models.State.dispatch_paused` value
            after the mutation — ``True`` after ``agent.pause``, ``False``
            after ``agent.resume``.
    """

    model_config = ConfigDict(extra="forbid")
    paused: bool


def _pick_runtime(*, override: str | None, preference: list[str] | None) -> str:
    """Pick the runtime adapter id for a dispatch.

    Args:
        override: Caller-supplied ``runtime`` param. Wins when set.
        preference: ``Wave.runtime_preference`` from state.

    Returns:
        Adapter id string.

    Raises:
        ValueError: When neither *override* nor *preference* yields a
            runtime -- the dispatch fails fast so the operator notices the gap
            rather than silently picking an arbitrary runtime.
    """
    if override:
        return override
    if preference:
        return preference[0]
    raise ValueError("no runtime resolved: pass 'runtime' param or set wave.runtime_preference")


def _runtime_triple(runtime_id: str) -> RuntimeTriple:
    """Translate a plugin-manifest runtime id to its event-surface spelling.

    Args:
        runtime_id: Resolved runtime adapter id in the plugin-manifest
            vocabulary (``"claude-code"`` / ``"codex"`` / ``"opencode"``).

    Returns:
        The short :data:`~eawf.kernel.store.kinds.events.base.RuntimeTriple`
        spelling the C09 event payloads key on.

    Raises:
        ValueError: When *runtime_id* is not a known plugin-manifest
            runtime id.
    """
    try:
        return _RUNTIME_TRIPLE[runtime_id]  # type: ignore[index]
    except KeyError as exc:
        known = ", ".join(sorted(_RUNTIME_TRIPLE))
        raise ValueError(f"unknown runtime: {runtime_id!r} (known: {known})") from exc


def _emit_dispatch_events(
    ctx: MethodContext,
    *,
    wave_id: str,
    runtime: str,
    outcome: DispatchOutcome,
    trace_request_id: str | None,
    session_id: str | None = None,
) -> tuple[str, ...]:
    """Drive the dispatch runner so the C09 events land in the live log.

    Translates the resolved *runtime* (and the outcome's
    :attr:`DispatchOutcome.fallback_runtime`) to the event-surface
    :data:`~eawf.kernel.store.kinds.events.base.RuntimeTriple` spelling, then
    calls :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch`, which emits a
    ``runtime_switched`` event when *outcome* carries a ``primary_error``
    and always emits a ``dispatch_cost`` event through the daemon
    canonical writer.

    When no fallback runtime is supplied (the no-error path) the primary
    runtime stands in as the unused ``fallback_runtime`` argument the
    runner requires.

    Args:
        ctx: Daemon method context — supplies ``event_path`` + ``bus``.
        wave_id: ``W<NN>`` wave being dispatched.
        runtime: Resolved primary runtime adapter id (plugin spelling).
        outcome: Post-dispatch metering the caller supplied.
        trace_request_id: Optional daemon RPC request id for the §5.8
            correlation chain.
        session_id: Optional registered
            :class:`~eawf.kernel.state.models.AgentSession` id. When
            supplied it is threaded into
            :func:`~eawf.runtime.daemon.dispatch_runner.run_dispatch` so
            the ``agent_end`` report emit fires; ``None`` (default)
            preserves the pre-W01 hand-fed-outcome behaviour where the
            runner skips the report.

    Returns:
        The ids of the emitted envelopes, in append order.

    Raises:
        ValueError: When ``outcome.primary_error`` is set but
            ``outcome.fallback_runtime`` is unset, or when a runtime id
            cannot be mapped to its event-surface spelling.
    """
    primary_triple = _runtime_triple(runtime)
    if outcome.primary_error is not None:
        if outcome.fallback_runtime is None:
            raise ValueError("fallback_runtime required when primary_error is set")
        fallback_triple = _runtime_triple(outcome.fallback_runtime)
    else:
        fallback_triple = primary_triple
    tokens = DispatchTokens(
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        cache_creation_input_tokens=outcome.cache_creation_input_tokens,
        cache_read_input_tokens=outcome.cache_read_input_tokens,
    )
    result = run_dispatch(
        ctx,
        wave_id=wave_id,
        primary_runtime=primary_triple,
        fallback_runtime=fallback_triple,
        model=outcome.model,
        pricing_version=outcome.pricing_version,
        primary_error=outcome.primary_error,
        tokens=tokens,
        cost_usd=outcome.cost_usd,
        trace_request_id=trace_request_id,
        session_id=session_id,
    )
    return result.event_ids


def _build_plan(
    *,
    wave_id: str,
    runtime: str,
    state_path: Path | None,
) -> DispatchPlan:
    """Compute a fresh-dispatch plan for *wave_id*.

    Reads ``state.json`` (when configured) to find the highest existing
    attempt number and pick the next one. When the daemon runs without
    an on-disk state (unit tests; daemonless paths), the wave is
    treated as having zero attempts — the plan defaults to attempt 1.

    Args:
        wave_id: Wave id to dispatch against.
        runtime: Runtime adapter id (already resolved by
            :func:`_pick_runtime`).
        state_path: Optional path to ``state.json``.

    Returns:
        A :class:`DispatchPlan` carrying the typed annotation + session-attempt
        payload for the plan-only / hand-fed-outcome dispatch (no subprocess).

    Raises:
        ValueError: When *wave_id* is unknown in the on-disk state.
    """
    session_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    # The opaque handle here is the URN-form sentinel for the plan-only /
    # hand-fed-outcome path; the live-spawn path (``_spawn_and_dispatch``)
    # substitutes the adapter-resolved session-log handle off the real spawned
    # subprocess instead.
    handle = f"urn:eawf:v1:session-log:{runtime}:{uuid.uuid4().hex}"

    attempt = 1
    runtime_from: str | None = None
    if state_path is not None and Path(state_path).exists():
        state = load_state(Path(state_path))
        wave = state.waves.get(wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {wave_id!r}")
        if wave.sessions:
            attempt = max(wave.sessions) + 1
            last = wave.sessions[max(wave.sessions)]
            runtime_from = last.runtime if last.runtime != runtime else None

    # ``runtime_from`` is set only when the resolved runtime differs from the
    # last attempt's — an operator/preference override with no error involved,
    # so the swap is manual. The error-driven V5 reactive switch lives in
    # ``runtimes.fallback`` and owns ``SWITCH_ON_ERROR``.
    note = DispatchNote.FRESH_DISPATCH if runtime_from is None else DispatchNote.SWITCH_MANUAL
    annotation = DispatchAnnotation(
        attempt=attempt,
        note=note,
        runtime_from=runtime_from,
        runtime_to=runtime,
        occurred_at=now,
    )
    session_attempt = SessionAttempt(
        attempt=attempt,
        runtime=runtime,
        session_id=session_id,
        session_log_handle=handle,
        started_at=now,
    )
    return DispatchPlan(
        session_id=session_id,
        attempt=attempt,
        pid=0,
        runtime=runtime,
        annotation=annotation,
        session_attempt=session_attempt,
    )


#: Default routing inputs when a dispatched wave omits ``agent_role`` /
#: ``effort_bucket``. An untyped live-spawn wave defaults to executor, and a
#: medium-effort default keeps the resolved model on the mid pricing tier rather
#: than the costlier Opus tier when the wave carries no effort hint.
_DEFAULT_SPAWN_ROLE: AgentSessionRole = AgentSessionRole.EXECUTOR
_DEFAULT_SPAWN_EFFORT: EffortBucket = EffortBucket.M


def _validate_report_body(raw: object, *, role: AgentSessionRole) -> AgentReportBody:
    """Force spawned output to the report-body schema for *role*.

    The forced-schema validator the live-spawn lane hands
    :func:`~eawf.workflow.dispatch.llm_assist.assist_with_schema`. The
    session role selects one narrow body class rather than the whole
    ``agent_end`` union. A spawn that emits a different role or omits a
    role-required field is rejected and re-asked, never persisted under a
    mismatched session.

    Args:
        raw: JSON-decoded spawn output (the assist loop decodes the
            ``text`` before handing it here).

    Returns:
        The validated role-specific :class:`AgentReportBody`.

    Raises:
        pydantic.ValidationError: When *raw* does not satisfy the selected
            report-body schema (the assist loop catches this to re-ask).
    """
    body_class = body_class_for_role(role)
    return cast(AgentReportBody, body_class.model_validate(raw))


def _candidate_report_envelope(
    body: AgentReportBody,
    *,
    role: AgentSessionRole,
    wave_id: str,
    session_id: str,
    session_scope_id: str,
    runtime: str,
    generated_at: datetime,
) -> Envelope:
    """Build an unpersisted report envelope for canonical invariant checks."""
    report_id = report_record_id(role=role, base_id=wave_id, attempt=1)
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=session_id,
        scope_id=session_scope_id,
        base_id=wave_id,
        attempt=1,
        runtime=runtime,
        generated_at=generated_at,
        summary=body.summary[:500],
    )
    payload = AgentReportPayload(header=header, body=body)
    return Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=session_scope_id,
        created_at=generated_at,
        summary=header.summary,
        payload=payload.model_dump(mode="json"),
    )


def _validate_live_report_body(
    raw: object,
    *,
    state: State,
    role: AgentSessionRole,
    wave_id: str,
    session_id: str,
    session_scope_id: str,
    runtime: str,
    generated_at: datetime,
) -> AgentReportBody:
    """Validate schema plus canonical report invariants before persistence."""

    def _check(body: AgentReportBody) -> AgentReportBody:
        envelope = _candidate_report_envelope(
            body,
            role=role,
            wave_id=wave_id,
            session_id=session_id,
            session_scope_id=session_scope_id,
            runtime=runtime,
            generated_at=generated_at,
        )
        violations = list(check_agent_report_invariants(state, [envelope]))
        if violations:
            detail = "; ".join(f"{violation.code}: {violation.message}" for violation in violations)
            raise ValueError(f"agent report invariant failed: {detail}")
        return body

    body = _validate_report_body(raw, role=role)
    validator: TypeAdapter[AgentReportBody] = TypeAdapter(
        Annotated[AgentReportBody, AfterValidator(_check)]
    )
    return validator.validate_python(body)


def _resolve_spawn_model(
    state_path: Path, *, wave_id: str, runtime: str, override: str | None
) -> str:
    """Resolve the model id the live spawn prices + runs against.

    An explicit *override* wins. Otherwise the live dispatch resolves the wave's
    ``(agent_role, effort_bucket)`` to a :class:`~eawf.workflow.dispatch.routing.RoutingDecision`
    directly via :func:`eawf.workflow.dispatch.routing.resolve_routing` (so the
    per-role tier table selects the model tier instead of a hardcoded default),
    then maps that decision onto the serving *runtime*'s own vendor model via
    :func:`eawf.workflow.dispatch.routing.runtime_model_for_decision`. The
    executor / medium-effort defaults stand in when the wave omits either field
    so the resolver always returns a priced row. Mapping per runtime is the
    cross-vendor fix: a codex / opencode spawn must run its OWN vendor's model
    (a bare OpenAI id / a ``provider/model`` id) rather than a claude id the
    foreign CLI rejects; the claude path stays byte-identical.

    Args:
        state_path: Path to ``state.json`` (read to recover the wave's
            role + effort hints).
        wave_id: ``W<NN>`` wave being dispatched.
        runtime: Resolved runtime adapter id (plugin-manifest spelling) the
            model is selected for. Mapped to the short ``RuntimeTriple``
            spelling the routing table keys on.
        override: Caller-supplied ``model`` param; wins when set.

    Returns:
        The resolved per-runtime model id (a key of the pricing snapshot).

    Raises:
        ValueError: When *wave_id* is unknown in the on-disk state.
    """
    if override:
        return override
    state = load_state(state_path)
    wave = state.waves.get(wave_id)
    if wave is None:
        raise ValueError(f"unknown wave: {wave_id!r}")
    role = wave.agent_role if wave.agent_role is not None else _DEFAULT_SPAWN_ROLE
    effort = wave.effort_bucket if wave.effort_bucket is not None else _DEFAULT_SPAWN_EFFORT
    triple = _runtime_triple(runtime)
    runtime_models = resolve_runtime_tier_models(state_path.parent.parent)
    decision = resolve_routing(role, effort)
    model = runtime_model_for_decision(decision, triple, runtime_models=runtime_models)
    logger.debug(
        f"_resolve_spawn_model wave={wave_id} role={role.value} "
        f"effort={effort.value} runtime={triple!r} tier_model={decision.model!r} model={model!r}"
    )
    return model


def _preflight_spawn_model(
    state_path: Path,
    *,
    wave_id: str,
    runtime: str,
    override: str | None,
) -> str:
    """Validate the adapter and resolve its model without mutating state."""
    select_adapter(runtime)
    return _resolve_spawn_model(
        state_path,
        wave_id=wave_id,
        runtime=runtime,
        override=override,
    )


def _resolve_budget_enforce(state_path: Path) -> EnforceMode:
    """Resolve ``flow.budget.enforce`` for the repo that owns ``state_path``."""
    repo = state_path.parent.parent
    merged, _sources = merge_config(workspace=repo, repo=repo)
    flow = merged.get("flow")
    budget = flow.get("budget") if isinstance(flow, dict) else None
    value = budget.get("enforce", DEFAULT_ENFORCE) if isinstance(budget, dict) else DEFAULT_ENFORCE
    if value not in ("soft", "hard"):
        raise ValueError(f"invalid flow.budget.enforce: {value!r}")
    return cast(EnforceMode, value)


def _resolve_config_runtime_preference(state_path: Path) -> list[str]:
    """Return the repo's configured runtime preference order, or ``[]``.

    The fleet drive dispatches a wave that carries no per-wave
    :attr:`~eawf.kernel.state.models.Wave.runtime_preference`; without a
    fallback the dispatch fails fast with "no runtime resolved" because
    :func:`_pick_runtime` refuses to guess. The project's
    ``runtime.preference`` (synthesised from ``runtime.adapters``) is the
    operator's *explicit* runtime choice, not an arbitrary pick, so it is the
    correct default for an unpinned wave -- this is what lets ``eawf init
    --runtime codex`` actually drive a codex fleet without stamping every wave.

    Args:
        state_path: Filesystem path to ``state.json``.

    Returns:
        The configured runtime ids in preference order; empty when no
        ``runtime`` block is configured.
    """
    repo = state_path.parent.parent
    merged, _sources = merge_config(workspace=repo, repo=repo)
    runtime = merged.get("runtime")
    if not isinstance(runtime, dict):
        return []
    raw = runtime.get("preference") or runtime.get("adapters") or []
    return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []


#: Which measure the headless snapshots come from: the spawn's own wall-clock
#: duration, which is a different quantity from either the transcript's per-turn
#: figure or the statusline's cost block. Declared so a flip between sources is a
#: known change rather than an unknown one (see P30-I25-W45).
_HEADLESS_MEASURE_VERSION: int = 201


def _headless_runtime_snapshots(
    spawn_result: SpawnResult, *, serving_runtime: str
) -> tuple[RuntimeBaseline, RuntimeLatest]:
    """Build a zero baseline + priced latest crediting one headless spawn.

    A headless codex/claude spawn fires no ``runtime.capture`` RPC -- that
    writer is driven by the Claude Code ``Stop`` hook, which a sandboxed
    non-Claude runtime never runs. Left unstamped, the spawn's metered cost
    lands only in the ``dispatch_cost`` event and never on the wave, so the
    lane spend, the close actuals, and the fleet ``spent_usd`` counter all read
    zero. The single spawn IS the whole runtime, so the baseline is the zero
    point and the latest carries the spawn's priced cost, token tally, and
    wall-clock duration; :func:`compute_runtime_delta` then yields the spawn's
    real spend (USD) and a duration-derived EU.

    Args:
        spawn_result: The completed, already-priced live spawn outcome.
        serving_runtime: The runtime that served the spawn -- recorded as the
            ``harness`` attribution on both snapshots.

    Returns:
        A ``(baseline, latest)`` pair whose delta is the spawn's spend.
    """
    priced = price_spawn_result(spawn_result)
    model = spawn_result.resolved_model or spawn_result.model
    duration_ms = max(
        0, int((spawn_result.ended_at - spawn_result.started_at).total_seconds() * 1000)
    )
    baseline = RuntimeBaseline(
        measure_version=_HEADLESS_MEASURE_VERSION,
        api_duration_ms=0,
        total_duration_ms=0,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        harness=serving_runtime,
        model=model,
        session_id=spawn_result.session_id,
        captured_at=spawn_result.started_at,
    )
    latest = RuntimeLatest(
        measure_version=_HEADLESS_MEASURE_VERSION,
        api_duration_ms=duration_ms,
        total_duration_ms=duration_ms,
        cost_usd=float(priced.cost_usd),
        input_tokens=spawn_result.input_tokens,
        output_tokens=spawn_result.output_tokens,
        cache_creation_input_tokens=spawn_result.cache_creation_input_tokens,
        cache_read_input_tokens=spawn_result.cache_read_input_tokens,
        harness=serving_runtime,
        model=model,
        session_id=spawn_result.session_id,
        captured_at=spawn_result.ended_at,
    )
    return baseline, latest


def _persist_live_session_attempt(
    ctx: MethodContext,
    *,
    wave_id: str,
    requested_runtime: str,
    serving_runtime: str,
    session_log_handle: str,
    spawn_result: SpawnResult,
    pid: int,
) -> tuple[int, DispatchAnnotation, SessionAttempt] | None:
    """Persist the live spawn attempt, including the pid used for kill/budget.

    Re-reads ``wave.status`` under the state lock BEFORE computing the attempt:
    close-on-behalf (the liveness watcher resolving a wave "closed") can move
    the wave to a terminal status while this dispatch is still in flight
    unlocked. When the wave has already reached a terminal status
    (:data:`_TERMINAL_WAVE_STATUSES`), the attempt is DROPPED entirely -- no
    :class:`~eawf.kernel.state.models.SessionAttempt` is written, no
    ``dispatch_history`` row is appended, no cost / tokens accrue, the attempt
    counter is not bumped, and ``state.json`` is not mutated -- and ``None`` is
    returned so the caller short-circuits the dispatch rather than driving a
    phantom attempt onto a closed wave (R3 concurrency fix).

    Returns:
        The ``(attempt, annotation, session_attempt)`` triple for the persisted
        attempt on the non-terminal path, or ``None`` when the wave was already
        terminal at persist time and the attempt was dropped.
    """
    if ctx.state_path is None:
        raise LiveSpawnError(f"live spawn requires state_path for wave: {wave_id!r}")
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        wave = state.waves.get(wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {wave_id!r}")
        if wave.status in _TERMINAL_WAVE_STATUSES:
            # Close-on-behalf raced this in-flight dispatch and already closed
            # the wave; drop the attempt so no phantom SessionAttempt / cost /
            # tokens land on a terminal wave. Nothing is written to state.json.
            logger.info(
                f"_persist_live_session_attempt wave={wave_id} "
                f"status={wave.status.value} outcome=dropped-terminal"
            )
            return None
        previous_attempt = max(wave.sessions) if wave.sessions else None
        previous = wave.sessions[previous_attempt] if previous_attempt is not None else None
        attempt = (previous_attempt or 0) + 1
        switched = serving_runtime != requested_runtime
        if switched:
            note = DispatchNote.SWITCH_ON_ERROR
            runtime_from = requested_runtime
        elif previous is not None and previous.runtime != serving_runtime:
            note = DispatchNote.SWITCH_MANUAL
            runtime_from = previous.runtime
        else:
            note = DispatchNote.FRESH_DISPATCH
            runtime_from = None
        now = datetime.now(UTC)
        annotation = DispatchAnnotation(
            attempt=attempt,
            note=note,
            runtime_from=runtime_from,
            runtime_to=serving_runtime,
            occurred_at=now,
        )
        session_attempt = SessionAttempt(
            attempt=attempt,
            runtime=serving_runtime,
            # SessionAttempt is runtime provenance. The EAWF ``SES-*`` claim
            # id belongs on Wave.claim_session_id and report headers; this row
            # records the vendor/runtime session disclosed by the spawn.
            session_id=spawn_result.session_id,
            session_log_handle=session_log_handle,
            started_at=spawn_result.started_at,
            ended_at=spawn_result.ended_at,
            exit_status=spawn_result.exit_status,
            subprocess_pid=pid,
            cache_creation_input_tokens=spawn_result.cache_creation_input_tokens,
            cache_read_input_tokens=spawn_result.cache_read_input_tokens,
            input_tokens=spawn_result.input_tokens,
            output_tokens=spawn_result.output_tokens,
            # Stamp THIS attempt's own priced cost so a wave with several
            # genuine dispatch attempts surfaces per-attempt cost, not just the
            # single wave-level runtime snapshot (which credits one spawn).
            cost_usd=float(price_spawn_result(spawn_result).cost_usd),
        )
        wave.sessions[attempt] = session_attempt
        wave.dispatch_history.append(annotation)
        # A headless runtime fires no runtime.capture RPC, so credit its priced
        # spawn onto the wave here. The spawn's own SpawnResult is authoritative
        # for this wave's runtime spend, so stamp the matched zero-baseline +
        # priced-latest pair unconditionally: a claim-time sidecar baseline (when
        # one exists) belongs to the operator's interactive session -- a foreign
        # cumulative counter, not this spawn -- so differencing the spawn's
        # absolute latest against it would subtract the wrong origin (and can
        # reject on a negative delta). Replacing both with the matched pair
        # guarantees a clean, non-negative delta whose tokens + cost are exactly
        # this spawn's spend, so compute_runtime_delta yields a real
        # tokens_consumed at close instead of collapsing to zero (a headless
        # wave never reaches this line on an interactive-capture path -- that
        # path runs runtime.capture, never _persist_live_session_attempt).
        baseline, latest = _headless_runtime_snapshots(
            spawn_result, serving_runtime=serving_runtime
        )
        wave.runtime_baseline = baseline
        wave.runtime_latest = latest
        state.updated_at = now
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(
        f"_persist_live_session_attempt wave={wave_id} attempt={attempt} "
        f"runtime={serving_runtime!r} pid={pid}"
    )
    return attempt, annotation, session_attempt


def _restore_dispatch_session(
    state: State,
    *,
    wave_id: str,
    session_id: str,
    runtime: str,
    role: AgentSessionRole,
    scope_id: str,
    started_at: datetime,
) -> bool:
    """Restore one validated claim session and its forward wave index."""
    changed = False
    session = state.agent_sessions.get(session_id)
    if session is None:
        session = AgentSession(
            id=session_id,
            role=role,
            runtime=runtime,
            scope_id=scope_id,
            status=AgentSessionStatus.ACTIVE,
            claimed_wave_ids=[wave_id],
            started_at=started_at,
        )
        state.agent_sessions[session_id] = session
        changed = True
    else:
        if session.role is not role:
            session.role = role
            changed = True
        if session.runtime != runtime:
            session.runtime = runtime
            changed = True
        if session.scope_id != scope_id:
            session.scope_id = scope_id
            changed = True
        if session.status is not AgentSessionStatus.ACTIVE:
            session.status = AgentSessionStatus.ACTIVE
            session.ended_at = None
            changed = True
        if wave_id not in session.claimed_wave_ids:
            session.claimed_wave_ids.append(wave_id)
            changed = True
    if session_id not in state.current.active_session_ids:
        state.current.active_session_ids = [*state.current.active_session_ids, session_id]
        changed = True
    return changed


def _restore_dispatch_wave(
    state: State,
    *,
    wave: Wave,
    session_id: str,
    claimed_at: datetime,
) -> bool:
    """Restore a dispatched wave's status, binding, and active index."""
    changed = False
    if wave.id not in state.current.active_wave_ids:
        state.current.active_wave_ids = [*state.current.active_wave_ids, wave.id]
        changed = True
    if wave.status is WaveStatus.PENDING:
        wave.status = WaveStatus.IN_PROGRESS
        changed = True
    if wave.claim_session_id != session_id:
        wave.claim_session_id = session_id
        changed = True
    if wave.claimed_at is None:
        wave.claimed_at = claimed_at
        changed = True
    return changed


def _reassert_dispatch_state(
    ctx: MethodContext,
    *,
    wave_id: str,
    session_id: str,
    session_runtime: str,
    session_role: AgentSessionRole,
    session_scope_id: str,
    session_started_at: datetime,
) -> None:
    """Restore daemon-owned dispatch rows a jailed agent reverted mid-spawn (W10).

    The daemon writes the claim, the role-matched session registration, and the
    phase-active pointer into the repo-tracked ``.ea/state.json`` BEFORE the
    spawn. A sandboxed executor that runs ``git checkout -- .ea/`` to drop
    out-of-scope changes reverts those uncommitted rows, so the post-spawn
    close would resolve a corrupted state: a missing session ``KeyError``s the
    close, a wave reverted to ``PENDING`` falls off the ready frontier, and a
    cleared ``phase_id`` orphans the phase. Reload post-spawn and re-assert any
    missing row from the wave's own bookkeeping so the close proceeds against
    authoritative state. A no-op when nothing was reverted (the healthy path).

    Args:
        ctx: Daemon method context carrying the bound ``state_path``.
        wave_id: The dispatched wave whose rows are re-asserted.
        session_id: The bound session id the close resolves.
        session_runtime: Runtime recorded on the validated claim session.
        session_role: Role recorded on the validated claim session.
        session_scope_id: Scope recorded on the validated claim session.
        session_started_at: Original start instant of the claim session.
    """
    if ctx.state_path is None:
        return
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        wave = state.waves.get(wave_id)
        if wave is None or wave.status in _TERMINAL_WAVE_STATUSES:
            # Unknown wave, or close-on-behalf already resolved it terminally --
            # do not resurrect a wave the daemon lawfully finished.
            return
        session_changed = _restore_dispatch_session(
            state,
            wave_id=wave_id,
            session_id=session_id,
            runtime=session_runtime,
            role=session_role,
            scope_id=session_scope_id,
            started_at=session_started_at,
        )
        wave_changed = _restore_dispatch_wave(
            state,
            wave=wave,
            session_id=session_id,
            claimed_at=session_started_at,
        )
        changed = session_changed or wave_changed
        iter_row = state.iters.get(wave.iter_id)
        phase_id = iter_row.phase_id if iter_row is not None else None
        if phase_id is not None and state.current.phase_id != phase_id:
            state.current.phase_id = phase_id
            state.current.iter_id = wave.iter_id
            changed = True
        if changed:
            state.updated_at = datetime.now(UTC)
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
            logger.warning(
                f"_reassert_dispatch_state wave={wave_id} session={session_id!r} "
                f"restored=reverted-dispatch-rows"
            )


def _claim_live_session(
    ctx: MethodContext,
    *,
    wave_id: str,
    runtime: str,
    out_of_order: bool,
    model_override: str | None = None,
) -> _LiveClaim:
    """Commit one live session + wave claim transaction before spawning.

    A PENDING wave stages a wave-scoped ACTIVE session and claims with that
    exact id under one state lock and one atomic state write. Session-start and
    claim events append only after the state commit, so any lifecycle or H02
    guard rejection leaves both state and event stores byte-identical. A
    CLAIMED/IN_PROGRESS wave may be redispatched only through its ACTIVE bound
    session; no missing, stale, or different session is silently substituted.

    Args:
        ctx: Daemon method context carrying canonical state/event paths.
        wave_id: Wave to bind before live process creation.
        runtime: Resolved runtime for a newly staged session.
        out_of_order: Whether sibling-order relaxation was explicitly requested.
        model_override: Optional explicit model id resolved before any mutation.

    Returns:
        The committed session id, authoritative runtime, and session role.

    Raises:
        LiveSpawnError: When state/event paths are unavailable.
        LifecycleError: When a lifecycle or claim-session guard rejects.
    """
    if ctx.state_path is None or ctx.event_path is None:
        raise LiveSpawnError(f"live spawn requires state_path + event_path for wave: {wave_id!r}")
    state_path = Path(ctx.state_path)
    event_path = Path(ctx.event_path)
    staged_event: Envelope | None = None
    claim_event: Envelope | None = None
    before_version = ""
    after_version = ""
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        before_version = state_version(state.model_dump(mode="json"))
        wave = state.waves.get(wave_id)
        if wave is None:
            raise LifecycleError(f"unknown wave {wave_id!r}")

        if wave.status in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
            session_id = wave.claim_session_id
            if session_id is None:
                raise LifecycleGuardError(
                    CLAIM_SESSION_NOT_FOUND,
                    wave_id,
                    f"cannot dispatch wave {wave_id!r}: claimed wave has no bound session",
                )
            session = validate_claim_session(state, wave, session_id)
            model = _preflight_spawn_model(
                state_path,
                wave_id=wave_id,
                runtime=session.runtime,
                override=model_override,
            )
            logger.info(
                f"_claim_live_session reuse wave={wave_id} runtime={session.runtime!r} "
                f"session={session.id!r}"
            )
            return _LiveClaim(
                session_id=session.id,
                runtime=session.runtime,
                model=model,
                role=session.role,
                scope_id=session.scope_id,
                started_at=session.started_at,
            )

        required_role = wave.agent_role or AgentSessionRole.EXECUTOR
        model = _preflight_spawn_model(
            state_path,
            wave_id=wave_id,
            runtime=runtime,
            override=model_override,
        )
        active = next(
            (
                session
                for session in state.agent_sessions.values()
                if session.scope_id == wave_id
                and session.runtime == runtime
                and session.status is AgentSessionStatus.ACTIVE
            ),
            None,
        )
        if active is None:
            staged = stage_session(
                state=state,
                role=required_role,
                scope_id=wave_id,
                runtime=runtime,
            )
            session = staged.session
            staged_event = staged.event
        else:
            session = active

        claim_wave(
            state,
            wave_id=wave_id,
            session_id=session.id,
            out_of_order=out_of_order,
        )
        state.updated_at = datetime.now(UTC)
        new_payload = state.model_dump(mode="json")
        after_version = state_version(new_payload)
        claimed_at = state.waves[wave_id].claimed_at or datetime.now(UTC)
        claim_event = build_event(
            event_id=f"EV-{uuid.uuid4().hex}",
            event_type="wave.claim",
            actor=session.id,
            command="dispatch wave",
            args_hash="",
            status="ok",
            message=f"wave {wave_id} claimed by session {session.id}",
            scope_id=wave_id,
            occurred_at=claimed_at,
            before_state_version=before_version,
            after_state_version=after_version,
        )
        atomic_write_json_locked(state_path, new_payload)

    if staged_event is not None:
        commit_event(event_path, staged_event)
    assert claim_event is not None
    commit_event(event_path, claim_event)
    logger.info(
        f"_claim_live_session wave={wave_id} runtime={session.runtime!r} "
        f"session={session.id!r} before={before_version} after={after_version}"
    )
    return _LiveClaim(
        session_id=session.id,
        runtime=session.runtime,
        model=model,
        role=session.role,
        scope_id=session.scope_id,
        started_at=session.started_at,
    )


async def _bind_or_synthesize_report(
    accepted: SpawnResult,
    *,
    state: State,
    binding: _LiveClaim,
    wave_id: str,
    prompt: str,
    serving_runtime: str,
    spawn_once: Callable[[str], Awaitable[SpawnResult]],
) -> AgentReportBody:
    """Bind the spawned agent's output to a report body, synthesizing on exhaustion.

    Wraps :func:`_bind_role_report`: on report-schema exhaustion the bind
    raises :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError` (the model
    answered in prose), which would strand the wave -- ``run_dispatch`` never
    runs, so the dispatch-cost emit + EU accrual never fire even though the
    spawn already spent real cost. The synth fallback mints a typed BLOCKED body
    (marked ``report_source=synthesized``) matching the session role so the
    dispatch always completes and the degrade is auditable -- never a green
    PASS, since a synthesized body was not authored by the agent (see
    :func:`_synthesize_role_report`).

    Returns:
        The bound role-specific :class:`AgentReportBody`, or a synthesized
        body of the same role when the assist loop exhausted its ceiling.
    """
    try:
        return await _bind_role_report(
            accepted,
            state=state,
            binding=binding,
            wave_id=wave_id,
            prompt=prompt,
            serving_runtime=serving_runtime,
            spawn_once=spawn_once,
        )
    except LLMAssistError as exc:
        body = _synthesize_role_report(
            accepted,
            wave_id=wave_id,
            role=binding.role,
            phase_id=state.iters[state.waves[wave_id].iter_id].phase_id,
            exc=exc,
        )
        body = _validate_live_report_body(
            body.model_dump(mode="json"),
            state=state,
            role=binding.role,
            wave_id=wave_id,
            session_id=binding.session_id,
            session_scope_id=binding.scope_id,
            runtime=serving_runtime,
            generated_at=binding.started_at,
        )
        logger.info(
            f"_spawn_and_dispatch wave={wave_id} role={binding.role.value} "
            f"status=synth-fallback "
            f"attempts={exc.attempts} reason={exc.failures[-1].reason!r}"
        )
        return body


async def _bind_role_report(
    accepted: SpawnResult,
    *,
    state: State,
    binding: _LiveClaim,
    wave_id: str,
    prompt: str,
    serving_runtime: str,
    spawn_once: Callable[[str], Awaitable[SpawnResult]],
) -> AgentReportBody:
    """Bind a spawned agent's real output to its role-specific report body.

    Drives :func:`~eawf.workflow.dispatch.llm_assist.assist_with_schema`
    over the spawned agent's OWN ``text`` to populate an
    role-specific :class:`~eawf.kernel.store.kinds.agent_report.AgentReportBody`,
    replacing the runner's synthetic placeholder. The first assist spawn
    reuses *accepted* (the already-completed, already-priced spawn) so the
    initial attempt is not double-spawned; each subsequent re-ask drives a
    fresh spawn of the correction prompt via *spawn_once* on the serving
    runtime. The assist loop's correction notice names the validation
    failure, and on ceiling-exhaustion the loop raises
    :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError`; the caller
    converts that terminal failure into a role-matched BLOCKED synth body.

    Args:
        accepted: The already-completed spawn whose ``text`` is validated
            first (reused so the initial spawn is not repeated).
        prompt: The rendered dispatch prompt; the assist loop appends a
            correction notice to it on a re-ask.
        serving_runtime: The runtime the accepted spawn ran on, recorded for
            the trace log.
        spawn_once: The closure that drives one fresh spawn on the serving
            runtime for a re-ask prompt.

    Returns:
        The validated role-specific report body parsed from the agent's own
        output.

    Raises:
        eawf.workflow.dispatch.llm_assist.LLMAssistError: When every spawn
            (the reused initial plus the re-asks, up to the loop's ceiling)
            produced output that failed the selected report-body schema.
    """
    spawns = 0

    async def _assist_spawn(reask_prompt: str) -> SpawnResult:
        nonlocal spawns
        spawns += 1
        # The first assist iteration validates the already-completed accepted
        # spawn (no re-spawn); re-asks spawn fresh on the serving runtime so
        # the correction notice reaches the model.
        if spawns == 1:
            return accepted
        return await spawn_once(reask_prompt)

    result = await assist_with_schema(
        prompt,
        spawn=_assist_spawn,
        validator=lambda raw: _validate_live_report_body(
            raw,
            state=state,
            role=binding.role,
            wave_id=wave_id,
            session_id=binding.session_id,
            session_scope_id=binding.scope_id,
            runtime=serving_runtime,
            generated_at=binding.started_at,
        ),
    )
    body = result.body
    logger.info(
        f"_bind_role_report runtime={serving_runtime!r} role={binding.role.value} "
        f"attempts={result.attempts_used} verdict={body.verdict.value}"
    )
    if body.role != binding.role.value:  # pragma: no cover - role validator forces this
        raise TypeError(f"assist returned body role {body.role!r}; expected {binding.role.value!r}")
    return body


def _redact_report_body(body: AgentReportBody) -> AgentReportBody:
    """Redact local/sensitive tokens from a headless report body's text.

    A headless agent's report prose may name an absolute path or another
    scrub-tripping token -- the live codex e2e showed the agent citing the
    repo root in its ``outcome``. The report-store scrub
    (:func:`~eawf.workflow.agent_report.store.append_agent_report`) REJECTS
    such a body with :class:`AgentReportScrubError`, which would hard-fail an
    otherwise-successful wave. Rewriting every string field through the
    canonical scrub redactor (:func:`~eawf.platform.scrub.scan.rewrite_text`)
    keeps the agent's report -- verdict, evidence refs, follow-ups -- while
    satisfying the store's no-local-tokens invariant, so the wave closes
    instead of failing. The redactor only rewrites matched path / sensitive
    patterns, so ids (``wave_id``), enum values, and repo-relative file paths
    pass through unchanged.

    Args:
        body: The bound (or synthesized) role-specific report body.

    Returns:
        A new role-specific report body with local tokens rewritten.
    """

    def _walk(value: object) -> object:
        if isinstance(value, str):
            return rewrite_text(value)
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, dict):
            return {key: _walk(item) for key, item in value.items()}
        return value

    redacted = _walk(body.model_dump(mode="json"))
    return _validate_report_body(redacted, role=AgentSessionRole(body.role))


def _synthesize_role_report(
    accepted: SpawnResult,
    *,
    wave_id: str,
    role: AgentSessionRole,
    phase_id: str,
    exc: LLMAssistError,
) -> AgentReportBody:
    """Synthesize a typed role body when the assist loop exhausts its re-asks.

    The safety net for the headless dispatch path: when the spawned agent's
    output never validates against the role's report schema (a model that
    answers in prose re-asks to the ceiling and raises
    :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError`), the wave would
    otherwise hang -- :func:`run_dispatch` never runs, so the dispatch-cost emit
    and EU accrual never fire even though the spawn already spent real cost.
    Rather than let the exception escape, this builds a typed
    role-specific :class:`AgentReportBody` from the accepted spawn's observable
    outcome signals so the dispatch always completes without violating the
    session/body role invariant.

    The verdict is ALWAYS :attr:`AgentReportVerdict.BLOCKED`, mirroring the
    researcher synth path (:func:`~eawf.runtime.daemon.methods.research._bind_researcher_body`):
    a synthesized body means the agent's work was never verified as passing, so
    a green close must not be minted from ``exit_status == 0`` -- the process
    exited, the work did not pass. (BLOCKED is tolerated downstream by the
    close path's blocked handler.) The confidence is :attr:`Confidence.LOW`
    (nothing was verified), the ``report_source`` marker is
    :attr:`ReportSource.SYNTHESIZED`, and a follow-up names the parse failure so
    the degrade is auditable. The body reuses the runner's
    :func:`~eawf.runtime.daemon.dispatch_runner._build_completion_body` so the
    synthetic path mints the same typed shape as the rich-output path.

    Args:
        accepted: The already-completed, already-priced spawn whose exit status
            is recorded in the synthesized prose (but never drives the verdict).
        wave_id: The wave the synthesized report scopes.
        exc: The exhausted-assist error carrying the attempt ceiling and the
            ordered rejection trail (the last failure's ``reason`` is named in
            the synthesized prose + the follow-up).

    Returns:
        A typed role-specific body carrying the BLOCKED synth verdict, LOW
        confidence, ``report_source=synthesized``, and one parse-failure
        follow-up.
    """
    # A synthesized body was never authored by the agent, so it is a degrade,
    # not a verified pass -- mint BLOCKED (mirroring the researcher synth path)
    # so a wave never closes green on output no agent stood behind.
    verdict = AgentReportVerdict.BLOCKED
    last_reason = exc.failures[-1].reason if exc.failures else "unknown"
    # Bound to the executor body's outcome cap (1000); the summary cap (4000)
    # is wider, so a string that fits outcome fits both.
    outcome = (
        f"synthesized {role.value} report: agent output failed report-body validation "
        f"after {exc.attempts} attempt(s) (last reason: {last_reason}); "
        f"spawn exit_status={accepted.exit_status}"
    )[:500]
    # The executor body's commit_sha is min_length=7-or-None, so the builder
    # cannot mint an empty placeholder; pass a 7-char sentinel to satisfy the
    # builder, then drop it to None below (no commit landed on this path).
    built = _build_completion_body(
        role=role,
        wave_id=wave_id,
        commit_sha="0000000",
        outcome=outcome,
        files_changed=[],
        tests_run=[],
        verdict=verdict,
        confidence=Confidence.LOW,
    )
    if built.role != role.value:  # pragma: no cover - builder routes by role
        raise TypeError(f"completion builder returned body role: {built.role!r}")
    # Attach the parse-failure follow-up so every role's degrade is auditable.
    # Canonical report invariants still apply to synthesized rows: executor
    # keeps the explicit sentinel commit, auditor/reviewer carry one grounded
    # failure/coverage row, and operator points at the wave's parent phase.
    followup = AgentReportFollowup(
        title=f"{role.value} output failed report-body validation; report synthesized",
        owner_role=role,
        priority="P1",
        detail=(
            f"the spawned {role.value} answered with output that failed its "
            f"report-body schema after {exc.attempts} attempt(s) "
            f"(last reason: {last_reason}); the report body was synthesized from "
            f"the spawn exit status so cost + EU still accrue"
        )[:500],
    )
    updates: dict[str, object] = {
        "followups": [followup],
        "report_source": ReportSource.SYNTHESIZED,
    }
    evidence_ref = AgentReportEvidenceRef(
        kind="store_record",
        ref=f"wave:{wave_id}",
        note="report-body validation exhausted",
    )
    if isinstance(built, AuditorReportBody):
        updates["criteria"] = [
            CriterionVerdict(
                criterion="agent output satisfies the typed report contract",
                passed=False,
                evidence_refs=[evidence_ref],
            )
        ]
    elif isinstance(built, ReviewerReportBody):
        updates["coverage_refs"] = [evidence_ref]
    elif isinstance(built, OperatorReportBody):
        updates["phase_id"] = phase_id
        updates["completed_wave_ids"] = []
    return built.model_copy(update=updates)


async def _spawn_and_dispatch(
    ctx: MethodContext,
    *,
    wave_id: str,
    runtime: str,
    model_override: str | None,
    trace_request_id: str | None,
    out_of_order: bool,
) -> DispatchPlan:
    """Run the live-spawn dispatch path and return the resulting plan.

    Registers a session matching the wave role, renders the prompt, resolves
    sandbox policy and retry preferences, spawns behind the runtime floor,
    validates the agent's own output into the matching role report body,
    persists the wave-local attempt row, then drives :func:`run_dispatch` with
    the live pid/pgid and config enforcement mode.

    On report-schema exhaustion the spawned agent's output never validates
    against the session role's report body (e.g. a model that answers in prose).
    Rather than let :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError`
    escape -- which would strand the wave with cost already spent but
    :func:`run_dispatch` never run -- the path synthesizes a typed BLOCKED
    role-specific :class:`~eawf.kernel.store.kinds.agent_report.AgentReportBody`
    (``report_source=synthesized``, see :func:`_synthesize_role_report`) so the
    dispatch always completes and the dispatch-cost + EU accrual still fire.
    The BLOCKED verdict then blocks the close: a synthesized body is never closed
    green, because the agent never authored a passing report.

    Args:
        ctx: Daemon method context — supplies ``state_path``, ``event_path``,
            ``bus``.
        wave_id: ``W<NN>`` wave being dispatched.
        runtime: Resolved runtime adapter id (plugin spelling) for the first
            spawn; the retry loop may switch past it on an availability error.
        model_override: Optional explicit model id; when ``None`` the
            model is resolved from the wave's routing inputs.
        trace_request_id: Optional daemon RPC request id for the §5.8
            correlation chain.
        out_of_order: Explicit sibling-order relaxation for fleet dispatch.

    Returns:
        A :class:`DispatchPlan` carrying the real captured ``pid``, the
        registered session id, the serving runtime, and the runner's emitted
        ``event_ids`` (including the report-driven events).

    Raises:
        LiveSpawnError: When required stores are not configured.
        eawf.workflow.dispatch.retry.RetryExhaustedError: When retry cannot
            produce a usable spawn.
    """
    if ctx.state_path is None or ctx.event_path is None:
        raise LiveSpawnError(f"live spawn requires state_path + event_path for wave: {wave_id!r}")
    state_path = Path(ctx.state_path)

    # 1. Resolve the authoritative runtime + model, register/reuse the live
    # session, and claim the wave under one lock + one state write. Adapter/model
    # preflight runs inside the transaction before its first mutation, so an
    # invalid runtime cannot orphan a session, wave claim, or event. Nothing
    # below may render a worktree or spawn a process until this commits.
    binding = _claim_live_session(
        ctx,
        wave_id=wave_id,
        runtime=runtime,
        out_of_order=out_of_order,
        model_override=model_override,
    )
    session_id = binding.session_id
    runtime = binding.runtime
    model = binding.model

    # 2. Render the prompt. The model was selected for the resolved runtime so
    # a codex / opencode spawn runs its own vendor's model rather than a claude
    # id the foreign CLI rejects. A V5 switch re-resolves the model for the
    # switched runtime inside the spawn closure (below), so each runtime always
    # spawns its own model.
    state = load_state(state_path)
    # The live-spawn path reads the spawned model's final message as a JSON
    # role-specific report body, so render the headless prompt: it pins the
    # report schema + an output-only-JSON instruction so the model emits a
    # parseable body on the first try rather than answering in prose.
    role_tier = resolve_role_blocks(state_path.parent.parent)
    envelope = render_dispatch_envelope(
        state,
        wave_id,
        runtime,
        repo_root=state_path.parent.parent,
        role_blocks=role_tier.role_blocks,
        role_tier_token_cap=role_tier.token_cap,
        headless=True,
        role_override=binding.role,
    )

    # 3. Resolve the per-wave sandbox deny-list (wave-scoped + global
    # policies) so the spawned child is launched with those tools disabled.
    # The caller passes only the tool names; the adapter owns the vendor
    # flag spelling.
    denied = resolve_denied_tools(state.sandbox_policies, wave_id=wave_id)
    logger.info(f"_spawn_and_dispatch wave={wave_id} denied_tools={len(denied)}")

    # 4. Resolve the per-wave runtime preference ladder so the retry loop's
    # V5 reactive switch has a runtime to fall through to on an availability
    # error (server / timeout / api). Empty when the wave pins one runtime.
    wave = state.waves.get(wave_id)
    preference = list(wave.runtime_preference) if wave and wave.runtime_preference else []

    # 5. Spawn for real behind the floor through the bounded retry loop: a
    # rate limit retries the same runtime, an availability error switches to
    # the next preference runtime, and an auth error halts. The closure
    # re-resolves the per-runtime adapter so a switch spawns on the next
    # runtime's adapter; ``parse_error`` classifies a ``RuntimeSpawnError``.
    # The adapter jails the argv + scrubs the child env + applies the
    # deny-list. The captured pid rides the ``on_spawn`` callback.
    captured_pid: list[int] = []

    # Live-output streaming (W45 + W19): the adapter awaits ``on_chunk`` once per
    # stdout line AS IT ARRIVES. We BATCH lines and flush on the count batch OR a
    # wall-clock budget (:func:`_chunk_should_flush`), plus a final flush at spawn
    # end, and persist each batch as an ``agent.output.chunk`` event keyed on the
    # wave's scope_id, so the Watch tail renders the agent's words live AND the
    # per-chunk output is durable after the TUI is closed. The time budget bounds
    # how long a sub-batch codex burst sits unpersisted (and thus invisible to the
    # store-backed tail). The buffer + seq + last-flush live in this scope so the
    # closure mutates them across calls; asyncio's single-threaded read loop awaits
    # ``on_chunk`` serially, so no lock is needed.
    chunk_buffer: list[str] = []
    chunk_seq: list[int] = [0]
    last_chunk_flush: list[float] = [time.monotonic()]

    def _flush_chunk_buffer() -> None:
        if not chunk_buffer:
            return
        emit_agent_output_chunk(
            ctx,
            wave_id=wave_id,
            session_id=None,
            seq=chunk_seq[0],
            text="".join(chunk_buffer),
            trace_request_id=trace_request_id,
        )
        chunk_seq[0] += 1
        chunk_buffer.clear()
        last_chunk_flush[0] = time.monotonic()

    async def _on_chunk(line: str) -> None:
        chunk_buffer.append(line)
        # Flush on the line-count batch OR a wall-clock budget (W19): codex
        # streams in bursts at turn boundaries, so a sub-batch burst would sit
        # unpersisted -- invisible to the Watch tail, which reads chunks off the
        # event store -- until the next burst fills the count batch. Bounding the
        # hold time keeps a slow turn's output flowing to the live tail.
        if _chunk_should_flush(
            buffered=len(chunk_buffer),
            elapsed_s=time.monotonic() - last_chunk_flush[0],
        ):
            _flush_chunk_buffer()

    async def _spawn_once(spawn_runtime: str) -> SpawnResult:
        spawn_adapter = select_adapter(spawn_runtime)
        # Re-resolve the model for the runtime actually spawning: on the first
        # spawn this equals ``model``; after a V5 switch the switched runtime
        # gets ITS own vendor model (a bare claude id would be rejected by the
        # codex / opencode CLI). An explicit override pins the model regardless.
        spawn_model = (
            model
            if spawn_runtime == runtime
            else _resolve_spawn_model(
                state_path, wave_id=wave_id, runtime=spawn_runtime, override=model_override
            )
        )
        return await spawn_adapter.spawn_session(
            envelope.prompt,
            model=spawn_model,
            cwd=str(state_path.parent.parent),
            denied_tools=sorted(denied),
            on_spawn=captured_pid.append,
            on_chunk=_on_chunk,
        )

    def _classify(exc: RuntimeSpawnError, spawn_runtime: str) -> ErrorClass:
        # parse_error wants a concrete exit code; a parse-level failure with
        # no exit context coerces to -1, which falls through to the
        # conservative RUNTIME_API_ERROR (a switch signal).
        exit_status = exc.exit_status if exc.exit_status is not None else -1
        return select_adapter(spawn_runtime).parse_error(exit_status, exc.stderr)

    spawn_result = await spawn_with_retry(
        runtime=runtime,
        preference=preference,
        spawn=_spawn_once,
        classify=_classify,
    )
    # Final flush: empty any partial batch the cap did not flush so the persisted
    # chunk tail is complete (the spawn's last few lines are not stranded).
    _flush_chunk_buffer()
    # The serving runtime is the one the accepted spawn ran on -- a V5 switch
    # may have moved it past the originally-resolved runtime. Each adapter
    # stamps its own id on the result, so this is authoritative.
    serving_runtime = spawn_result.runtime
    adapter = select_adapter(serving_runtime)
    pid = captured_pid[-1] if captured_pid else spawn_result.subprocess_pid

    # 6. Price the spawn from its token classes.
    metered = price_spawn_result(spawn_result)
    tokens = DispatchTokens(
        input_tokens=metered.input_tokens,
        output_tokens=metered.output_tokens,
        cache_creation_input_tokens=metered.cache_creation_input_tokens,
        cache_read_input_tokens=metered.cache_read_input_tokens,
    )

    # 7. Bind the spawned agent's OWN output to its validated role report body
    # through the bounded re-ask loop. The first assist spawn reuses the
    # already-completed accepted spawn_result (no double-spawn of the initial
    # attempt); each re-ask drives a fresh spawn of the correction prompt on
    # the serving runtime. On ceiling-exhaustion the loop raises LLMAssistError
    # and the wrapper mints a role-matched BLOCKED synth body.
    serving_model = spawn_result.model

    async def _spawn_correction(reask_prompt: str) -> SpawnResult:
        # A re-ask spawns the correction prompt on the serving runtime's
        # adapter + the model the accepted spawn was requested with (the
        # runtime the accepted spawn ran on), so the model sees the
        # validation-failure notice the assist loop appended.
        return await adapter.spawn_session(
            reask_prompt,
            model=serving_model,
            cwd=str(state_path.parent.parent),
            denied_tools=sorted(denied),
            on_spawn=captured_pid.append,
        )

    # Bind the spawned agent's own output to a validated report body, falling
    # back to a synthesized body on report-schema exhaustion so the dispatch
    # always completes (see _bind_or_synthesize_report).
    report_body = await _bind_or_synthesize_report(
        spawn_result,
        state=state,
        binding=binding,
        wave_id=wave_id,
        prompt=envelope.prompt,
        serving_runtime=serving_runtime,
        spawn_once=_spawn_correction,
    )
    # Redact local/sensitive tokens from the agent's own report prose before the
    # store scrub runs: a headless agent may cite an absolute path in its
    # summary / outcome, which the report-store scrub rejects -- redacting keeps
    # the report and closes the wave instead of hard-failing a successful spawn.
    report_body = _redact_report_body(report_body)
    persisted = _persist_live_session_attempt(
        ctx,
        wave_id=wave_id,
        requested_runtime=runtime,
        serving_runtime=serving_runtime,
        session_log_handle=adapter.session_log_handle(spawn_result.session_id),
        spawn_result=spawn_result,
        pid=pid,
    )
    if persisted is None:
        # Close-on-behalf closed the wave while this dispatch ran unlocked, so
        # the attempt was dropped (no SessionAttempt / cost / tokens). Do NOT
        # drive run_dispatch -- that would emit dispatch_cost + accrue EU onto
        # the already-terminal wave (the phantom attempt R3 guards). Return a
        # no-op plan carrying the real spawn's pid/session but no attempt row
        # and no emitted events. The reported attempt is the wave's existing
        # session count (lock-free read) since this dispatch added none.
        current_wave = load_state(state_path).waves.get(wave_id)
        dropped_attempt = (
            max(current_wave.sessions) if current_wave and current_wave.sessions else 0
        )
        logger.info(
            f"_spawn_and_dispatch wave={wave_id} runtime={serving_runtime!r} "
            f"pid={pid} session={session_id!r} outcome=dropped-terminal"
        )
        return DispatchPlan(
            session_id=session_id,
            attempt=dropped_attempt,
            pid=pid,
            runtime=serving_runtime,
            annotation=None,
            session_attempt=None,
            event_ids=(),
        )
    attempt, annotation, session_attempt = persisted
    # Re-assert any daemon-owned dispatch row a sandboxed agent reverted while
    # it ran (W10): a jailed `git checkout -- .ea/` can drop the uncommitted
    # session registration, claim, and phase pointer, which would otherwise
    # corrupt the close that follows.
    _reassert_dispatch_state(
        ctx,
        wave_id=wave_id,
        session_id=session_id,
        session_runtime=binding.runtime,
        session_role=binding.role,
        session_scope_id=binding.scope_id,
        session_started_at=binding.started_at,
    )
    enforce = _resolve_budget_enforce(state_path)

    # 8. Drive the runner with the registered session id + the validated body
    # so the role-specific ``agent_end`` emit persists the agent's own outcome
    # and verdict. The serving runtime is the one the
    # accepted spawn ran on (a V5 switch may have moved it).
    runtime_triple = _runtime_triple(serving_runtime)
    result: DispatchResult = run_dispatch(
        ctx,
        wave_id=wave_id,
        primary_runtime=runtime_triple,
        fallback_runtime=runtime_triple,
        model=metered.model,
        pricing_version=metered.pricing_version,
        primary_error=None,
        tokens=tokens,
        cost_usd=metered.cost_usd,
        trace_request_id=trace_request_id,
        session_id=session_id,
        report_body=report_body,
        pgid=pid,
        enforce=enforce,
        # The spawned agent's OWN captured answer text feeds the W08 stdout
        # producer so the live spawn emits an ``agent.output`` event the
        # agent-watch tail renders -- without this the producer is wired into
        # run_dispatch but no live caller supplies it, so the tail stays empty.
        output_text=spawn_result.text,
    )
    logger.info(
        f"_spawn_and_dispatch wave={wave_id} runtime={serving_runtime!r} pid={pid} "
        f"session={session_id!r} attempt={attempt} enforce={enforce} "
        f"cost_usd={metered.cost_usd} report_id={result.report_id!r} terminated={result.terminated}"
    )
    return DispatchPlan(
        session_id=session_id,
        attempt=attempt,
        pid=pid,
        runtime=serving_runtime,
        annotation=annotation,
        session_attempt=session_attempt,
        event_ids=result.event_ids,
    )


async def _dispatch_live(
    ctx: MethodContext,
    *,
    args: DispatchParams,
    runtime: str,
) -> DispatchPlan:
    """Run live dispatch and map lifecycle guards to daemon validation."""
    try:
        return await _spawn_and_dispatch(
            ctx,
            wave_id=args.wave_id,
            runtime=runtime,
            model_override=args.model,
            trace_request_id=None,
            out_of_order=args.out_of_order,
        )
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


@register("agent.dispatch")
async def dispatch(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Build a fresh-dispatch plan and emit its C09 events for the wave.

    Resolves the runtime + builds the dispatch plan (attempt number,
    session id, typed annotation + attempt rows). When the caller
    supplies a :class:`DispatchOutcome` *and* the daemon context carries
    an ``event_path``, the method drives
    :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch`, which emits the C09
    ``runtime_switched`` (on a V5 fallback) + ``dispatch_cost`` events to
    the live event log through the daemon canonical writer; the emitted
    envelope ids ride back on :attr:`DispatchPlan.event_ids`. When no
    outcome is supplied (or no ``event_path`` is configured, as in unit
    tests) the method stays plan-only.

    When ``spawn=True`` the method takes the live-spawn path instead
    (:func:`_spawn_and_dispatch`): it registers a role-matched
    :class:`~eawf.kernel.state.models.AgentSession`, renders the prompt,
    resolves the runtime adapter, ``await``s its
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
    (jailed argv + scrubbed env -- the safety floor), prices the spawn,
    and drives the dispatch runner with the registered session id so the
    role-specific ``agent_end`` report emit fires. The returned plan carries
    the real captured ``pid`` + the registered session id.

    On the plan-only and hand-fed-outcome paths the plan is not persisted
    to ``state.json`` and no subprocess is spawned; only the live-spawn
    path registers a session + forks a child.

    Args:
        ctx: Server context. Needs ``state_path`` to resolve the wave's
            ``runtime_preference`` and ``event_path`` (+ ``bus``) to emit
            the dispatch events; both are omitted in unit tests. The
            live-spawn path requires both.
        params: JSON-RPC params per :class:`DispatchParams`.

    Returns:
        Dict matching :class:`DispatchPlan`.

    Raises:
        ValueError: When ``session_policy="continue"`` is requested
            (the V8 continue path is deferred); when both ``spawn`` and
            ``outcome`` are supplied (a live spawn derives its own
            outcome); when a ``skill_manifest`` is supplied and the
            ``runtime`` override is not in its ``runtime`` list
            (:class:`~eawf.runtime.runtimes.dispatch.AdapterManifestMismatchError`);
            when no manifest-listed runtime resolves to an adapter
            (:class:`~eawf.runtime.runtimes.dispatch.AdapterResolutionError`);
            or when an ``outcome`` carries a ``primary_error`` without a
            ``fallback_runtime``. The server maps all of these to
            ``-32602 invalid params``.
        LiveSpawnError: When ``spawn=True`` but the daemon context lacks
            ``state_path`` or ``event_path`` (the live spawn cannot
            register a session + sink its events without both).
    """
    args = DispatchParams.model_validate(params)
    if args.session_policy == "continue":
        raise ValueError(
            f"session_policy={args.session_policy!r} not implemented in W07 "
            "(--continue resume + V5 fallback are deferred to a later phase)"
        )
    if args.spawn and args.outcome is not None:
        raise ValueError(
            "spawn and outcome are mutually exclusive: a live spawn derives its outcome"
        )
    if args.spawn and (ctx.state_path is None or ctx.event_path is None):
        # Live spawn fails fast before runtime resolution: it cannot register
        # a session + sink its events without both on-disk stores.
        raise LiveSpawnError(
            f"spawn=True requires state_path + event_path for wave: {args.wave_id!r}"
        )
    state_path = ctx.state_path
    preference: list[str] | None = None
    if state_path is not None and Path(state_path).exists():
        state = load_state(Path(state_path))
        wave = state.waves.get(args.wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {args.wave_id!r}")
        preference = wave.runtime_preference
        # The fleet drive dispatches waves that carry no per-wave runtime
        # override; fall back to the project's configured runtime preference
        # (the operator's explicit ``runtime.adapters`` choice) so the drive
        # resolves a runtime instead of failing fast. An explicit ``runtime``
        # param still wins inside _pick_runtime; this only fills the gap when
        # neither the param nor the wave names one.
        if not preference:
            preference = _resolve_config_runtime_preference(Path(state_path)) or None
    if args.skill_manifest is not None:
        # Skill -> adapter handshake: the manifest declares which
        # runtimes can host the skill; the daemon picks the highest-
        # preference resolvable one and rejects an off-manifest override.
        # ``AdapterManifestMismatchError`` subclasses ``ValueError`` so
        # the server maps it to -32602 invalid params.
        _adapter, handshake = resolve_adapter(
            manifest=args.skill_manifest,
            preference=preference,
            override=args.runtime,
        )
        runtime = handshake.runtime_id
    else:
        runtime = _pick_runtime(override=args.runtime, preference=preference)
    if args.spawn:
        # Live-spawn path (stores already asserted above): register a session
        # + spawn for real behind the floor + drive the runner with the
        # session id so the agent_end report emit fires.
        plan = await _dispatch_live(ctx, args=args, runtime=runtime)
        logger.info(
            f"dispatch wave={args.wave_id!r} runtime={runtime!r} spawn=live "
            f"pid={plan.pid} session={plan.session_id!r} events={len(plan.event_ids)}"
        )
        return plan.model_dump(mode="json")
    plan = _build_plan(wave_id=args.wave_id, runtime=runtime, state_path=state_path)
    # The dispatch runner emits to ``event.jsonl`` through the canonical
    # writer, so it only runs when an outcome is supplied AND the daemon
    # context is wired to an on-disk event store (unit tests omit both).
    if args.outcome is not None and ctx.event_path is not None:
        event_ids = _emit_dispatch_events(
            ctx,
            wave_id=args.wave_id,
            runtime=runtime,
            outcome=args.outcome,
            trace_request_id=None,
        )
        plan = plan.model_copy(update={"event_ids": event_ids})
    logger.info(
        f"dispatch wave={args.wave_id!r} runtime={runtime!r} "
        f"attempt={plan.attempt} session={plan.session_id!r} events={len(plan.event_ids)}"
    )
    return plan.model_dump(mode="json")


@register("agent.session")
async def session(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Read the typed sessions table off ``state.json`` for a wave.

    When ``attempt`` is supplied the result restricts to just that
    attempt (missing attempts return an empty dict rather than
    raising — V8 may call this on a wave whose attempts have already
    been pruned by the TTL sweep).

    Args:
        ctx: Server context; ``ctx.state_path`` must be configured.
        params: JSON-RPC params per :class:`SessionParams`.

    Returns:
        Dict matching :class:`SessionResult`.

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset (e.g. tests).
        ValueError: When ``wave_id`` is unknown in ``state.json``.
    """
    args = SessionParams.model_validate(params)
    state_path = ctx.state_path
    if state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state = load_state(Path(state_path))
    wave = state.waves.get(args.wave_id)
    if wave is None:
        raise ValueError(f"unknown wave: {args.wave_id!r}")
    if args.attempt is not None:
        match = wave.sessions.get(args.attempt)
        sessions = {args.attempt: match} if match is not None else {}
    else:
        sessions = dict(wave.sessions)
    result = SessionResult(sessions=sessions)
    return result.model_dump(mode="json")


@register("agent.kill")
async def kill(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the wave's live fleet lane then signal its process group.

    Resolves the in-flight lane for the ``(wave_id, attempt)`` pair off the
    :class:`~eawf.kernel.state.models.FleetRun` registry via
    :func:`eawf.runtime.daemon.methods.fleet.kill_lane`, then -- when a live
    killable lane resolves -- signals its ``pgid``: ``signal="kill"`` delivers
    SIGKILL (a hard stop), while ``signal="halt"`` (or the legacy ``"term"``
    alias) delivers SIGTERM (a graceful stop). The signalled lane transitions
    to a killed terminal that deregisters from the registry, and the handler
    returns ``killed=true``. A group already dead at signal time still counts
    as reaped (the kill primitive reports it rather than raising).

    When no live fleet lane resolves (no fleet run armed, or no lane for the
    pair) the kill FALLS BACK to the wave's single-wave dispatched session (W09):
    a wave dispatched via ``eawf dispatch wave`` (no fleet run) records its child
    pid on the matching ``SessionAttempt``, so the kill resolves that pid and
    signals its group -- a single dispatched session is killable even without a
    fleet run. Only when NEITHER a live lane NOR a session pid resolves (or the
    lane / session carries no addressable pid) does the handler return
    ``killed=false`` + a typed ``reason`` and signal nothing, so the response
    never fakes a kill on an unaddressable target.

    Args:
        ctx: Server context; ``ctx.state_path`` carries the lane registry + the
            session table + the deregister write target. A stateless context
            resolves neither, so every kill returns the ``no-fleet-run``
            not-found.
        params: JSON-RPC params per :class:`KillParams`.

    Returns:
        Dict matching :class:`KillResult` -- ``killed=true`` on a signalled
        lane, or ``killed=false`` + ``reason`` on the not-found path.
    """
    args = KillParams.model_validate(params)
    hard = args.signal in _HARD_KILL_SIGNALS
    result = kill_lane(ctx, wave_id=args.wave_id, attempt=args.attempt, hard=hard)
    logger.info(
        f"kill wave={args.wave_id!r} attempt={args.attempt} signal={args.signal!r} "
        f"hard={hard} killed={result.killed} reason={result.reason!r}"
    )
    return KillResult(killed=result.killed, signal=args.signal, reason=result.reason).model_dump(
        mode="json"
    )


def _set_dispatch_paused(ctx: MethodContext, *, paused: bool, repo_root: str | None = None) -> bool:
    """Persist :attr:`~eawf.kernel.state.models.State.dispatch_paused` = *paused*.

    Routes the write through the daemon canonical state/event path:
    acquire the state sibling lock, load the typed state, set the flag,
    stamp ``updated_at``, persist ``state.json``, append a matching
    ``EVENT`` row, then publish that same envelope on the subscription bus.
    Idempotent -- setting the flag to its current value re-writes the same
    payload (only ``updated_at`` advances) and emits a fresh event row.

    Args:
        ctx: Daemon method context — supplies the boot-root ``state_path``
            fallback and the bus.
        paused: The value to persist (``True`` to pause, ``False`` to resume).
        repo_root: Optional per-request repo root; the toggle persists into
            that repo's state/event files (multi-root serve). ``None`` falls
            back to the daemon-bound boot root.

    Returns:
        The persisted flag value (always equal to *paused*).

    Raises:
        RuntimeError: When neither *repo_root* nor ``ctx.state_path``
            resolves a state path (the toggle cannot persist without an
            on-disk state).
    """
    if repo_root:
        state_path = Path(repo_root) / ".ea" / "state.json"
        event_path = store_path(state_path, StoreKind.EVENT)
    elif ctx.state_path is not None:
        state_path = Path(ctx.state_path)
        event_path = (
            Path(ctx.event_path)
            if ctx.event_path is not None
            else store_path(state_path, StoreKind.EVENT)
        )
    else:
        raise RuntimeError("state_path not configured on daemon context")
    command = "agent.pause" if paused else "agent.resume"
    summary = f"{command} dispatch_paused={paused}"
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        before_version = state_version(state.model_dump(mode="json"))
        state.dispatch_paused = paused
        state.updated_at = datetime.now(UTC)
        new_payload = state.model_dump(mode="json")
        after_version = state_version(new_payload)
        atomic_write_json_locked(state_path, new_payload)
        now = datetime.now(UTC)
        args_hash = hashlib.sha256(
            orjson.dumps({"paused": paused}, option=orjson.OPT_SORT_KEYS)
        ).hexdigest()[:16]
        envelope = Envelope(
            schema_version="1.0",
            id=f"EV-{uuid.uuid4().hex[:12]}",
            kind=StoreKind.EVENT,
            scope_id=state.urn,
            created_at=now,
            updated_at=None,
            summary=summary,
            payload=EventPayload(
                timestamp=now,
                event_type=f"state.mutate.{command}",
                event_kind="state_mutated",
                actor="daemon",
                command=command,
                args_hash=args_hash,
                before_state_version=before_version,
                after_state_version=after_version,
                status="ok",
                message=summary,
                extras={"dispatch_paused": paused},
            ).model_dump(mode="json"),
            blob_refs=[],
            artifact_ids=[],
        )
        append_envelope(event_path, envelope)
    cross_root = (
        ctx.state_path is not None and state_path.resolve() != Path(ctx.state_path).resolve()
    )
    if not cross_root and ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id
    logger.info(f"_set_dispatch_paused paused={paused} envelope_id={envelope.id!r}")
    return paused


@register("agent.pause")
async def pause(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Pause dispatch by persisting ``state.dispatch_paused = True``.

    A deliberate operator stop: while the flag is set,
    :func:`eawf.workflow.lifecycle.wave.claim_wave` rejects every claim
    (regardless of ``out_of_order``) until ``agent.resume`` clears it. The
    flag is written through the daemon canonical state writer; the call is
    idempotent (pausing an already-paused state re-writes the same flag).

    Args:
        ctx: Server context; ``ctx.state_path`` must be configured.
        params: JSON-RPC params per :class:`PauseParams`.

    Returns:
        Dict matching :class:`PauseResult` with ``paused=true``.

    Raises:
        RuntimeError: When neither ``repo_root`` nor ``ctx.state_path``
            resolves a state path.
    """
    args = PauseParams.model_validate(params)
    note_cross_root_serve(ctx, repo_root=args.repo_root, command="dispatch pause")
    paused = _set_dispatch_paused(ctx, paused=True, repo_root=args.repo_root)
    return PauseResult(paused=paused).model_dump(mode="json")


@register("agent.resume")
async def resume(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resume dispatch by persisting ``state.dispatch_paused = False``.

    Clears the deliberate operator stop ``agent.pause`` set, so
    :func:`eawf.workflow.lifecycle.wave.claim_wave` accepts claims again.
    The flag is written through the daemon canonical state writer; the call
    is idempotent (resuming an already-running state re-writes the same
    flag).

    Args:
        ctx: Server context; ``ctx.state_path`` must be configured.
        params: JSON-RPC params per :class:`PauseParams` (parameterless).

    Returns:
        Dict matching :class:`PauseResult` with ``paused=false``.

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset (e.g. tests).
    """
    args = PauseParams.model_validate(params)
    note_cross_root_serve(ctx, repo_root=args.repo_root, command="dispatch resume")
    paused = _set_dispatch_paused(ctx, paused=False, repo_root=args.repo_root)
    return PauseResult(paused=paused).model_dump(mode="json")
