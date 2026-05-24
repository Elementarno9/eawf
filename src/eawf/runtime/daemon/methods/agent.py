"""``agent.*`` JSON-RPC methods: dispatch / session / kill.

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
writer. This is the production caller for the dispatch runner. The live
subprocess spawn + token *metering* that will populate the outcome
automatically — plus the ``state.mutate`` ``AddSessionAttempt`` wiring —
still lands later; until then the method does **not** spawn a subprocess
and the caller hands the metering in. When no outcome is supplied (or no
``event_path`` is configured, as in unit tests) the method computes the
plan and returns it without emitting events.

``agent.session`` is a read-only inspection helper that returns the
typed session table from ``state.json`` for a wave.

``agent.kill`` is a placeholder that returns ``killed=false`` +
``signal="term"``; a later wave wires the real subprocess-signalling
ladder (SIGTERM grace window then SIGKILL on POSIX, ``TerminateProcess``
on Windows).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import DispatchNote
from eawf.kernel.state.models import DispatchAnnotation, SessionAttempt
from eawf.kernel.store.kinds.events.base import RuntimeTriple
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.observability.telemetry.pricing import PRICING_VERSION
from eawf.runtime.daemon.dispatch_runner import DispatchTokens, run_dispatch
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.runtimes.dispatch import resolve_adapter
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.workflow.evidence._io import load_state

logger = logging.getLogger(__name__)


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
#: runs end-to-end in W07; ``"continue"`` is rejected with -32602
#: invalid params, and ``"hybrid"`` falls through to the fresh path
#: (since no prior attempts exist for the wave).
SessionPolicy = Literal["fresh", "continue", "hybrid"]

#: Signals accepted by :func:`kill`. Default ``"term"`` maps to
#: SIGTERM; ``"kill"`` maps to SIGKILL (POSIX) /
#: ``TerminateProcess`` (Windows).
KillSignal = Literal["term", "kill"]


class DispatchOutcome(BaseModel):
    """Post-dispatch metering the caller hands :func:`dispatch`.

    The live subprocess spawn + token metering that will populate this
    automatically lands in a later wave; until then the caller supplies
    the served model + token tally + priced cost, plus an optional
    error-driven fallback. When :attr:`primary_error` is set the
    dispatch runner emits a ``runtime_switched`` event and the
    :attr:`fallback_runtime` serves the dispatch; the cost is always
    billed against the serving attempt.

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
            stays plan-only.
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str = Field(min_length=1)
    runtime: str | None = None
    session_policy: SessionPolicy = "fresh"
    skill_manifest: SkillManifest | None = None
    outcome: DispatchOutcome | None = None


class DispatchPlan(BaseModel):
    """Dispatch-plan result.

    A later wave turns this payload into an ``AddSessionAttempt``
    mutation against ``state.json`` + the real subprocess spawn. The
    daemon returns the plan so callers can exercise the fresh-path shape
    (attempt number, session id, typed annotation + attempt rows).

    When the caller supplies a :class:`DispatchOutcome` and the daemon
    context carries an ``event_path``, :attr:`event_ids` carries the ids
    of the C09 envelopes
    :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` emitted (the
    ``runtime_switched`` envelope first when a V5 fallback fired, then
    ``dispatch_cost``); it is empty otherwise.

    Attributes:
        session_id: UUID-v4 session id minted for this dispatch.
        attempt: 1-based attempt number for the wave.
        pid: Subprocess PID once spawned (``0`` until the spawn lands).
        runtime: Resolved runtime adapter id (plugin spelling).
        annotation: Typed dispatch annotation for the attempt.
        session_attempt: Typed session-attempt row a later wave persists.
        event_ids: Ids of the C09 envelopes emitted to the live event
            log, in append order; empty when no outcome was supplied.
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
    signal: KillSignal = "term"


class KillResult(BaseModel):
    """Result of :func:`kill` (placeholder until a later wave wires real signalling)."""

    model_config = ConfigDict(extra="forbid")
    killed: bool
    signal: KillSignal


def _pick_runtime(*, override: str | None, preference: list[str] | None) -> str:
    """Pick the runtime adapter id for a dispatch.

    Args:
        override: Caller-supplied ``runtime`` param. Wins when set.
        preference: ``Wave.runtime_preference`` from state.

    Returns:
        Adapter id string.

    Raises:
        ValueError: When neither *override* nor *preference* yields a
            runtime. (W08 layers a daemon-config default in front of
            this; W07 fails fast so the operator notices the gap.)
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
        A :class:`DispatchPlan` carrying the typed annotation and
        session-attempt payload W09 will persist.

    Raises:
        ValueError: When *wave_id* is unknown in the on-disk state.
    """
    session_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    # The opaque handle here is the URN-form sentinel for the plan; W09
    # will substitute the daemon-registered handle once the dispatcher
    # opens the runtime subprocess and resolves the session-log path.
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

    The plan is **not yet** persisted to ``state.json`` and the
    subprocess is **not yet** spawned — a later wave wires both plus the
    live token metering that will populate the outcome automatically.

    Args:
        ctx: Server context. Needs ``state_path`` to resolve the wave's
            ``runtime_preference`` and ``event_path`` (+ ``bus``) to emit
            the dispatch events; both are omitted in unit tests.
        params: JSON-RPC params per :class:`DispatchParams`.

    Returns:
        Dict matching :class:`DispatchPlan`.

    Raises:
        ValueError: When ``session_policy="continue"`` is requested
            (the V8 continue path is deferred); when a
            ``skill_manifest`` is supplied and the ``runtime`` override
            is not in its ``runtime`` list
            (:class:`~eawf.runtime.runtimes.dispatch.AdapterManifestMismatchError`);
            when no manifest-listed runtime resolves to an adapter
            (:class:`~eawf.runtime.runtimes.dispatch.AdapterResolutionError`);
            or when an ``outcome`` carries a ``primary_error`` without a
            ``fallback_runtime``. The server maps all of these to
            ``-32602 invalid params``.
    """
    args = DispatchParams.model_validate(params)
    if args.session_policy == "continue":
        raise ValueError(
            f"session_policy={args.session_policy!r} not implemented in W07 "
            "(--continue resume + V5 fallback are deferred to a later phase)"
        )
    state_path = ctx.state_path
    preference: list[str] | None = None
    if state_path is not None and Path(state_path).exists():
        state = load_state(Path(state_path))
        wave = state.waves.get(args.wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {args.wave_id!r}")
        preference = wave.runtime_preference
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
    """Placeholder kill — W09 wires real subprocess signalling.

    The handler validates params (so callers can shake out the shape
    today) and returns ``killed=false`` + the requested signal so the
    response is forensically obvious. W09 replaces the placeholder with
    the SIGTERM→SIGKILL ladder on POSIX and ``TerminateProcess`` on
    Windows.

    Args:
        ctx: Server context (unused in W07; W09 reads
            ``ctx.dispatcher`` for the live subprocess map).
        params: JSON-RPC params per :class:`KillParams`.

    Returns:
        Dict matching :class:`KillResult` with ``killed=false``.
    """
    args = KillParams.model_validate(params)
    logger.info(
        f"kill wave={args.wave_id!r} attempt={args.attempt} signal={args.signal!r} placeholder=true"
    )
    result = KillResult(killed=False, signal=args.signal)
    return result.model_dump(mode="json")
