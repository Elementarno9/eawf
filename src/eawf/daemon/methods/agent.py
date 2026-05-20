"""``agent.*`` JSON-RPC methods: dispatch / session / kill.

W07 wired only the **fresh-dispatch** path of ``agent.dispatch``. P26
C04d (W13) layers the **skill -> adapter handshake** on top: when a
caller supplies a :class:`~eawf.runtimes.plugin_manifest.SkillManifest`
the dispatcher runs :func:`eawf.runtimes.dispatch.resolve_adapter` to
pick the highest-preference runtime that the skill manifest can host
*and* that resolves to a concrete adapter (rejecting an off-manifest
``runtime`` override per C04d F-d01). The complementary V5 reactive
switchover + V8 ``--continue`` fall-through *policy* lives in
:mod:`eawf.runtimes.fallback`; the live subprocess spawn + ``state.mutate``
``AddSessionAttempt`` wiring still lands later. Until that lands the
method does **not** mutate state and does **not** spawn a subprocess —
it computes the plan and returns it.

``agent.session`` is a read-only inspection helper that returns the
typed session table from ``state.json`` for a wave.

``agent.kill`` is a placeholder that returns ``killed=false`` +
``signal="term"``; W09 wires the real subprocess-signalling ladder
(SIGTERM grace window then SIGKILL on POSIX, ``TerminateProcess`` on
Windows).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.daemon.methods import MethodContext, register
from eawf.evidence._io import load_state
from eawf.runtimes.dispatch import resolve_adapter
from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.state.enums import DispatchNote
from eawf.state.models import DispatchAnnotation, SessionAttempt

logger = logging.getLogger(__name__)


#: Session-policy values accepted by :func:`dispatch`. Only ``"fresh"``
#: runs end-to-end in W07; ``"continue"`` is rejected with -32602
#: invalid params, and ``"hybrid"`` falls through to the fresh path
#: (since no prior attempts exist for the wave).
SessionPolicy = Literal["fresh", "continue", "hybrid"]

#: Signals accepted by :func:`kill`. Default ``"term"`` maps to
#: SIGTERM; ``"kill"`` maps to SIGKILL (POSIX) /
#: ``TerminateProcess`` (Windows).
KillSignal = Literal["term", "kill"]


class DispatchParams(BaseModel):
    """Params for :func:`dispatch`.

    Attributes:
        wave_id: Wave to dispatch against; must exist in ``state.json``.
        runtime: Optional runtime adapter override. When omitted, the
            dispatcher picks
            :class:`~eawf.state.models.Wave.runtime_preference[0]`
            (W08 also wires a daemon-config default). When *skill_manifest*
            is supplied, this override is validated against the manifest
            ``runtime`` list (C04d F-d01).
        session_policy: V8 dispatch policy. Only ``"fresh"`` runs in
            W07 (the spec defers ``"continue"`` / V5 fallback to a
            later phase).
        skill_manifest: Optional per-skill manifest (C04b
            :class:`~eawf.runtimes.plugin_manifest.SkillManifest`). When
            present the dispatcher runs the C04d skill -> adapter
            handshake — it picks the highest-preference runtime that is
            both hostable by the skill manifest *and* resolvable to an
            adapter, and rejects an off-manifest *runtime* override
            (F-d01). When omitted the legacy override-or-preference pick
            (:func:`_pick_runtime`) runs unchanged.
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str = Field(min_length=1)
    runtime: str | None = None
    session_policy: SessionPolicy = "fresh"
    skill_manifest: SkillManifest | None = None


class DispatchPlan(BaseModel):
    """W07 dispatch-plan result.

    W09 turns this payload into an ``AddSessionAttempt`` mutation
    against ``state.json`` + the real subprocess spawn. Until W09
    lands the daemon returns this plan unchanged so callers can
    exercise the fresh-path shape without state mutation.
    """

    model_config = ConfigDict(extra="forbid")
    session_id: str
    attempt: int
    pid: int
    runtime: str
    annotation: DispatchAnnotation
    session_attempt: SessionAttempt


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
    """Result of :func:`kill` (placeholder until W09 wires real signalling)."""

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

    note = DispatchNote.FRESH_DISPATCH if runtime_from is None else DispatchNote.SWITCH_ON_ERROR
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
    """Build a fresh-dispatch plan for the requested wave.

    The plan is **not yet** persisted to ``state.json`` and the
    subprocess is **not yet** spawned — W09 wires both. W07 returns the
    plan unchanged so the wave shape (attempt number, session id,
    typed annotation + attempt rows) is exercised by callers and
    snapshot tests.

    Args:
        ctx: Server context. ``ctx.event_path`` is consulted only by
            sibling methods; this one needs ``state_path`` (passed via
            ``ctx`` attribute or omitted in W07 unit tests).
        params: JSON-RPC params per :class:`DispatchParams`.

    Returns:
        Dict matching :class:`DispatchPlan`.

    Raises:
        ValueError: When ``session_policy="continue"`` is requested
            (the V8 continue path is deferred); when a
            ``skill_manifest`` is supplied and the ``runtime`` override
            is not in its ``runtime`` list (C04d F-d01,
            :class:`~eawf.runtimes.dispatch.AdapterManifestMismatchError`);
            or when no manifest-listed runtime resolves to an adapter
            (:class:`~eawf.runtimes.dispatch.AdapterResolutionError`).
            The server maps all of these to ``-32602 invalid params``.
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
        # C04d skill -> adapter handshake: the manifest declares which
        # runtimes can host the skill; the daemon picks the highest-
        # preference resolvable one and rejects an off-manifest override
        # (F-d01). ``AdapterManifestMismatchError`` subclasses
        # ``ValueError`` so the server maps it to -32602 invalid params.
        _adapter, handshake = resolve_adapter(
            manifest=args.skill_manifest,
            preference=preference,
            override=args.runtime,
        )
        runtime = handshake.runtime_id
    else:
        runtime = _pick_runtime(override=args.runtime, preference=preference)
    plan = _build_plan(wave_id=args.wave_id, runtime=runtime, state_path=state_path)
    logger.info(
        f"dispatch wave={args.wave_id!r} runtime={runtime!r} "
        f"attempt={plan.attempt} session={plan.session_id!r}"
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
