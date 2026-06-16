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
metered outcome, the daemon registers an executor
:class:`~eawf.kernel.state.models.AgentSession` through the canonical
state writer, renders the dispatch prompt, resolves the runtime adapter,
and ``await``s its :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
(which jails the argv + scrubs the child env -- the safety floor). The
spawn's :class:`~eawf.runtime.runtimes.adapter.SpawnResult` is priced via
:func:`eawf.runtime.runtimes.metering.price_spawn_result`, then
:func:`run_dispatch` is driven with the **registered session id** so its
``agent_end`` executor-report emit fires. The returned
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
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.layered import merge_config, resolve_runtime_tier_models
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AgentSessionStatus,
    Confidence,
    DispatchNote,
    EffortBucket,
    StoreKind,
)
from eawf.kernel.state.io import state_version
from eawf.kernel.state.models import DispatchAnnotation, SessionAttempt, State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportFollowup, ExecutorReportBody
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.events.base import RuntimeTriple
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.observability.telemetry.pricing import PRICING_VERSION
from eawf.runtime.budget.policy import DEFAULT_ENFORCE, EnforceMode
from eawf.runtime.daemon.dispatch_runner import (
    DispatchResult,
    DispatchTokens,
    _build_completion_body,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.fleet import kill_lane
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.adapter import ErrorClass, RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.dispatch import resolve_adapter
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.runtime.runtimes.metering import price_spawn_result
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.runtime.runtimes.selector import select_adapter
from eawf.runtime.sandbox.policy import resolve_denied_tools
from eawf.runtime.session.store import SessionConflict, start_session
from eawf.workflow.dispatch.llm_assist import LLMAssistError, assist_with_schema
from eawf.workflow.dispatch.renderer import render_dispatch_envelope
from eawf.workflow.dispatch.retry import spawn_with_retry
from eawf.workflow.dispatch.routing import resolve_routing, runtime_model_for_decision
from eawf.workflow.evidence._io import load_state

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
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str = Field(min_length=1)
    runtime: str | None = None
    session_policy: SessionPolicy = "fresh"
    skill_manifest: SkillManifest | None = None
    outcome: DispatchOutcome | None = None
    spawn: bool = False
    model: str | None = None


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
        annotation: Typed dispatch annotation for the attempt.
        session_attempt: Typed session-attempt row for the dispatch (the
            live-spawn path persists it on the registered session).
        event_ids: Ids of the C09 envelopes emitted to the live event
            log, in append order; empty when no outcome was supplied and
            no live spawn ran. On the live-spawn path these are the
            runner's emitted ids, including the report-driven events.
    """

    model_config = ConfigDict(extra="forbid")
    session_id: str
    attempt: int
    pid: int
    runtime: str
    annotation: DispatchAnnotation
    session_attempt: SessionAttempt
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

    Both methods are parameterless toggles of the durable
    :attr:`~eawf.kernel.state.models.State.dispatch_paused` flag, so the
    model carries no fields; it exists to enforce ``extra="forbid"`` so a
    caller that ships a stray key is rejected rather than silently
    ignored.
    """

    model_config = ConfigDict(extra="forbid")


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
#: ``effort_bucket``. The live-spawn lane is the executor lane, and a
#: medium-effort default keeps the resolved model on the mid pricing tier
#: rather than the costlier Opus tier when the wave carries no effort hint.
_DEFAULT_SPAWN_ROLE: AgentSessionRole = AgentSessionRole.EXECUTOR
_DEFAULT_SPAWN_EFFORT: EffortBucket = EffortBucket.M


def _validate_executor_body(raw: object) -> ExecutorReportBody:
    """Force the spawned executor's JSON-decoded output to an ``ExecutorReportBody``.

    The forced-schema validator the live-spawn lane hands
    :func:`~eawf.workflow.dispatch.llm_assist.assist_with_schema`. The
    executor lane forces the narrow
    :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody`
    (rather than the whole ``agent_end`` union) so a spawn that emits a
    non-executor body — or omits the executor-required ``wave_id`` /
    ``outcome`` — is rejected and re-asked, not silently accepted as a
    different role's body.

    Args:
        raw: JSON-decoded spawn output (the assist loop decodes the
            ``text`` before handing it here).

    Returns:
        The validated :class:`ExecutorReportBody`.

    Raises:
        pydantic.ValidationError: When *raw* does not satisfy the executor
            report-body schema (the assist loop catches this to re-ask).
    """
    return ExecutorReportBody.model_validate(raw)


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


def _persist_live_session_attempt(
    ctx: MethodContext,
    *,
    wave_id: str,
    requested_runtime: str,
    serving_runtime: str,
    session_id: str,
    session_log_handle: str,
    spawn_result: SpawnResult,
    pid: int,
) -> tuple[int, DispatchAnnotation, SessionAttempt]:
    """Persist the live spawn attempt, including the pid used for kill/budget."""
    if ctx.state_path is None:
        raise LiveSpawnError(f"live spawn requires state_path for wave: {wave_id!r}")
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        wave = state.waves.get(wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {wave_id!r}")
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
            session_id=session_id,
            session_log_handle=session_log_handle,
            started_at=spawn_result.started_at,
            ended_at=spawn_result.ended_at,
            exit_status=spawn_result.exit_status,
            subprocess_pid=pid,
            cache_creation_input_tokens=spawn_result.cache_creation_input_tokens,
            cache_read_input_tokens=spawn_result.cache_read_input_tokens,
            input_tokens=spawn_result.input_tokens,
            output_tokens=spawn_result.output_tokens,
        )
        wave.sessions[attempt] = session_attempt
        wave.dispatch_history.append(annotation)
        state.updated_at = now
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(
        f"_persist_live_session_attempt wave={wave_id} attempt={attempt} "
        f"runtime={serving_runtime!r} pid={pid}"
    )
    return attempt, annotation, session_attempt


def _register_executor_session(
    ctx: MethodContext,
    *,
    wave_id: str,
    runtime: str,
) -> str:
    """Register (or reuse) an ACTIVE executor session for *wave_id*.

    Opens a new :class:`~eawf.kernel.state.models.AgentSession` with role
    :attr:`~eawf.kernel.state.enums.AgentSessionRole.EXECUTOR`,
    ``scope_id=wave_id``, and the resolved *runtime* via
    :func:`eawf.runtime.session.store.start_session`, persisting it through
    the daemon canonical state writer (``portalock`` + locked atomic
    write, mirroring :func:`~eawf.runtime.daemon.dispatch_runner._mark_wave_in_progress`).
    The session-start event is appended to ``event.jsonl`` by the store.

    When an ACTIVE session already exists for ``(wave_id, runtime)`` the
    store raises :class:`~eawf.runtime.session.store.SessionConflict`; this
    helper catches it and reuses the existing session id rather than
    surfacing the error, so a re-dispatch of the same lane threads the
    live ``agent_end`` report onto the already-open session.

    Args:
        ctx: Daemon method context — supplies ``state_path`` + ``event_path``.
        wave_id: ``W<NN>`` wave the executor session scopes to.
        runtime: Resolved runtime adapter id (plugin spelling) recorded on
            the session row.

    Returns:
        The canonical :class:`~eawf.kernel.state.models.AgentSession` id to
        thread into the dispatch runner.

    Raises:
        LiveSpawnError: When ``ctx.state_path`` or ``ctx.event_path`` is
            unset (the live spawn cannot register a session without both).
    """
    if ctx.state_path is None or ctx.event_path is None:
        raise LiveSpawnError(f"live spawn requires state_path + event_path for wave: {wave_id!r}")
    state_path = Path(ctx.state_path)
    events_path = Path(ctx.event_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        before_version = state_version(state.model_dump(mode="json"))
        try:
            result = start_session(
                state=state,
                events_path=events_path,
                role=AgentSessionRole.EXECUTOR,
                scope_id=wave_id,
                runtime=runtime,
            )
        except SessionConflict as exc:
            # An ACTIVE executor session already scopes this (wave, runtime)
            # pair. Reuse it so the live report threads onto the open session
            # rather than failing the re-dispatch. No state write is needed.
            existing = _find_active_executor(state, wave_id=wave_id, runtime=runtime)
            if existing is None:
                raise LiveSpawnError(
                    f"session conflict but no active executor session found: {wave_id!r}"
                ) from exc
            logger.info(
                f"_register_executor_session reuse wave={wave_id} "
                f"runtime={runtime!r} session={existing!r}"
            )
            return existing
        session_id = result.session.id
        state.updated_at = datetime.now(UTC)
        new_payload = state.model_dump(mode="json")
        after_version = state_version(new_payload)
        atomic_write_json_locked(state_path, new_payload)
    logger.info(
        f"_register_executor_session wave={wave_id} runtime={runtime!r} "
        f"session={session_id!r} before={before_version} after={after_version}"
    )
    return session_id


def _find_active_executor(state: State, *, wave_id: str, runtime: str) -> str | None:
    """Return the id of an ACTIVE executor session for the pair, or ``None``.

    Mirrors the ``(scope_id, runtime)`` uniqueness key the session store
    enforces, narrowed to the executor role so the live-spawn reuse path
    resolves the exact session the store's
    :class:`~eawf.runtime.session.store.SessionConflict` refers to.

    Args:
        state: Validated state snapshot carrying ``agent_sessions``.
        wave_id: The session scope id to match.
        runtime: The session runtime to match (plugin spelling).

    Returns:
        The matching session id, or ``None`` when no ACTIVE executor
        session scopes the pair.
    """
    for sess in state.agent_sessions.values():
        if (
            sess.role is AgentSessionRole.EXECUTOR
            and sess.scope_id == wave_id
            and sess.runtime == runtime
            and sess.status is AgentSessionStatus.ACTIVE
        ):
            return sess.id
    return None


async def _bind_executor_report(
    accepted: SpawnResult,
    *,
    prompt: str,
    serving_runtime: str,
    spawn_once: Callable[[str], Awaitable[SpawnResult]],
) -> ExecutorReportBody:
    """Bind the spawned executor's real output to a validated report body.

    Drives :func:`~eawf.workflow.dispatch.llm_assist.assist_with_schema`
    over the spawned agent's OWN ``text`` to populate an
    :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody`,
    replacing the runner's synthetic placeholder. The first assist spawn
    reuses *accepted* (the already-completed, already-priced spawn) so the
    initial attempt is not double-spawned; each subsequent re-ask drives a
    fresh spawn of the correction prompt via *spawn_once* on the serving
    runtime. The assist loop's correction notice names the validation
    failure, and on ceiling-exhaustion the loop raises
    :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError` — no
    synthetic body is ever persisted on a parse failure.

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
        The validated :class:`ExecutorReportBody` parsed from the agent's
        own output.

    Raises:
        eawf.workflow.dispatch.llm_assist.LLMAssistError: When every spawn
            (the reused initial plus the re-asks, up to the loop's ceiling)
            produced output that failed the executor report-body schema.
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
        validator=_validate_executor_body,
    )
    body = result.body
    logger.info(
        f"_bind_executor_report runtime={serving_runtime!r} "
        f"attempts={result.attempts_used} verdict={body.verdict.value}"
    )
    # The forced validator is ExecutorReportBody, so the assist loop's body
    # is by construction an executor body; narrow for the typed return.
    if not isinstance(body, ExecutorReportBody):  # pragma: no cover - validator forces this
        raise TypeError(f"assist returned non-executor body: {body.role!r}")
    return body


def _synthesize_executor_report(
    accepted: SpawnResult, *, wave_id: str, exc: LLMAssistError
) -> ExecutorReportBody:
    """Synthesize a typed executor body when the assist loop exhausts its re-asks.

    The safety net for the headless dispatch path: when the spawned agent's
    output never validates against the executor report schema (a model that
    answers in prose re-asks to the ceiling and raises
    :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError`), the wave would
    otherwise hang -- :func:`run_dispatch` never runs, so the dispatch-cost emit
    and EU accrual never fire even though the spawn already spent real cost.
    Rather than let the exception escape, this builds a typed
    :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody` from the
    accepted spawn's observable outcome signals so the dispatch always completes.

    The verdict mirrors the accepted spawn's exit status (``exit_status == 0``
    is :attr:`AgentReportVerdict.PASS`, else :attr:`AgentReportVerdict.FAIL`),
    the confidence is :attr:`Confidence.MEDIUM` (the report was synthesized, not
    authored), and a follow-up names the parse failure so the degrade is
    auditable. The body reuses the runner's
    :func:`~eawf.runtime.daemon.dispatch_runner._build_completion_body` so the
    synthetic path mints the same typed shape as the rich-output path.

    Args:
        accepted: The already-completed, already-priced spawn whose exit status
            drives the synthesized verdict.
        wave_id: The wave the synthesized report scopes.
        exc: The exhausted-assist error carrying the attempt ceiling and the
            ordered rejection trail (the last failure's ``reason`` is named in
            the synthesized prose + the follow-up).

    Returns:
        A typed :class:`ExecutorReportBody` carrying the synthesized verdict,
        MEDIUM confidence, and one parse-failure follow-up.
    """
    verdict = AgentReportVerdict.PASS if accepted.exit_status == 0 else AgentReportVerdict.FAIL
    last_reason = exc.failures[-1].reason if exc.failures else "unknown"
    # Bound to the executor body's outcome cap (1000); the summary cap (4000)
    # is wider, so a string that fits outcome fits both.
    outcome = (
        f"synthesized executor report: agent output failed report-body validation "
        f"after {exc.attempts} attempt(s) (last reason: {last_reason}); "
        f"spawn exit_status={accepted.exit_status}"
    )[:1000]
    # The executor body's commit_sha is min_length=7-or-None, so the builder
    # cannot mint an empty placeholder; pass a 7-char sentinel to satisfy the
    # builder, then drop it to None below (no commit landed on this path).
    built = _build_completion_body(
        role=AgentSessionRole.EXECUTOR,
        wave_id=wave_id,
        commit_sha="0000000",
        outcome=outcome,
        files_changed=[],
        tests_run=[],
        verdict=verdict,
        confidence=Confidence.MEDIUM,
    )
    # The EXECUTOR role forces an ExecutorReportBody; narrow for the typed copy.
    if not isinstance(built, ExecutorReportBody):  # pragma: no cover - role forces this
        raise TypeError(f"completion builder returned non-executor body: {built.role!r}")
    # Drop the sentinel commit_sha (no commit landed) and attach the
    # parse-failure follow-up so the degrade is auditable.
    followup = AgentReportFollowup(
        title="executor output failed report-body validation; report synthesized",
        owner_role=AgentSessionRole.EXECUTOR,
        priority="P1",
        detail=(
            f"the spawned executor answered with output that failed the "
            f"ExecutorReportBody schema after {exc.attempts} attempt(s) "
            f"(last reason: {last_reason}); the report body was synthesized from "
            f"the spawn exit status so cost + EU still accrue"
        )[:500],
    )
    return built.model_copy(update={"commit_sha": None, "followups": [followup]})


async def _spawn_and_dispatch(
    ctx: MethodContext,
    *,
    wave_id: str,
    runtime: str,
    model_override: str | None,
    trace_request_id: str | None,
) -> DispatchPlan:
    """Run the live-spawn dispatch path and return the resulting plan.

    Registers the executor session, renders the prompt, resolves sandbox policy
    and retry preferences, spawns behind the runtime floor, validates the
    executor's own output into a report body, persists the wave-local attempt
    row, then drives :func:`run_dispatch` with the live pid/pgid and config
    enforcement mode.

    On report-schema exhaustion the spawned agent's output never validates
    against the executor report body (e.g. a model that answers in prose).
    Rather than let :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError`
    escape -- which would strand the wave with cost already spent but
    :func:`run_dispatch` never run -- the path synthesizes a typed
    :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody` from the
    accepted spawn's exit status (see :func:`_synthesize_executor_report`) so the
    dispatch always completes and the dispatch-cost + EU accrual still fire.

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

    # 1. Register the executor session (canonical state writer) so the
    # dispatch runner has a session row to use as report authority.
    session_id = _register_executor_session(ctx, wave_id=wave_id, runtime=runtime)

    # 2. Resolve the per-runtime model for the first spawn + render the prompt.
    # The model is selected for the resolved runtime so a codex / opencode spawn
    # runs its own vendor's model rather than a claude id the foreign CLI
    # rejects. A V5 switch re-resolves the model for the switched runtime inside
    # the spawn closure (below), so each runtime always spawns its own model.
    model = _resolve_spawn_model(
        state_path, wave_id=wave_id, runtime=runtime, override=model_override
    )
    state = load_state(state_path)
    envelope = render_dispatch_envelope(state, wave_id, runtime, repo_root=state_path.parent.parent)

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

    # 7. Bind the spawned agent's OWN output to a validated ExecutorReportBody
    # through the bounded re-ask loop. The first assist spawn reuses the
    # already-completed accepted spawn_result (no double-spawn of the initial
    # attempt); each re-ask drives a fresh spawn of the correction prompt on
    # the serving runtime. On ceiling-exhaustion the loop raises LLMAssistError
    # and NO synthetic body is persisted — the report carries the agent's
    # words or nothing.
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

    try:
        report_body = await _bind_executor_report(
            spawn_result,
            prompt=envelope.prompt,
            serving_runtime=serving_runtime,
            spawn_once=_spawn_correction,
        )
    except LLMAssistError as exc:
        # The spawned agent's output never validated against the executor report
        # schema (e.g. a model that answers in prose). Letting the error escape
        # would strand the wave -- run_dispatch never runs, so the dispatch-cost
        # emit + EU accrual never fire even though the spawn already spent real
        # cost. Synthesize a typed body from the accepted spawn's exit status so
        # the dispatch always completes and the degrade is auditable.
        report_body = _synthesize_executor_report(spawn_result, wave_id=wave_id, exc=exc)
        logger.info(
            f"_spawn_and_dispatch wave={wave_id} status=synth-fallback "
            f"attempts={exc.attempts} reason={exc.failures[-1].reason!r}"
        )
    attempt, annotation, session_attempt = _persist_live_session_attempt(
        ctx,
        wave_id=wave_id,
        requested_runtime=runtime,
        serving_runtime=serving_runtime,
        session_id=session_id,
        session_log_handle=adapter.session_log_handle(spawn_result.session_id),
        spawn_result=spawn_result,
        pid=pid,
    )
    enforce = _resolve_budget_enforce(state_path)

    # 8. Drive the runner with the registered session id + the validated body
    # so the ``agent_end`` executor-report emit persists the agent's own
    # outcome / files_changed / verdict. The serving runtime is the one the
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
    (:func:`_spawn_and_dispatch`): it registers an executor
    :class:`~eawf.kernel.state.models.AgentSession`, renders the prompt,
    resolves the runtime adapter, ``await``s its
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`
    (jailed argv + scrubbed env -- the safety floor), prices the spawn,
    and drives the dispatch runner with the registered session id so the
    ``agent_end`` executor-report emit fires. The returned plan carries
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
        plan = await _spawn_and_dispatch(
            ctx,
            wave_id=args.wave_id,
            runtime=runtime,
            model_override=args.model,
            trace_request_id=None,
        )
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


def _set_dispatch_paused(ctx: MethodContext, *, paused: bool) -> bool:
    """Persist :attr:`~eawf.kernel.state.models.State.dispatch_paused` = *paused*.

    Routes the write through the daemon canonical state/event path:
    acquire the state sibling lock, load the typed state, set the flag,
    stamp ``updated_at``, persist ``state.json``, append a matching
    ``EVENT`` row, then publish that same envelope on the subscription bus.
    Idempotent -- setting the flag to its current value re-writes the same
    payload (only ``updated_at`` advances) and emits a fresh event row.

    Args:
        ctx: Daemon method context — supplies ``state_path``.
        paused: The value to persist (``True`` to pause, ``False`` to resume).

    Returns:
        The persisted flag value (always equal to *paused*).

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset (the toggle cannot
            persist without an on-disk state).
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    event_path = (
        Path(ctx.event_path)
        if ctx.event_path is not None
        else store_path(state_path, StoreKind.EVENT)
    )
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
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
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
        params: JSON-RPC params per :class:`PauseParams` (parameterless).

    Returns:
        Dict matching :class:`PauseResult` with ``paused=true``.

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset (e.g. tests).
    """
    PauseParams.model_validate(params)
    paused = _set_dispatch_paused(ctx, paused=True)
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
    PauseParams.model_validate(params)
    paused = _set_dispatch_paused(ctx, paused=False)
    return PauseResult(paused=paused).model_dump(mode="json")
