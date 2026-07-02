"""Daemon-owned fleet auto-drain loop -- ``fleet.drive`` RPC + loop runner.

The fleet auto-drain loop claims the ready wave frontier, dispatches each
claimed wave as a live spawn, watches the in-flight lanes to completion, and
advances the frontier unattended until it empties, a budget cap fires, or a
convergence criterion (K consecutive clean rounds) is met. The whole loop is
daemon-owned: every run-state transition is persisted as the typed
:class:`~eawf.kernel.state.models.FleetRun` on the optional ``State.fleet_run``
field through the **daemon canonical state writer** (the
``portalock(state.json)`` + :func:`~eawf.kernel.state.writer.atomic_write_json_locked`
path every other mutator takes, per the daemon-as-sole-mutator rule). The loop
never opens ``state.json`` directly.

The loop honours the durable
:attr:`~eawf.kernel.state.models.State.dispatch_paused` flag: a drive armed
while dispatch is paused claims no wave and stays
:data:`~eawf.kernel.state.models.FleetRunState.IDLE`. An empty ready frontier
refuses to arm with a typed
:class:`~eawf.workflow.lifecycle._errors.LifecycleError` rather than entering a
``DRAINING``-with-zero-lanes state that can never make progress.

Concurrency model. The loop holds at most ``concurrency`` lanes at once. On
each round it fills every free lane from the head of the frontier (claim +
dispatch), then watches the in-flight lanes; a lane that closes clean frees
its slot for the next frontier wave, while a lane that forks (failed /
re-planned) increments the fork counter and resets the convergence streak.

Pause vs halt. A pause-all on a ``DRAINING`` run sets
:data:`~eawf.kernel.state.models.FleetRunState.PAUSED`, claims zero further
waves, and leaves in-flight lanes intact; a resume returns the run to
``DRAINING`` and claiming restarts. A halt-all sets
:data:`~eawf.kernel.state.models.FleetRunState.HALTED`, blocks new claims, and
lets in-flight lanes finish -- distinct from a kill-all, which would reap the
in-flight work. Both transitions persist through the daemon canonical writer.

Determinism. The loop drives spawn + watch through two injectable callables
(:class:`LaneSpawner` + :class:`LaneWatcher`); the daemon wires the live
``agent.dispatch`` spawn + status-poll watch, while tests inject deterministic
fakes so the loop runs without real subprocesses.
# noqa: EAWF010 cohesive fleet-loop surface mid-build across P30-I12 (drive +
lanes/pgid + kill + risk-tier + reattach + budget teeth); the pure budget /
spend helpers split into a sibling module once the loop settles.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.schema import EuBasis
from eawf.kernel.spec.auq_bridge import WaveFrontierItem, compute_ready_frontier
from eawf.kernel.state.enums import RiskTier, StoreKind, WaveStatus
from eawf.kernel.state.models import (
    FleetCounters,
    FleetFork,
    FleetForkReason,
    FleetForkResolution,
    FleetLane,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
    State,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportBody, AgentReportPayload
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.observability.telemetry.join import DEFAULT_EU_MINUTES
from eawf.runtime.daemon.dispatch_runner import (
    SpawnFailureClass,
    classify_spawn_failure,
)
from eawf.runtime.daemon.methods import register
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.adapter import ErrorClass, RuntimeSpawnError
from eawf.runtime.runtimes.cancel import CancelResult, cancel_process_group
from eawf.runtime.runtimes.fallback import (
    FallbackAction,
    fallback_action,
    next_runtime_on_error,
)
from eawf.runtime.runtimes.selector import select_adapter
from eawf.workflow.dispatch.retry import (
    ErrorClassifier,
    RepairExhaustedError,
    RepairSpawnFn,
    RepairVerifier,
    repair_until_resolved,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.spec import WAVE_TRANSITIONS, validate_transition
from eawf.workflow.lifecycle.wave import (
    claim_wave,
    close_wave,
    compute_runtime_delta,
    fail_wave,
)
from eawf.workflow.verify.dispatch_close import (
    CloseGateResult,
    run_close_gates,
    verify_close_readiness,
)
from eawf.workflow.verify.oracle import classify_risk_tier, risk_tier_auto_closes

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec
    from eawf.kernel.store.kinds.evidence import EvidenceRecord
    from eawf.runtime.daemon.methods import MethodContext
    from eawf.runtime.runtimes.adapter import SpawnResult

logger = logging.getLogger(__name__)


@dataclass
class _ActiveDrive:
    """The single in-flight background drive thread + its captured loop -- W01.

    The fleet drain runs SYNCHRONOUSLY (claim -> dispatch -> blocking watch ->
    advance), so it would block the async ``fleet.drive`` RPC handler -- and
    every other RPC on the daemon event loop -- if it ran inside the awaited
    handler. The handler instead starts the drain on this worker thread and
    returns a run handle in under a second; the thread drains in the background
    while the daemon answers concurrent RPCs (``daemon.ping``).

    The captured *loop* is the daemon event loop the RPC handler was running on;
    a bus publish from the drive thread marshals onto it through
    ``loop.call_soon_threadsafe`` (set on the loop thread) so the bus'
    ``asyncio.Event`` is never set from off the loop thread. *cancel* is raised
    by :func:`shutdown_drive` so the loop checks it between rounds and stops
    claiming, letting daemon shutdown join the thread cleanly.

    Attributes:
        thread: The worker thread running the synchronous drain.
        loop: The daemon event loop the bus publish marshals onto.
        cancel: Set on daemon shutdown so the loop stops claiming + joins.
        handle_id: The run handle id the RPC returned for this drive.
    """

    thread: threading.Thread
    loop: asyncio.AbstractEventLoop | None
    cancel: threading.Event
    handle_id: str


#: The single active background drive (W01). At most one drive runs at a time:
#: a second ``fleet.drive`` while one is in flight is rejected. Guarded by
#: :data:`_DRIVE_LOCK` so the start / clear races stay consistent across the RPC
#: thread + the drive thread's own terminal clear.
_ACTIVE_DRIVE: list[_ActiveDrive | None] = [None]

#: The daemon event-loop thread, recorded when a drive is started so a publish
#: from that same thread (an arm-time IDLE persist) publishes directly while a
#: publish from the worker drive thread marshals through ``call_soon_threadsafe``.
#: A list cell so the module-level value is mutable without a ``global``.
_LOOP_THREAD: list[threading.Thread | None] = [None]

#: Serialises the start / clear of :data:`_ACTIVE_DRIVE` so the RPC thread and
#: the drive thread's terminal clear never race the single-active-run guard.
_DRIVE_LOCK = threading.Lock()


def _active_drive_loop() -> asyncio.AbstractEventLoop | None:
    """Return the captured event loop of the active background drive, or ``None``.

    The bus-publish marshal seam consults this so a publish from the drive
    thread can hop back onto the daemon event loop; ``None`` (no active drive,
    or an arm-time persist before the drive thread started) means the publish
    runs on the current thread directly.

    Returns:
        The active drive's captured loop, or ``None`` when no drive is in flight.
    """
    active = _ACTIVE_DRIVE[0]
    return active.loop if active is not None else None


#: Terminal outcome of watching one in-flight lane to completion. The watcher
#: contract is **blocking**: it returns only once the lane has finished, so the
#: loop never busy-spins on a still-running lane.
#:
#: ``"closed"`` -- the lane's wave reached :data:`WaveStatus.CLOSED` (a clean
#: round contribution). ``"forked"`` -- the lane failed or was re-planned, which
#: resets the convergence streak. ``"needs_user"`` -- the lane hit a needs-user
#: split mid-run (a clarification the executor could not resolve), so it pauses
#: to a blocking fork for operator input (DL-6) rather than failing outright.
LaneOutcome = str


class LaneDispatch(BaseModel):
    """Outcome of dispatching one claimed wave into a lane.

    The spawner yields this so the loop can register the live per-lane
    process: the ``session_id`` correlates the dispatch row, and ``pgid``
    keys the kill (DL-3) / reattach (DL-8) registry to a real OS process
    group. A spawn that produced no subprocess yields ``pgid=None``, which
    marks the lane unkillable rather than recording a fabricated pid -- the
    registry never holds a pid the OS does not own.

    Attributes:
        session_id: Registered dispatch session id, or ``None`` on a
            plan-only / stateless dispatch.
        pgid: Process-group id of the spawned child (its own group leader,
            so the pgid equals the child pid), or ``None`` when the spawn
            produced no subprocess.
        attempt: 1-based dispatch attempt -- the second half of the
            ``(wave_id, attempt)`` registry key.
    """

    model_config = ConfigDict(extra="forbid")
    session_id: str | None = None
    pgid: int | None = Field(default=None, ge=1)
    attempt: int = Field(default=1, ge=1)


#: Spawn a claimed wave's dispatch. Given the daemon context + wave id, returns
#: a :class:`LaneDispatch` carrying the registered session id, the spawned
#: child's pgid (``None`` on a plan-only / stateless dispatch -- the lane is
#: then unkillable), and the dispatch attempt. The daemon wires the live
#: ``agent.dispatch`` spawn=True path; tests inject a deterministic fake.
#:
#: A fake that returns a bare ``str | None`` is still accepted: the loop
#: normalises it to a :class:`LaneDispatch` (the string becomes the
#: ``session_id``, pgid stays ``None``), so the W01 spawner fakes keep working.
LaneSpawner = Callable[["MethodContext", str], "LaneDispatch | str | None"]

#: Watch one in-flight lane to its terminal :data:`LaneOutcome` (blocking).
#: Given the daemon context + the lane, returns ``"closed"`` or ``"forked"``.
#: The daemon wires a status-poll watcher; tests inject a deterministic fake.
LaneWatcher = Callable[["MethodContext", FleetLane], LaneOutcome]


@dataclass(frozen=True)
class LaneSpend:
    """The effort-unit + USD spend a finished lane added to the run -- DL-4.

    The budget HALT (DL-4) accumulates these per finished lane into the run's
    :attr:`~eawf.kernel.state.models.FleetCounters.spent_eu` / ``spent_usd``
    tallies so the cap check tests against live spend. A lane whose runtime was
    not captured contributes zero on both axes (the run simply never accrues
    spend toward its cap), so an instrumented and an uninstrumented run share
    one accumulation path.

    Attributes:
        eu: Effort units the lane spent, read off its runtime delta (zero when
            no runtime was captured).
        usd: USD the lane spent, read off its runtime delta (zero when no
            runtime was captured).
    """

    eu: float = 0.0
    usd: float = 0.0


#: Read one finished lane's :class:`LaneSpend` (EU + USD) for the budget HALT.
#: Given the daemon context + the finished lane's wave id, returns the spend
#: the lane added to the run. The daemon wires the live runtime-delta reader
#: (:func:`_default_lane_spend`); tests inject a deterministic fake so the cap
#: fires on injected figures without a real runtime sidecar.
LaneSpendReader = Callable[["MethodContext", str], LaneSpend]


def _default_lane_spend(ctx: MethodContext, wave_id: str) -> LaneSpend:
    """Read a finished lane's EU + USD spend off the wave's runtime delta -- live.

    The daemon-wired default: reads the wave's claim-time
    :attr:`~eawf.kernel.state.models.Wave.runtime_baseline` and the latest
    captured :attr:`~eawf.kernel.state.models.Wave.runtime_latest` off
    ``state.json`` (free read access) and computes the close-time runtime delta
    via :func:`~eawf.workflow.lifecycle.wave.compute_runtime_delta` -- the same
    EUCAP runtime-delta read the wave-close rollup uses (I05-W06). A wave with
    no captured runtime (no baseline / no latest) yields a zero
    :class:`LaneSpend` so the run never accrues phantom spend. A stateless
    context or a vanished wave likewise yields zero.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        wave_id: ``W<NN>`` wave whose finished lane spend to read.

    Returns:
        The lane's :class:`LaneSpend` (zero on both axes when no runtime was
        captured).
    """
    if ctx.state_path is None:
        return LaneSpend()
    wave = load_state(Path(ctx.state_path)).waves.get(wave_id)
    if wave is None:
        return LaneSpend()
    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        eu_minutes=DEFAULT_EU_MINUTES,
        eu_basis=EuBasis.API_DURATION,
    )
    if delta is None:
        return LaneSpend()
    return LaneSpend(eu=delta.elapsed_eu, usd=delta.actual_cost_usd)


#: Read the evidence ref backing one lane's blocking fork (DL-6). Given the
#: daemon context + the forking wave id + the resolved fork reason, returns the
#: repo-relative path / Eawf URN / external URL the operator reads before
#: resolving the fork, or ``None`` when no ref is available. The daemon wires
#: :func:`_default_fork_evidence` (a synthetic per-wave fork URN); tests inject a
#: deterministic fake to pin a specific ref.
ForkEvidenceReader = Callable[["MethodContext", str, FleetForkReason], "str | None"]


def _default_fork_evidence(ctx: MethodContext, wave_id: str, reason: FleetForkReason) -> str | None:
    """Derive the evidence ref backing a lane's blocking fork -- the live default.

    The daemon-wired default: synthesize a stable Eawf-style fork URN from the
    forking wave id + the fork reason so the queued :class:`FleetFork` always
    carries a non-empty, PII-free ref the cockpit can render. A stateless
    context yields the same synthetic ref (the URN needs no on-disk read), so an
    instrumented and an uninstrumented run share one path.

    Args:
        ctx: Daemon method context (unused by the synthetic default -- present
            so a live override can read ``state_path``).
        wave_id: ``W<NN>`` wave whose forking lane to reference.
        reason: The resolved :class:`FleetForkReason`.

    Returns:
        A synthetic ``urn:eawf:v1:fork:<wave>:<reason>`` ref.
    """
    del ctx
    return f"urn:eawf:v1:fork:{wave_id}:{reason.value}"


def budget_exhausted(run: FleetRun) -> bool:
    """Return whether any armed spend cap on *run* is reached -- pure, DL-4.

    Tests the run's live tallies against its armed caps: the EU cap against
    :attr:`~eawf.kernel.state.models.FleetCounters.spent_eu`, the USD cap
    against ``spent_usd``, and the waves cap against ``claimed``. A cap left
    ``None`` (the default) never fires, so a run armed with no cap -- or one
    far under every armed cap -- always reports ``False`` (the negative path).
    The first armed cap that is met returns ``True``; reaching any one cap is
    sufficient to stop claiming.

    Args:
        run: The live :class:`FleetRun` to test against its armed caps.

    Returns:
        ``True`` when at least one armed cap is reached, else ``False``.
    """
    if run.eu_cap is not None and run.counters.spent_eu >= run.eu_cap:
        return True
    if run.usd_cap is not None and run.counters.spent_usd >= run.usd_cap:
        return True
    return run.waves_cap is not None and run.counters.claimed >= run.waves_cap


def _normalise_dispatch(result: LaneDispatch | str | None) -> LaneDispatch:
    """Coerce a spawner return into a :class:`LaneDispatch`.

    The live default spawner returns a :class:`LaneDispatch`; the W01 spawner
    fakes return a bare ``str | None`` session id. A bare value is wrapped so
    the string becomes the ``session_id`` and the pgid stays ``None`` (the
    fake recorded no real subprocess, so its lane is unkillable). A
    :class:`LaneDispatch` passes through unchanged.

    Args:
        result: The spawner's return -- a :class:`LaneDispatch` or a bare
            session id (or ``None``).

    Returns:
        The normalised :class:`LaneDispatch`.
    """
    if isinstance(result, LaneDispatch):
        return result
    return LaneDispatch(session_id=result)


class DriveParams(BaseModel):
    """Params for :func:`drive`.

    Attributes:
        frontier: Ready ``W<NN>`` wave ids to drain, in claim order. Must be
            non-empty -- an empty frontier refuses to arm.
        concurrency: Maximum lanes the loop holds at once (the drain width).
        convergence: Convergence mode -- ``drain`` (stop only when the
            frontier empties) or ``kclean`` (stop after K consecutive clean
            rounds).
        kclean_k: K threshold for the ``kclean`` mode. Ignored under ``drain``.
        eu_cap: Optional cumulative EU spend cap; ``None`` leaves the run
            uncapped. At the cap the loop stops claiming (DL-4).
        usd_cap: Optional cumulative USD spend cap; ``None`` leaves the run
            uncapped.
        waves_cap: Optional claimed-wave count cap; ``None`` leaves the run
            uncapped.
        hard_halt: The arm-modal budget toggle. ``False`` (the default) drains
            the in-flight lanes at the cap; ``True`` KILLS them (DL-3).
    """

    model_config = ConfigDict(extra="forbid")
    frontier: list[str] = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1)
    convergence: str = "drain"
    kclean_k: int = Field(default=2, ge=1)
    eu_cap: float | None = Field(default=None, gt=0.0)
    usd_cap: float | None = Field(default=None, gt=0.0)
    waves_cap: int | None = Field(default=None, ge=1)
    hard_halt: bool = False


class DriveResult(BaseModel):
    """Result of :func:`drive` -- the terminal :class:`FleetRun` snapshot.

    Attributes:
        run_state: The terminal :class:`FleetRunState` the loop reached.
        terminal_reason: Why the run reached ``DONE`` (``None`` if it stopped
            short of a terminal state, e.g. ``PAUSED`` / ``HALTED``).
        counters: The run's final tallies.
    """

    model_config = ConfigDict(extra="forbid")
    run_state: FleetRunState
    terminal_reason: FleetTerminalReason | None
    counters: FleetCounters


class FleetDriveHandle(BaseModel):
    """Run handle the backgrounded ``fleet.drive`` RPC returns in under a second -- W01.

    The drain runs on a worker thread off the daemon event loop, so the RPC no
    longer blocks until the run reaches a terminal state. It returns this handle
    the instant the thread is started: the cockpit reads the run-state off it
    (``draining`` when the drain is underway, or ``idle`` when dispatch was paused
    so the arm staged the frontier but claimed nothing) and reads live vitals off
    the persisted ``state.fleet_run`` the thread advances.

    Attributes:
        handle_id: Opaque id for this drive, so a later RPC (status / reattach)
            can correlate against the started run.
        run_state: The run state at the moment the handle was returned --
            ``draining`` once the worker thread is claiming, or ``idle`` when
            dispatch was paused on arm (the frontier is staged, nothing claimed).
        backgrounded: ``True`` when the drain is running on a worker thread (the
            normal path); ``False`` when the arm stayed ``idle`` (dispatch
            paused) so no thread was started.
    """

    model_config = ConfigDict(extra="forbid")
    handle_id: str
    run_state: FleetRunState
    backgrounded: bool


class ResolveForkParams(BaseModel):
    """Params for the ``fleet.resolve_fork`` RPC -- DL-6.

    Attributes:
        wave_id: ``W<NN>`` wave whose queued fork to resolve.
        attempt: 1-based dispatch attempt -- the second half of the fork key.
        resolution: The operator's :class:`FleetForkResolution`.
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str
    attempt: int = Field(default=1, ge=1)
    resolution: FleetForkResolution


def _persist_fleet_run(ctx: MethodContext, fleet_run: FleetRun | None) -> None:
    """Write *fleet_run* onto ``State.fleet_run`` through the canonical writer.

    Routes the mutation through the daemon canonical state writer
    (``portalock(state.json)`` + locked atomic write, mirroring
    :func:`eawf.runtime.daemon.dispatch_runner._mark_wave_in_progress`): acquire
    the sibling lock, load the typed state, set ``fleet_run``, stamp
    ``updated_at``, then ``atomic_write_json_locked`` under the held lock. The
    loop never opens ``state.json`` directly.

    After the disk write the run transition is published to the subscription
    bus (:func:`_publish_run_transition`) so live subscribers (the TUI cockpit)
    see the run-state move without a poll tick. The drive loop runs on a worker
    thread (W01), so the publish marshals onto the daemon event loop through
    ``loop.call_soon_threadsafe`` rather than touching the ``asyncio.Event`` the
    bus sets from off the loop thread; a same-thread (event-loop) call publishes
    directly. The marshal seam is single-sourced in :func:`_publish_run_transition`.

    A bus-less / stateless context (``ctx.state_path`` unset) is a no-op so
    unit tests can drive the in-memory loop without an on-disk state.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        fleet_run: The run snapshot to persist, or ``None`` to clear it.
    """
    if ctx.state_path is None:
        return
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        state.fleet_run = fleet_run
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    run_state = fleet_run.run_state.value if fleet_run is not None else None
    _publish_run_transition(ctx, fleet_run)
    logger.info(f"_persist_fleet_run run_state={run_state!r}")


def _publish_run_transition(ctx: MethodContext, fleet_run: FleetRun | None) -> None:
    """Publish a ``fleet_run`` transition envelope on the subscription bus.

    The drive loop persists the run on every transition; this wakes live
    subscribers (the TUI cockpit vitals header) immediately with a
    ``state_mutated`` event envelope carrying the run state, mirroring the
    envelope :func:`eawf.runtime.daemon.dispatch_runner._publish_state_revision`
    publishes so a fleet-run transition is indistinguishable from any other
    state mutation on the wire.

    The W01 background-run thread drives the drain off the daemon event loop, so
    a publish from that thread MUST NOT touch the bus' ``asyncio.Event`` directly
    (an ``Event.set`` off the loop thread is a data race). When a captured loop
    is recorded for the active run (:func:`_active_drive_loop`) the publish is
    marshalled onto that loop through ``loop.call_soon_threadsafe``; an arm-time
    persist that already runs on the event-loop thread (no captured loop, or the
    current thread IS the loop thread) publishes directly. A bus-less context is
    a no-op.

    Args:
        ctx: Daemon method context -- supplies ``bus``.
        fleet_run: The persisted run snapshot, or ``None`` when cleared.
    """
    if ctx.bus is None or not hasattr(ctx.bus, "publish"):
        return
    run_state = fleet_run.run_state.value if fleet_run is not None else "cleared"
    now = datetime.now(UTC)
    summary = f"fleet_run run_state={run_state}"
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.fleet_run_transition",
        event_kind="state_mutated",
        actor="daemon",
        command="fleet.drive",
        args_hash="",
        status="ok",
        message=summary,
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    loop = _active_drive_loop()
    if loop is not None and loop.is_running() and threading.current_thread() is not _LOOP_THREAD[0]:
        loop.call_soon_threadsafe(ctx.bus.publish, envelope)
    else:
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def _dispatch_paused(ctx: MethodContext) -> bool:
    """Return the durable ``State.dispatch_paused`` flag.

    Reads through :func:`load_state` (free read access -- no lock needed for a
    point read). A stateless context (no ``state_path``) is treated as not
    paused so the in-memory loop runs.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.

    Returns:
        ``True`` when dispatch is paused, else ``False``.
    """
    if ctx.state_path is None:
        return False
    return load_state(Path(ctx.state_path)).dispatch_paused


def _default_spawner(ctx: MethodContext, wave_id: str) -> LaneDispatch:
    """Claim then live-dispatch *wave_id*, returning the lane dispatch outcome.

    The daemon-wired default: claim the wave through the pure-functional
    :func:`eawf.workflow.lifecycle.wave.claim_wave` transition (under the state
    portalock, out-of-order since parallel siblings of one dep-frontier are
    claimed at once), then issue an ``agent.dispatch`` spawn=True for it. The
    claim + the dispatch's own session registration both persist through the
    daemon canonical writer.

    The live ``agent.dispatch`` handler is async; the synchronous loop drives
    it on a fresh event loop in a worker thread so the loop never nests an
    ``asyncio.run`` inside the awaiting RPC handler's running loop. Tests bypass
    this entirely by injecting a :class:`LaneSpawner` fake.

    The spawned child is its own process-group leader (the claude adapter
    spawns ``start_new_session=True`` and resolves the group via ``os.getpgid``
    on the ``on_pgid`` seam), so the child pid the dispatch plan surfaces IS
    the group-leader id -- this records it as the lane ``pgid`` for the kill /
    reattach registry. A plan-only dispatch surfaces ``pid==0`` (no
    subprocess), which records ``pgid=None`` so the lane is unkillable rather
    than carrying a fabricated pid.

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave to claim + dispatch.

    Returns:
        A :class:`LaneDispatch` carrying the registered session id, the
        spawned child's pgid (``None`` on a plan-only dispatch), and the
        dispatch attempt.
    """
    claim_session_id = f"fleet-drive-{wave_id}"
    if ctx.state_path is not None:
        state_path = Path(ctx.state_path)
        with portalock.acquire(state_path, timeout=5.0):
            state = load_state(state_path)
            claim_wave(state, wave_id=wave_id, session_id=claim_session_id, out_of_order=True)
            state.updated_at = datetime.now(UTC)
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    plan = _run_dispatch_threaded(ctx, wave_id)
    # The child is its own group leader, so its pgid equals the surfaced child
    # pid; a plan-only dispatch surfaces ``pid==0`` (no subprocess) -> no pgid.
    pid = plan.get("pid")
    pgid = pid if isinstance(pid, int) and pid > 0 else None
    return LaneDispatch(
        session_id=plan.get("session_id"),
        pgid=pgid,
        attempt=int(plan.get("attempt", 1)),
    )


def _run_dispatch_threaded(ctx: MethodContext, wave_id: str) -> dict[str, Any]:
    """Run the async ``agent.dispatch`` spawn=True handler off a worker thread.

    The fleet loop is synchronous, so it cannot ``await`` the async dispatch
    handler directly nor ``asyncio.run`` it from inside the RPC's running loop.
    This runs the coroutine on a fresh loop in a dedicated thread and joins,
    returning the dispatch plan dict.

    Args:
        ctx: Daemon method context threaded into the dispatch handler.
        wave_id: ``W<NN>`` wave to dispatch spawn=True.

    Returns:
        The dispatch plan dict the handler returned.
    """
    from concurrent.futures import ThreadPoolExecutor

    from eawf.runtime.daemon.methods.agent import dispatch as _agent_dispatch

    async def _dispatch() -> dict[str, Any]:
        return await _agent_dispatch(ctx, {"wave_id": wave_id, "spawn": True})

    def _run() -> dict[str, Any]:
        return asyncio.run(_dispatch())

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


#: Runtime id -> the short ``RuntimeTriple`` spelling
#: :func:`~eawf.workflow.dispatch.routing.model_for_runtime` expects (it routes
#: on the short token, not the CLI-facing id). Mirrors the canonical map the
#: jury spawn factory uses so the live repair re-spawn resolves the runtime's own
#: vendor model.
_REPAIR_RUNTIME_TRIPLE: dict[str, str] = {
    "claude-code": "claude",
    "claude-agent-sdk": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def _live_lane_error_classifier(exc: RuntimeSpawnError, runtime: str) -> ErrorClass:
    """Classify a live lane-spawn failure via the resolved adapter -- W03 / DL-11.

    The production :class:`~eawf.workflow.dispatch.retry.ErrorClassifier` the
    bounded spawn ladder (:func:`spawn_lane_or_fork`) consults on a RECOVERABLE
    failure to pick the V5 ladder action: it resolves the runtime the spawn
    failed on and asks that adapter's
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.parse_error` to map the
    failure to a canonical :class:`~eawf.runtime.runtimes.adapter.ErrorClass`.
    This is the SAME binding the single-wave dispatch path uses
    (:func:`eawf.runtime.daemon.methods.agent._spawn_and_dispatch`'s ``_classify``
    closure), lifted to a module-level callable so the live drive can wire it
    into :func:`arm_drive` -- without it ``classify is None`` and the spawn
    ladder never fires on a real autopilot run.

    Args:
        exc: The :class:`RuntimeSpawnError` the live spawn raised.
        runtime: Runtime id the spawn failed on (the loop may have switched).

    Returns:
        The canonical :class:`~eawf.runtime.runtimes.adapter.ErrorClass` the
        resolved adapter classifies the failure to. A parse-level failure with
        no exit context coerces the status to ``-1``, which falls through to the
        conservative ``RUNTIME_API_ERROR`` (a switch signal).
    """
    exit_status = exc.exit_status if exc.exit_status is not None else -1
    return select_adapter(runtime).parse_error(exit_status, exc.stderr)


def _build_live_lane_repair_hook(ctx: MethodContext) -> LaneRepairHook:
    """Build the production repair hook the live drive wires into the loop -- W03 / DL-7.

    The grounded repair ladder (:func:`repair_lane_or_fork`) was built + tested
    with ZERO production callers: the live drive never passed ``repair=``, so a
    failing-check fork on a real autopilot run skipped the ladder entirely. This
    returns the live :class:`LaneRepairHook` the drive wires in, so a genuine
    failing-check lane is re-dispatched up the bounded grounded repair ladder
    rather than counted a terminal failure on the first refusal.

    The hook drives the BUILT pieces, inventing no new policy:

    1. Resolve the forked lane's wave + its FIRST refused success criterion off
       ``state.json`` (free read), grounding the repair on the wave's recorded
       ``outcome`` (the concrete failing-check detail the agent left). When the
       wave / criterion / grounding detail cannot be resolved off disk, the hook
       returns ``resolved=False`` WITHOUT enqueuing a fork, so the lane keeps its
       pre-W03 terminal-fork behaviour (the ladder is a re-dispatch, never a
       silent drop).
    2. Build the verbatim base prompt for the re-dispatch via
       :func:`~eawf.workflow.dispatch.renderer.render_dispatch_envelope`.
    3. Bind the LIVE adapter re-spawn + the LIVE close-gate oracle re-verify
       (:func:`~eawf.workflow.verify.oracle.run_oracle`) and drive
       :func:`repair_lane_or_fork`: a resolved repair returns the re-dispatched
       :class:`LaneDispatch` under its INCREMENTED attempt (the cockpit repair
       counter), and an exhausted ladder has already enqueued the
       ``REPAIR_EXHAUSTED`` fork through the canonical writer.

    Args:
        ctx: Daemon method context -- supplies ``state_path`` + ``event_path``
            for the live re-spawn + re-verify.

    Returns:
        A :class:`LaneRepairHook` closing over *ctx*. Tests inject a
        deterministic fake instead so the loop exercises the repair path without
        a real adapter.
    """

    def _hook(hook_ctx: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
        outcome: LaneRepairOutcome = _drive_coro(_live_repair_lane(hook_ctx, lane))
        return outcome

    return _hook


async def _live_repair_lane(ctx: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
    """Drive one failing live lane through the grounded repair ladder -- W03 / DL-7.

    The async body of the live :class:`LaneRepairHook` (see
    :func:`_build_live_lane_repair_hook`). Resolves the refused criterion +
    grounding detail off ``state.json``, binds the live adapter re-spawn + the
    live close-gate re-verify, and drives :func:`repair_lane_or_fork`. A resolved
    repair yields a :class:`LaneRepairOutcome` carrying the re-dispatched lane
    under its incremented attempt; an exhausted ladder yields ``resolved=False``
    (the ``REPAIR_EXHAUSTED`` fork already enqueued by :func:`repair_lane_or_fork`).
    A lane whose grounding cannot be resolved off disk yields ``resolved=False``
    with no fork enqueued, so the loop keeps the pre-W03 terminal-fork behaviour.

    Args:
        ctx: Daemon method context.
        lane: The forked lane to repair (carrying its pre-repair attempt).

    Returns:
        The :class:`LaneRepairOutcome` of the repair drive.
    """
    from eawf.kernel.config.layered import resolve_runtime_tier_models
    from eawf.kernel.state.enums import AgentSessionRole as _Role
    from eawf.kernel.state.enums import EffortBucket as _Effort
    from eawf.runtime.sandbox.policy import resolve_denied_tools
    from eawf.workflow.dispatch.renderer import render_dispatch_envelope, resolve_role_blocks
    from eawf.workflow.dispatch.routing import model_for_runtime
    from eawf.workflow.verify.oracle import run_oracle

    wave_id = lane.wave_id
    if ctx.state_path is None or ctx.event_path is None:
        return LaneRepairOutcome(resolved=False, attempts_used=1, dispatch=None)
    state_path = Path(ctx.state_path)
    repo_root = state_path.parent.parent
    state = load_state(state_path)
    wave = state.waves.get(wave_id)
    # The grounding the repair re-dispatch needs: a refused criterion to target +
    # the concrete failing-check detail to ground the FIRST re-dispatch on. The
    # wave's recorded outcome IS that falsifier payload; with no criterion or no
    # grounding detail there is nothing to ground a repair on, so the lane keeps
    # its terminal-fork behaviour rather than dispatching a content-free repair.
    if wave is None or not wave.success_criteria:
        return LaneRepairOutcome(resolved=False, attempts_used=1, dispatch=None)
    criterion = wave.success_criteria[0]
    failing_detail = " ".join((wave.outcome or "").split())
    if not failing_detail:
        return LaneRepairOutcome(resolved=False, attempts_used=1, dispatch=None)

    # The lane carries no runtime; resolve it from the wave's preference ladder
    # (the first entry is the highest-preference runtime) so the re-spawn runs on
    # the runtime the wave pinned, falling back to claude-code when unset.
    runtime = wave.runtime_preference[0] if wave.runtime_preference else "claude-code"
    role = wave.agent_role if wave.agent_role is not None else _Role.EXECUTOR
    effort = wave.effort_bucket if wave.effort_bucket is not None else _Effort.M
    role_tier = resolve_role_blocks(repo_root)
    base_prompt = render_dispatch_envelope(
        state,
        wave_id,
        runtime,
        repo_root=repo_root,
        role_blocks=role_tier.role_blocks,
        role_tier_token_cap=role_tier.token_cap,
    ).prompt
    denied = sorted(resolve_denied_tools(state.sandbox_policies, wave_id=wave_id))
    cwd = str(repo_root)
    runtime_models = resolve_runtime_tier_models(repo_root)

    def _spawn_on(spawn_runtime: str) -> RepairSpawnFn:
        adapter = select_adapter(spawn_runtime)
        model = model_for_runtime(
            role,
            effort,
            _REPAIR_RUNTIME_TRIPLE.get(spawn_runtime, "claude"),
            runtime_models=runtime_models,
        )

        async def _spawn(prompt: str) -> SpawnResult:
            return await adapter.spawn_session(prompt, model=model, cwd=cwd, denied_tools=denied)

        return _spawn

    _repair_spawn = _spawn_on(runtime)

    async def _verify(result: SpawnResult) -> str | None:
        oracle = await run_oracle(
            criterion,
            list(wave.gates),
            wave=wave,
            state=state,
            state_path=state_path,
            events_path=Path(ctx.event_path),
            repo_root=repo_root,
            spawn_factory=_spawn_on,
        )
        # ``None`` signals the refusal is resolved; a still-failing oracle returns
        # the producing-gate detail so the next re-dispatch re-grounds on it.
        if oracle.status == "pass":
            return None
        return oracle.failing_detail()

    def _verify_sync(result: SpawnResult) -> str | None:
        still_failing: str | None = _drive_coro(_verify(result))
        return still_failing

    try:
        repaired = await repair_lane_or_fork(
            ctx,
            criterion,
            failing_detail,
            base_prompt=base_prompt,
            spawn=_repair_spawn,
            verify=_verify_sync,
            wave_id=wave_id,
            attempt=lane.attempt,
        )
    except RepairExhaustedError as exc:
        # repair_lane_or_fork already enqueued the REPAIR_EXHAUSTED fork through
        # the canonical writer; signal exhaustion so the loop absorbs that
        # disk-side fork (it does NOT re-dispatch forever).
        return LaneRepairOutcome(resolved=False, attempts_used=max(exc.attempts, 1), dispatch=None)
    # The repair re-dispatch resolved the refusal: re-register the lane in flight
    # under its INCREMENTED attempt (the cockpit repair counter). The child is
    # its own group leader, so its pid IS the pgid for the kill / reattach
    # registry.
    return LaneRepairOutcome(
        resolved=True,
        attempts_used=1,
        dispatch=LaneDispatch(
            session_id=repaired.session_id,
            pgid=repaired.subprocess_pid,
            attempt=lane.attempt + 1,
        ),
    )


#: Seconds the default watcher sleeps between on-disk status polls.
_WATCH_POLL_SECONDS = 1.0

#: Grace window (seconds) the watcher waits AFTER a lane's process group first
#: reads dead before it resolves a stalled lane to a fork (W05). A clean agent
#: exit closes the wave a moment after the process dies, so the deadline gives
#: that close-write time to land before declaring the lane wedged -- a healthy
#: lane that closes within the grace never forks on the liveness probe. Tuned
#: well above the 1s poll so a single slow status write never trips it.
_WATCH_STALL_DEADLINE_SECONDS = 30.0


def _has_open_pause(state_path: Path, wave_id: str) -> bool:
    """Return whether *wave_id* has an unresolved needs_user pause -- W09.

    The live watcher consults this each poll: a lane whose executor surfaced a
    needs_user pause (a clarification it could not resolve) has an open
    ``needs_user_pause`` row with no matching resume on the event feed, scoped to
    the wave id. The watcher reports such a lane ``"needs_user"`` so the loop
    produces the DL-6 ``NEEDS_USER_SPLIT`` blocking fork rather than wedging.
    Reads through :func:`~eawf.workflow.skills.needs_user.list_open_pauses` (free
    read access). A read error degrades to ``False`` (no pause) so a transient
    feed-read hiccup never mis-forks a healthy lane.

    Args:
        state_path: Path to ``state.json`` (the event feed resolves under its
            sibling ``store/``).
        wave_id: ``W<NN>`` wave whose open pauses to check.

    Returns:
        ``True`` when the wave has at least one unresolved needs_user pause.
    """
    from eawf.workflow.skills.needs_user import list_open_pauses

    try:
        return bool(list_open_pauses(state_path, scope_id=wave_id))
    except (OSError, ValueError) as exc:
        logger.debug(f"_has_open_pause wave={wave_id} read_failed cause={exc!r}")
        return False


@dataclass
class _StallDeadline:
    """Tracks the dead-pgid stall grace window for one watched lane -- W05.

    The liveness probe reads dead intermittently; this records the wall-clock of
    the FIRST dead observation and reports the lane stalled once the grace window
    elapses. A live (or transiently-recovered) observation resets the window so a
    flicker never trips the fork.

    Attributes:
        now: Monotonic clock the deadline measures against.
        stall_deadline: Grace seconds after the first dead read before forking.
        dead_since: Wall-clock of the first dead read, or ``None`` while alive.
    """

    now: Callable[[], float]
    stall_deadline: float
    dead_since: float | None = None

    def observe_dead(self, *, wave_id: str, pgid: int) -> bool:
        """Record a dead-pgid read; return whether the grace window has elapsed."""
        if self.dead_since is None:
            self.dead_since = self.now()
            logger.info(
                f"liveness_watcher wave={wave_id} pgid={pgid} status=dead "
                f"deadline={self.stall_deadline}"
            )
            return False
        if self.now() - self.dead_since >= self.stall_deadline:
            logger.warning(
                f"liveness_watcher wave={wave_id} pgid={pgid} status=stalled-dead outcome=forked"
            )
            return True
        return False

    def observe_alive(self) -> None:
        """Reset the window on a live (or unaddressable) read -- not a stall."""
        self.dead_since = None


def _status_terminal_outcome(wave: Any) -> LaneOutcome | None:
    """Map a polled wave's status to a terminal :data:`LaneOutcome`, or ``None``.

    The status half of the watcher's per-poll decision: a vanished wave (gone
    from state) or a ``FAILED`` / ``ABANDONED`` status resolves ``"forked"``, a
    ``CLOSED`` status resolves ``"closed"``, and any in-flight status returns
    ``None`` so the watcher keeps polling.

    Args:
        wave: The polled :class:`~eawf.kernel.state.models.Wave`, or ``None`` when
            it has vanished from state.

    Returns:
        The terminal :data:`LaneOutcome`, or ``None`` when the wave is still in
        flight.
    """
    if wave is None:
        return "forked"
    if wave.status is WaveStatus.CLOSED:
        return "closed"
    if wave.status in {WaveStatus.FAILED, WaveStatus.ABANDONED}:
        return "forked"
    return None


def _wave_has_close_ready_report(state_path: Path, wave_id: str) -> bool:
    """Return whether *wave_id* has a persisted close-ready executor report.

    The headless live-spawn dispatch persists the spawned agent's
    :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody` and runs
    :func:`~eawf.workflow.verify.dispatch_close.verify_close_readiness` (it raises
    on a FAIL / BLOCKED verdict, so a body persisted on the dispatch path is
    close-ready by construction). The liveness watcher consults this on the
    dead-pgid stall path: a SANDBOXED headless agent runs to completion in the
    synchronous dispatch but cannot run ``eawf wave close`` itself, so its wave
    stays IN_PROGRESS with a dead process. A persisted close-ready report is the
    evidence the agent SUCCEEDED -- the watcher then resolves the lane ``"closed"``
    (DL-5 still applies downstream in :meth:`_Loop._finish_lane`) rather than
    forking an otherwise-successful wave. Reads the LATEST executor-report row
    for the wave; a missing store / unreadable row / no matching row is ``False``
    (the lane forks as a genuine stall).

    Args:
        state_path: Path to ``state.json`` (the report store resolves under its
            sibling ``store/``).
        wave_id: ``W<NN>`` wave whose latest executor report to check.

    Returns:
        ``True`` when the wave's latest executor report passes close-readiness.
    """
    path = store_path(state_path, StoreKind.EXECUTOR_REPORT)
    if not path.exists():
        return False
    latest: AgentReportBody | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            body = AgentReportPayload.model_validate(
                Envelope.model_validate_json(line).payload
            ).body
            if getattr(body, "wave_id", None) == wave_id:
                latest = body
    except (OSError, ValueError) as exc:
        logger.debug(f"_wave_has_close_ready_report wave={wave_id} read_failed cause={exc!r}")
        return False
    if latest is None:
        return False
    return verify_close_readiness(wave_id, latest).passed


def build_liveness_watcher(
    *,
    is_alive: LivenessProbe | None = None,
    stall_deadline: float = _WATCH_STALL_DEADLINE_SECONDS,
    poll_seconds: float = _WATCH_POLL_SECONDS,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> LaneWatcher:
    """Build the live status-poll watcher with a pgid liveness probe + stall deadline -- W05.

    The pre-W05 watcher polled the lane's on-disk wave status FOREVER, so a dead
    agent (its process group reaped without the wave ever flipping terminal)
    wedged the lane and stalled the whole drain. This builds a watcher that, on
    each poll, also probes the lane's process-group liveness
    (:func:`_default_liveness`) and resolves a DEAD-but-unflipped lane to a fork
    once a bounded *stall_deadline* elapses -- so a dead lane resolves to a
    watcher fork within the deadline instead of wedging.

    The liveness probe never kills a HEALTHY slow lane: while the pgid reads
    alive (or the lane carries no addressable pgid, so there is nothing to
    probe) the watcher keeps polling the status, exactly as before. Only a lane
    whose group has been reaped AND whose wave has NOT reached a terminal status
    within the grace window forks. A clean agent exit closes the wave a moment
    after the process dies, so the deadline gives that close-write time to land
    before the lane is declared wedged.

    The probe / clock / sleep are injectable so a test exercises the deadline
    deterministically without a real process or a real wall-clock wait; the
    daemon wires the live :func:`_default_liveness` + :func:`time.monotonic` +
    :func:`time.sleep`.

    Args:
        is_alive: Liveness probe (``pgid -> bool``); ``None`` uses
            :func:`_default_liveness`.
        stall_deadline: Grace seconds after the pgid first reads dead before the
            lane forks.
        poll_seconds: Seconds slept between status polls.
        clock: Monotonic clock (``() -> float``); ``None`` uses
            :func:`time.monotonic`.
        sleep: Sleep callable (``float -> None``); ``None`` uses
            :func:`time.sleep`.

    Returns:
        A :class:`LaneWatcher` resolving each lane to its terminal outcome.
    """
    return _LivenessWatcher(
        probe=is_alive if is_alive is not None else _default_liveness,
        now=clock if clock is not None else time.monotonic,
        rest=sleep if sleep is not None else time.sleep,
        stall_deadline=stall_deadline,
        poll_seconds=poll_seconds,
    )


@dataclass
class _LivenessWatcher:
    """The status-poll watcher with a pgid liveness probe + stall deadline -- W05.

    A callable :data:`LaneWatcher` (not a closure) so each per-poll concern is a
    method with its own complexity budget. Built by :func:`build_liveness_watcher`
    with the live probe / clock / sleep (or injected fakes under test).

    Attributes:
        probe: Liveness probe (``pgid -> bool``).
        now: Monotonic clock for the stall deadline.
        rest: Sleep callable between polls.
        stall_deadline: Grace seconds after the pgid first reads dead before fork.
        poll_seconds: Seconds slept between status polls.
    """

    probe: LivenessProbe
    now: Callable[[], float]
    rest: Callable[[float], None]
    stall_deadline: float
    poll_seconds: float

    def _poll_outcome(self, state_path: Path, lane: FleetLane) -> LaneOutcome | None:
        """Return the lane's terminal outcome this poll, or ``None`` if in flight.

        A vanished / terminal wave resolves immediately
        (:func:`_status_terminal_outcome`), an open needs_user pause (W09)
        resolves ``"needs_user"``, else the lane is still in flight (``None``).
        """
        wave = load_state(state_path).waves.get(lane.wave_id)
        terminal = _status_terminal_outcome(wave)
        if terminal is not None:
            return terminal
        if _has_open_pause(state_path, lane.wave_id):
            logger.info(f"liveness_watcher wave={lane.wave_id} status=needs_user")
            return "needs_user"
        return None

    def _liveness_forked(self, lane: FleetLane, deadline: _StallDeadline) -> bool:
        """Return whether the dead-pgid stall deadline has elapsed this poll -- W05.

        A lane with no addressable pgid (or a live group) resets the deadline and
        keeps polling; a dead group starts / continues the deadline and forks once
        the grace window elapses.
        """
        if lane.pgid is not None and not self.probe(lane.pgid):
            return deadline.observe_dead(wave_id=lane.wave_id, pgid=lane.pgid)
        deadline.observe_alive()
        return False

    def __call__(self, ctx: MethodContext, lane: FleetLane) -> LaneOutcome:
        """Block-poll *lane* to its terminal outcome (the :data:`LaneWatcher` body)."""
        if ctx.state_path is None:
            # Stateless context cannot poll a status -- treat the lane as forked
            # so the loop frees the slot rather than spinning.
            return "forked"
        state_path = Path(ctx.state_path)
        deadline = _StallDeadline(now=self.now, stall_deadline=self.stall_deadline)
        while True:
            outcome = self._poll_outcome(state_path, lane)
            if outcome is not None:
                return outcome
            # The wave is still in flight. The liveness deadline forks a lane
            # whose process group has been dead past the grace window -- UNLESS
            # the agent left a close-ready report. A SANDBOXED headless agent
            # runs to completion in the synchronous dispatch (so its process is
            # legitimately dead here) but cannot run `eawf wave close` itself, so
            # its wave is still IN_PROGRESS; a persisted close-ready report is the
            # evidence it succeeded, so resolve "closed" (DL-5 is applied
            # downstream in _finish_lane) rather than forking a successful wave.
            if self._liveness_forked(lane, deadline):
                if _wave_has_close_ready_report(state_path, lane.wave_id):
                    logger.info(f"liveness_watcher wave={lane.wave_id} status=closed-on-report")
                    return "closed"
                return "forked"
            self.rest(self.poll_seconds)


def _default_watcher(ctx: MethodContext, lane: FleetLane) -> LaneOutcome:
    """Block-poll one lane to its terminal outcome via the on-disk wave status -- W05.

    The daemon-wired default: poll the lane's wave status from ``state.json``
    until it reaches a terminal status, mapping ``CLOSED`` -> a clean close and
    ``FAILED`` / ``ABANDONED`` (or a vanished wave) -> a fork. On each poll it
    ALSO probes the lane's process-group liveness and resolves a dead-but-
    unflipped lane to a fork once the stall deadline elapses (W05), so a dead
    agent no longer wedges the lane forever; a healthy slow lane is NOT killed by
    the probe. The poll sleeps between reads so the loop never busy-spins. Tests
    inject a :class:`LaneWatcher` fake instead (or build one via
    :func:`build_liveness_watcher` with injectable probe / clock / sleep), so
    this blocking poll never runs under test.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        lane: The in-flight lane to inspect.

    Returns:
        The lane's terminal :data:`LaneOutcome` (``"closed"`` or ``"forked"``).
    """
    return build_liveness_watcher()(ctx, lane)


def _lane_risk_tier(ctx: MethodContext, wave_id: str) -> RiskTier:
    """Resolve a wave's :class:`RiskTier` from its gate kinds at fill time.

    Loads the wave from ``state.json`` (free read access) and runs the pure
    :func:`~eawf.workflow.verify.oracle.classify_risk_tier` over its gates so
    the loop can record the resolved tier on the lane (the cockpit badge) and
    consult the auto-close / fork gate when the lane finishes. A stateless
    context or a vanished wave classifies :attr:`RiskTier.MECH` -- with no
    gates to inspect there is nothing that needs human judgement, so the
    least-risk band is the safe resolution.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        wave_id: ``W<NN>`` wave whose risk tier to resolve.

    Returns:
        The wave's classified :class:`RiskTier`.
    """
    if ctx.state_path is None:
        return RiskTier.MECH
    wave = load_state(Path(ctx.state_path)).waves.get(wave_id)
    if wave is None:
        return RiskTier.MECH
    return classify_risk_tier(wave.gates)


def gate_lane_outcome(
    outcome: LaneOutcome,
    risk_tier: RiskTier,
    *,
    block_authority: BlockAuthority,
) -> LaneOutcome:
    """Apply the RiskTier auto-close / fork gate to a watcher *outcome* -- pure.

    The fleet loop's safety gate (DL-5). A lane that the watcher reports
    ``"forked"`` always stays forked -- a failed lane never auto-closes. A lane
    the watcher reports ``"closed"`` is held to its wave's :class:`RiskTier`:

    - a :attr:`RiskTier.MECH` / :attr:`RiskTier.MED` close passes through (a
      deterministic pass or an auditor verdict is complete ground truth);
    - a :attr:`RiskTier.HIGH` / :attr:`RiskTier.UI` close passes through ONLY
      when the jury has earned :attr:`BlockAuthority.BLOCKING`; under the
      uncalibrated :attr:`BlockAuthority.ADVISORY` default the close is
      DOWNGRADED to ``"forked"`` so the wave NEVER silently auto-closes on an
      unearned jury. THIS NEGATIVE PATH IS THE LOAD-BEARING SAFETY INVARIANT.

    Args:
        outcome: The watcher's terminal :data:`LaneOutcome` (``"closed"`` /
            ``"forked"``).
        risk_tier: The lane's resolved :class:`RiskTier`.
        block_authority: The jury's earned authority for this close.

    Returns:
        The gated :data:`LaneOutcome` -- the input outcome, downgraded to
        ``"forked"`` only when a high / ui close lacks earned blocking
        authority. A ``"needs_user"`` outcome passes through unchanged (it is a
        DL-6 blocking-fork pause, not a clean close to gate).
    """
    if outcome != "closed":
        return outcome
    if risk_tier_auto_closes(risk_tier, block_authority=block_authority):
        return "closed"
    return "forked"


def classify_fork_reason(
    watched: LaneOutcome,
    risk_tier: RiskTier,
    *,
    block_authority: BlockAuthority,
) -> FleetForkReason | None:
    """Resolve why a finished lane pauses to a BLOCKING fork, or ``None`` -- DL-6, pure.

    A lane pauses to a blocking fork (removed from its slot, enqueued as a typed
    :class:`FleetFork`, sibling lanes keep draining) only on a reason the
    operator must resolve -- NOT on a genuine watcher failure, which is terminal:

    - a ``"needs_user"`` watch (any tier) -> :attr:`FleetForkReason.NEEDS_USER_SPLIT`;
    - a clean ``"closed"`` watch the DL-5 gate would NOT auto-close, split by the
      held lane's band: a :attr:`RiskTier.HIGH` (jury-gated) lane forks because
      the jury has not earned blocking authority ->
      :attr:`FleetForkReason.UNCALIBRATED_JURY`; a :attr:`RiskTier.UI`
      (visual-band) lane forks because the visual close is the least
      deterministic, high-risk close -> :attr:`FleetForkReason.HIGH_RISK_CLOSE`;
    - every other outcome (a clean auto-closing close, or a genuine
      ``"forked"`` failure) -> ``None`` (no operator pause).

    Args:
        watched: The watcher's raw terminal :data:`LaneOutcome` (``"closed"`` /
            ``"forked"`` / ``"needs_user"``).
        risk_tier: The lane's resolved :class:`RiskTier`.
        block_authority: The jury's earned authority for this close.

    Returns:
        The :class:`FleetForkReason` to enqueue, or ``None`` when the lane does
        not pause to a blocking fork.
    """
    if watched == "needs_user":
        return FleetForkReason.NEEDS_USER_SPLIT
    if watched != "closed":
        return None
    if risk_tier_auto_closes(risk_tier, block_authority=block_authority):
        return None
    if risk_tier is RiskTier.UI:
        return FleetForkReason.HIGH_RISK_CLOSE
    return FleetForkReason.UNCALIBRATED_JURY


#: Max chars the escalation fork's evidence ref carries from the last failing
#: check, bounded so a long oracle dump cannot blow the
#: :attr:`~eawf.kernel.state.models.FleetFork.evidence_ref` field.
_REPAIR_FORK_DETAIL_CAP = 1000


def repair_exhausted_fork(
    exc: RepairExhaustedError,
    *,
    wave_id: str,
    attempt: int,
    risk_tier: RiskTier,
) -> FleetFork:
    """Build the ``REPAIR_EXHAUSTED`` escalation fork from a spent repair loop -- DL-7, pure.

    The grounded repair loop (:func:`~eawf.workflow.dispatch.retry.repair_until_resolved`)
    raises :class:`~eawf.workflow.dispatch.retry.RepairExhaustedError` when its
    attempt budget is spent and the refused criterion still fails. Rather than
    silently dropping the lane or re-dispatching forever, the loop ESCALATES it
    to an operator-resolved :class:`FleetFork` tagged
    :attr:`~eawf.kernel.state.models.FleetForkReason.REPAIR_EXHAUSTED` ("repair
    exhausted -- your call"). The fork carries the LAST failing check (the
    freshest falsifier the final repair attempt still produced) as its evidence
    ref, normalised to a single line and bounded, so the operator reads the
    concrete check they must adjudicate -- never a content-free "it drifted".

    Args:
        exc: The typed repair exhaustion the spent loop raised; supplies the
            refused criterion id + the last failing-check payload.
        wave_id: ``W<NN>`` wave whose repair lane exhausted.
        attempt: 1-based dispatch attempt -- the second half of the
            ``(wave_id, attempt)`` fork key.
        risk_tier: The lane's resolved :class:`RiskTier` at escalation time, so
            the cockpit renders the band badge on the queued fork.

    Returns:
        The :class:`FleetFork` to enqueue -- reason ``REPAIR_EXHAUSTED``, evidence
        ref carrying the last failing check.
    """
    # Normalise the last failing check to a single bounded line so the evidence
    # ref stays a scannable reference rather than a multi-line oracle dump; a
    # blank payload (defensively) falls back to a stable repair-exhaustion URN.
    detail = " ".join(exc.last_failing_detail.split())[:_REPAIR_FORK_DETAIL_CAP]
    evidence_ref = detail or f"urn:eawf:v1:fork:{wave_id}:repair_exhausted"
    fork = FleetFork(
        wave_id=wave_id,
        attempt=attempt,
        risk_tier=risk_tier,
        reason=FleetForkReason.REPAIR_EXHAUSTED,
        evidence_ref=evidence_ref,
        forked_at=datetime.now(UTC),
    )
    logger.info(
        f"repair_exhausted_fork wave={wave_id} attempt={attempt} "
        f"criterion={exc.criterion_id!r} risk_tier={risk_tier.value} "
        f"reason={FleetForkReason.REPAIR_EXHAUSTED.value}"
    )
    return fork


def _enqueue_fork(ctx: MethodContext, fork: FleetFork) -> None:
    """Append *fork* to ``FleetRun.forks`` through the daemon canonical writer.

    The escalation half of :func:`repair_lane_or_fork`: under the state
    portalock, load the armed run, drop the exhausted lane's in-flight slot when
    it is still registered (so the registry never holds a lane that is now a
    queued fork), append the typed fork, bump the ``forked`` + ``blocked`` safety
    tallies, and atomic-write through the canonical writer. The loop never opens
    ``state.json`` directly.

    Dropping the lane and enqueuing the fork happen under ONE held lock so the
    lane can never be observed in any intermediate state other than fork: there
    is no window where the lane is removed but the fork is not yet queued.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        fork: The escalation :class:`FleetFork` to enqueue.

    Raises:
        LifecycleError: When ``ctx.state_path`` is unset or no fleet run is
            armed -- the escalation has nowhere to enqueue, so it fails loud
            rather than dropping the fork on the floor.
    """
    if ctx.state_path is None:
        raise LifecycleError("cannot enqueue repair-exhausted fork: state_path not configured")
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        run = state.fleet_run
        if run is None:
            raise LifecycleError("cannot enqueue repair-exhausted fork: no fleet run armed")
        existing = run.lanes.get(fork.wave_id)
        if existing is not None and existing.attempt == fork.attempt:
            del run.lanes[fork.wave_id]
        run.forks.append(fork)
        run.counters.forked += 1
        run.counters.blocked += 1
        state.fleet_run = run
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(
        f"_enqueue_fork wave={fork.wave_id} attempt={fork.attempt} "
        f"reason={fork.reason.value} forks_open={len(run.forks)}"
    )


async def repair_lane_or_fork(
    ctx: MethodContext,
    criterion: CriterionSpec,
    failing_detail: str,
    *,
    base_prompt: str,
    spawn: RepairSpawnFn,
    verify: RepairVerifier,
    wave_id: str,
    attempt: int = 1,
    risk_tier: RiskTier = RiskTier.MECH,
    max_attempts: int | None = None,
) -> SpawnResult:
    """Drive a lane's grounded repair, ESCALATING budget exhaustion to a fork -- DL-7.

    Runs the bounded grounded repair loop
    (:func:`~eawf.workflow.dispatch.retry.repair_until_resolved`) for a refused
    *criterion*. On a resolved repair the re-dispatch result is returned and the
    lane proceeds. When the repair budget is SPENT without the criterion passing,
    the loop raises
    :class:`~eawf.workflow.dispatch.retry.RepairExhaustedError`; this catches it
    and ESCALATES the lane to an operator-resolved
    :attr:`~eawf.kernel.state.models.FleetForkReason.REPAIR_EXHAUSTED` fork
    (carrying the last failing check) through the daemon canonical state writer,
    then re-raises the typed exhaustion so the caller sees the lane terminated as
    a fork.

    The no-silent-drop invariant (DL-7, success criterion C2): an exhausted lane
    is NEVER reset to PENDING and NEVER dropped without a queued fork -- the only
    terminal this path takes on exhaustion is enqueuing the ``REPAIR_EXHAUSTED``
    fork. The enqueue + the re-raise are the sole exhaustion exit, so no code path
    can leave the lane in any state other than fork.

    Args:
        ctx: Daemon method context -- supplies ``state_path`` (the fork-queue
            write target).
        criterion: The refused success criterion the repair targets.
        failing_detail: The concrete failing-check output the close gate refused
            on -- the grounding payload of the FIRST repair re-dispatch.
        base_prompt: The original rendered dispatch prompt, preserved verbatim
            under each repair notice.
        spawn: Injected async repair re-dispatch callable (the resolved adapter
            in production, a recording stub under test).
        verify: Injected re-verifier returning the still-failing payload or
            ``None`` once the refusal is resolved.
        wave_id: ``W<NN>`` wave whose repair lane this drives.
        attempt: 1-based dispatch attempt -- the second half of the fork key.
        risk_tier: The lane's resolved :class:`RiskTier`, recorded on the
            escalation fork.
        max_attempts: Optional repair-attempt ceiling override; ``None`` uses the
            repair loop's bounded default.

    Returns:
        The :class:`~eawf.runtime.runtimes.adapter.SpawnResult` of the repair
        re-dispatch the verifier accepted.

    Raises:
        RepairExhaustedError: When the repair budget is spent without the
            criterion passing -- re-raised AFTER the ``REPAIR_EXHAUSTED`` fork is
            enqueued, so the lane terminates as a fork (never a silent drop).
        LifecycleError: When the escalation has no armed run to enqueue onto.
    """
    repair_kwargs: dict[str, Any] = {}
    if max_attempts is not None:
        repair_kwargs["max_attempts"] = max_attempts
    try:
        return await repair_until_resolved(
            criterion,
            failing_detail,
            base_prompt=base_prompt,
            spawn=spawn,
            verify=verify,
            **repair_kwargs,
        )
    except RepairExhaustedError as exc:
        fork = repair_exhausted_fork(exc, wave_id=wave_id, attempt=attempt, risk_tier=risk_tier)
        _enqueue_fork(ctx, fork)
        logger.warning(
            f"repair_lane_or_fork wave={wave_id} attempt={attempt} "
            f"criterion={criterion.id!r} status=escalated-to-fork"
        )
        raise


#: Default total-attempt ceiling for :func:`spawn_lane_or_fork` -- DL-11. One
#: initial spawn plus up to ``DEFAULT_MAX_TOTAL_ATTEMPTS - 1`` bounded
#: RETRY_SAME / SWITCH respawns. Bounded so a lane whose agent-cli keeps failing
#: HALTS to a ``retry_exhausted`` fork rather than respawning forever.
DEFAULT_MAX_TOTAL_ATTEMPTS: int = 3


#: A callable that performs one live agent-cli spawn for *wave_id* against a
#: given runtime id, returning the lane dispatch outcome. Injected into
#: :func:`spawn_lane_or_fork` so the bounded retry driver drives a live adapter
#: in production and a recording fake under test. The runtime id is the loop's
#: switch lever: a ``SWITCH_RUNTIME`` action calls this again with the next
#: runtime in the preference ladder. A clean spawn returns the
#: :class:`LaneDispatch`; a failed spawn raises
#: :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` so the driver can
#: classify + bound it (never an infinite respawn).
LaneSpawnFn = Callable[["MethodContext", str, str], "Awaitable[LaneDispatch]"]


class LaneRepairOutcome(BaseModel):
    """Outcome of driving a failing lane through the grounded repair ladder -- W03.

    The loop's :attr:`_Loop.repair` hook returns this when a lane the watcher
    reported a failing-check fork is routed through the bounded grounded repair
    ladder (:func:`repair_lane_or_fork`):

    - ``resolved=True`` -- the repair re-dispatch passed the refused check, so the
      lane is re-registered in flight under *dispatch* (its incremented dispatch
      ``attempt`` IS the cockpit repair counter the lane cell renders).
    - ``resolved=False`` -- the repair budget was spent without the check passing,
      so the hook has already enqueued a ``REPAIR_EXHAUSTED`` fork; the lane stays
      forked.

    Attributes:
        resolved: Whether the repair re-dispatch resolved the refused check.
        attempts_used: The number of grounded-repair attempts the ladder burned
            (the cockpit's ``repair n/<budget>`` counter reads this).
        dispatch: The re-dispatched lane on a resolved repair (carrying the
            incremented attempt + fresh session / pgid); ``None`` on escalation.
    """

    model_config = ConfigDict(extra="forbid")
    resolved: bool
    attempts_used: int = Field(ge=1)
    dispatch: LaneDispatch | None = None


#: Drive one failing lane through the bounded grounded repair ladder -- W03.
#: Given the daemon context + the forked lane, returns the
#: :class:`LaneRepairOutcome` of re-dispatching it up the ladder
#: (:func:`repair_lane_or_fork`): a resolved repair carries the re-dispatched
#: lane (its incremented attempt is the cockpit repair counter), an exhausted
#: one has already enqueued the ``REPAIR_EXHAUSTED`` fork. The daemon wires a
#: live hook; tests inject a deterministic fake so the loop exercises the repair
#: path without a real adapter.
LaneRepairHook = Callable[["MethodContext", FleetLane], LaneRepairOutcome]


#: A HARD spawn-failure class -> the :class:`FleetForkReason` it terminates the
#: lane to (DL-11). Total over the two HARD members of
#: :class:`~eawf.runtime.daemon.dispatch_runner.SpawnFailureClass`
#: (``RECOVERABLE`` is NOT here -- it is handled by the bounded retry ladder, not
#: a clean termination). A new HARD class without a fork row fails fast at import
#: via the totality guard below.
_HARD_FAILURE_FORK_REASON: dict[SpawnFailureClass, FleetForkReason] = {
    SpawnFailureClass.RUNTIME_SPAWN_ERROR: FleetForkReason.RUNTIME_SPAWN_ERROR,
    SpawnFailureClass.SUBPROCESS_OOM: FleetForkReason.SUBPROCESS_OOM,
}

# Totality guard: every HARD spawn-failure class maps to exactly one fork
# reason. Kept as an import-time assertion so a new HARD class without a fork row
# fails fast rather than silently falling through to the retry ladder.
assert set(_HARD_FAILURE_FORK_REASON) == (set(SpawnFailureClass) - {SpawnFailureClass.RECOVERABLE})


#: Max chars a spawn fork's evidence ref carries from the terminal failure
#: detail, bounded so a long stderr dump cannot blow the
#: :attr:`~eawf.kernel.state.models.FleetFork.evidence_ref` field.
_SPAWN_FORK_DETAIL_CAP = 1000


class LaneRetryExhaustedError(RuntimeError):
    """Raised when a lane's bounded spawn-retry budget is spent without a clean spawn -- DL-11.

    Surfaces a TYPED terminal when the bounded ladder (RETRY_SAME then SWITCH)
    spent its whole ``max_total_attempts`` budget -- an auth HALT, a switch
    ladder run out of runtimes, or the attempt ceiling reached -- so the lane
    HALTS to a ``RETRY_EXHAUSTED`` fork rather than respawning forever. Raised by
    :func:`spawn_lane_or_fork` AFTER the fork is enqueued, so a caller that
    catches it knows the lane has already terminated as a queued fork.

    Attributes:
        wave_id: ``W<NN>`` wave whose spawn budget exhausted.
        attempt: 1-based dispatch attempt the exhausted lane was driving.
        error_class: Canonical runtime error class of the terminal failed spawn.
        attempts_used: Number of spawns the bounded ladder burned.
    """

    def __init__(
        self,
        *,
        wave_id: str,
        attempt: int,
        error_class: str,
        attempts_used: int,
    ) -> None:
        self.wave_id = wave_id
        self.attempt = attempt
        self.error_class = error_class
        self.attempts_used = attempts_used
        super().__init__(
            f"lane spawn retry exhausted for wave {wave_id!r} "
            f"after {attempts_used} attempt(s) (error_class={error_class})"
        )


def spawn_failure_fork(
    failure_class: SpawnFailureClass,
    detail: str,
    *,
    wave_id: str,
    attempt: int,
    risk_tier: RiskTier,
) -> FleetFork:
    """Build the HARD-spawn-failure termination fork for a lane -- DL-11, pure.

    A spawn that raised a HARD
    :class:`~eawf.runtime.daemon.dispatch_runner.SpawnFailureClass` -- a
    ``RUNTIME_SPAWN_ERROR`` (ENOENT / permission: the agent CLI cannot be
    launched at all) or a ``SUBPROCESS_OOM`` (the kernel OOM-killer reaped the
    child) -- can NEVER be recovered by a retry or a runtime switch. Rather than
    respawning a binary that is not there or a process that will re-OOM, the loop
    TERMINATES the lane cleanly to an operator-resolved :class:`FleetFork` whose
    reason names the failure class. The fork carries the terminal failure detail
    (normalised to a single bounded line) as its evidence ref so the operator
    reads the concrete launch / OOM failure.

    Args:
        failure_class: The HARD :class:`SpawnFailureClass` the spawn raised --
            must be ``RUNTIME_SPAWN_ERROR`` or ``SUBPROCESS_OOM`` (the totality
            guard rejects ``RECOVERABLE``, which the retry ladder handles).
        detail: The terminal failure detail (the spawn error message) that
            grounds the fork's evidence ref.
        wave_id: ``W<NN>`` wave whose lane terminated.
        attempt: 1-based dispatch attempt -- the second half of the
            ``(wave_id, attempt)`` fork key.
        risk_tier: The lane's resolved :class:`RiskTier` at termination, so the
            cockpit renders the band badge on the queued fork.

    Returns:
        The :class:`FleetFork` to enqueue -- reason mapped from *failure_class*,
        evidence ref carrying the terminal failure detail.

    Raises:
        KeyError: When *failure_class* is ``RECOVERABLE`` (a programming error --
            a recoverable failure is never terminated to a fork; it goes through
            the bounded retry ladder).
    """
    reason = _HARD_FAILURE_FORK_REASON[failure_class]
    normalised = " ".join(detail.split())[:_SPAWN_FORK_DETAIL_CAP]
    evidence_ref = normalised or f"urn:eawf:v1:fork:{wave_id}:{reason.value}"
    fork = FleetFork(
        wave_id=wave_id,
        attempt=attempt,
        risk_tier=risk_tier,
        reason=reason,
        evidence_ref=evidence_ref,
        forked_at=datetime.now(UTC),
    )
    logger.info(
        f"spawn_failure_fork wave={wave_id} attempt={attempt} "
        f"failure_class={failure_class.value} risk_tier={risk_tier.value} "
        f"reason={reason.value}"
    )
    return fork


def spawn_exhausted_fork(
    error_class: str,
    detail: str,
    *,
    wave_id: str,
    attempt: int,
    risk_tier: RiskTier,
) -> FleetFork:
    """Build the ``RETRY_EXHAUSTED`` HALT fork for a spent spawn-retry budget -- DL-11, pure.

    The bounded spawn-retry ladder (RETRY_SAME then SWITCH) spends its whole
    ``max_total_attempts`` budget without a clean spawn -- a rate-limit that
    never clears, or a switch ladder run out of runtimes. Rather than respawning
    the lane forever, the loop HALTS it to an operator-resolved
    :class:`FleetFork` tagged
    :attr:`~eawf.kernel.state.models.FleetForkReason.RETRY_EXHAUSTED`, carrying
    the terminal runtime error class + the last failure detail so the operator
    reads what the final attempt failed on -- never a content-free "it kept
    failing".

    Args:
        error_class: The canonical runtime error class of the terminal failed
            spawn (e.g. ``RUNTIME_RATE_LIMIT`` / ``RUNTIME_API_ERROR``).
        detail: The terminal failure detail (the last spawn error message) that
            grounds the fork's evidence ref.
        wave_id: ``W<NN>`` wave whose spawn budget exhausted.
        attempt: 1-based dispatch attempt -- the second half of the
            ``(wave_id, attempt)`` fork key.
        risk_tier: The lane's resolved :class:`RiskTier` at exhaustion, so the
            cockpit renders the band badge on the queued fork.

    Returns:
        The :class:`FleetFork` to enqueue -- reason ``RETRY_EXHAUSTED``, evidence
        ref carrying the terminal error class + last failure detail.
    """
    normalised = " ".join(detail.split())[:_SPAWN_FORK_DETAIL_CAP]
    evidence_ref = f"{error_class}: {normalised}" if normalised else error_class
    evidence_ref = evidence_ref[:_SPAWN_FORK_DETAIL_CAP]
    fork = FleetFork(
        wave_id=wave_id,
        attempt=attempt,
        risk_tier=risk_tier,
        reason=FleetForkReason.RETRY_EXHAUSTED,
        evidence_ref=evidence_ref,
        forked_at=datetime.now(UTC),
    )
    logger.info(
        f"spawn_exhausted_fork wave={wave_id} attempt={attempt} "
        f"error_class={error_class!r} risk_tier={risk_tier.value} "
        f"reason={FleetForkReason.RETRY_EXHAUSTED.value}"
    )
    return fork


async def spawn_lane_or_fork(
    ctx: MethodContext,
    *,
    runtime: str,
    preference: list[str],
    spawn: LaneSpawnFn,
    classify: ErrorClassifier,
    wave_id: str,
    attempt: int = 1,
    risk_tier: RiskTier = RiskTier.MECH,
    max_total_attempts: int = DEFAULT_MAX_TOTAL_ATTEMPTS,
) -> LaneDispatch:
    """Spawn a lane's agent-cli, bounding failures to a clean fork termination -- DL-11.

    The auto-drain loop spawns agents UNATTENDED, so a lane whose spawn keeps
    failing must terminate cleanly rather than respawn forever. This drives the
    bounded agent-cli failure taxonomy:

    - A clean spawn returns the :class:`LaneDispatch` immediately (no retry).
    - A spawn that raises
      :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` is classified
      via :func:`~eawf.runtime.daemon.dispatch_runner.classify_spawn_failure`:

      - A HARD failure (``RUNTIME_SPAWN_ERROR`` ENOENT / permission, or
        ``SUBPROCESS_OOM``) TERMINATES the lane on the FIRST such failure -- no
        retry or switch can launch a missing binary or un-OOM a reaped process,
        so the loop enqueues a typed termination fork (:func:`spawn_failure_fork`)
        and re-raises rather than looping.
      - A RECOVERABLE failure runs the V5 ladder action: ``RETRY_SAME`` (rate
        limit) respawns the SAME runtime; ``SWITCH_RUNTIME`` resolves the next
        runtime in *preference* and respawns on it; ``HALT`` (auth) stops at
        once. The ladder is BOUNDED by *max_total_attempts* total spawns.

    - When the bounded ladder spends its whole budget without a clean spawn (an
      auth HALT, a switch ladder run out of runtimes, or the attempt ceiling
      reached), the loop HALTS the lane to a ``RETRY_EXHAUSTED`` fork
      (:func:`spawn_exhausted_fork`) and raises -- never an infinite respawn.

    Every terminal path enqueues a typed fork carrying a failure-class + a
    concrete evidence ref through the daemon canonical state writer, so a lane is
    NEVER silently dropped and NEVER respawned forever (success criteria C1 +
    C2).

    Args:
        ctx: Daemon method context -- supplies ``state_path`` (the fork-queue
            write target).
        runtime: Runtime id of the first spawn (the highest-preference runtime
            the caller resolved).
        preference: Ordered ``Wave.runtime_preference`` runtime ladder the V5
            switch walks past the failed runtime. Empty means no switch target.
        spawn: Injected async lane-spawn callable performing one spawn per
            ``(ctx, wave_id, runtime)``; raises ``RuntimeSpawnError`` on a failed
            spawn. The production caller binds the live claim + ``agent.dispatch``
            spawn; a test binds a recording fake (no real subprocess).
        classify: Injected callable mapping a ``RuntimeSpawnError`` (+ the runtime
            it failed on) to a canonical
            :class:`~eawf.runtime.runtimes.adapter.ErrorClass` for the V5 ladder.
        wave_id: ``W<NN>`` wave whose lane this spawns.
        attempt: 1-based dispatch attempt -- the second half of the fork key.
        risk_tier: The lane's resolved :class:`RiskTier`, recorded on any
            termination fork.
        max_total_attempts: Total spawn ceiling (one initial + bounded retries /
            switches). Must be at least 1.

    Returns:
        The :class:`LaneDispatch` of the first spawn that succeeded.

    Raises:
        ValueError: When *max_total_attempts* is less than 1.
        RuntimeSpawnError: When a HARD spawn failure (ENOENT / permission / OOM)
            terminated the lane -- re-raised AFTER the typed termination fork is
            enqueued, so the lane terminates as a fork (C2).
        LaneRetryExhaustedError: When the bounded retry budget is spent without a
            clean spawn -- raised AFTER the ``RETRY_EXHAUSTED`` fork is enqueued
            (C1).
    """
    if max_total_attempts < 1:
        raise ValueError(f"max_total_attempts must be >= 1: {max_total_attempts!r}")

    current_runtime = runtime
    last_error_class = "RUNTIME_API_ERROR"
    last_detail = "lane spawn failed"
    for spawn_attempt in range(1, max_total_attempts + 1):
        try:
            return await spawn(ctx, wave_id, current_runtime)
        except RuntimeSpawnError as exc:
            failure_class = classify_spawn_failure(exc)
            detail = f"{exc}"[:_SPAWN_FORK_DETAIL_CAP] or "lane spawn failed"
            if failure_class is not SpawnFailureClass.RECOVERABLE:
                # HARD failure (ENOENT / permission / OOM): no retry or switch
                # can recover it. Terminate the lane cleanly to a typed fork on
                # the FIRST such failure (C2) rather than respawning forever.
                fork = spawn_failure_fork(
                    failure_class,
                    detail,
                    wave_id=wave_id,
                    attempt=attempt,
                    risk_tier=risk_tier,
                )
                _enqueue_fork(ctx, fork)
                logger.warning(
                    f"spawn_lane_or_fork wave={wave_id} attempt={attempt} "
                    f"failure_class={failure_class.value} status=terminated-to-fork"
                )
                raise
            error_class = classify(exc, current_runtime)
            action = fallback_action(error_class)
            last_error_class = error_class
            last_detail = detail
            logger.info(
                f"spawn_lane_or_fork wave={wave_id} spawn_attempt={spawn_attempt} "
                f"runtime={current_runtime!r} status=failed error_class={error_class} "
                f"action={action.value}"
            )
            if action is FallbackAction.HALT:
                # Auth never auto-retries: stop at once and HALT to a fork rather
                # than burning the remaining attempt budget.
                break
            if action is FallbackAction.SWITCH_RUNTIME:
                next_runtime = next_runtime_on_error(
                    failed_runtime=current_runtime,
                    preference=preference,
                    error_class=error_class,
                )
                if next_runtime is None:
                    # The preference ladder is exhausted -- no runtime left to
                    # switch to, so the spawn budget is terminal.
                    break
                logger.info(
                    f"spawn_lane_or_fork wave={wave_id} spawn_attempt={spawn_attempt} "
                    f"status=switching from={current_runtime!r} to={next_runtime!r}"
                )
                current_runtime = next_runtime
            # RETRY_SAME falls through with current_runtime unchanged.

    # The bounded budget is spent without a clean spawn: HALT the lane to a
    # RETRY_EXHAUSTED fork (C1) -- never respawn forever, never silent-drop.
    fork = spawn_exhausted_fork(
        last_error_class,
        last_detail,
        wave_id=wave_id,
        attempt=attempt,
        risk_tier=risk_tier,
    )
    _enqueue_fork(ctx, fork)
    logger.warning(
        f"spawn_lane_or_fork wave={wave_id} attempt={attempt} "
        f"error_class={last_error_class!r} status=retry-exhausted-to-fork"
    )
    raise LaneRetryExhaustedError(
        wave_id=wave_id,
        attempt=attempt,
        error_class=last_error_class,
        attempts_used=max_total_attempts,
    )


def _drive_coro(coro: Awaitable[Any]) -> Any:
    """Run *coro* to completion from the synchronous fleet loop -- W03.

    The fleet drain is synchronous (it runs on the W01 worker thread, or inline
    for a test caller), but the bounded spawn ladder
    (:func:`spawn_lane_or_fork`) and the grounded repair ladder
    (:func:`repair_lane_or_fork`) are async. This bridges the two: when no event
    loop is running on the current thread (the normal case -- the worker drive
    thread + the synchronous test callers) it runs the coroutine on a fresh loop
    via :func:`asyncio.run`; when a loop IS running (a coroutine driving the
    loop, e.g. a test that awaits the drive on the event loop) it runs the
    coroutine on a fresh loop in a dedicated worker thread so it never nests an
    ``asyncio.run`` inside the running loop.

    Args:
        coro: The awaitable the synchronous loop drives to completion.

    Returns:
        The coroutine's result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()  # type: ignore[arg-type]


@dataclass
class _Loop:
    """In-memory driver of one fleet auto-drain run.

    Holds the live :class:`FleetRun` plus the injected spawn / watch callables
    and the daemon context. Every transition that mutates ``run`` re-persists
    it through :func:`_persist_fleet_run` so the daemon canonical writer is the
    sole path ``state.json`` advances.

    Attributes:
        ctx: Daemon method context threaded into the spawner / watcher.
        run: The live run snapshot the loop advances + persists.
        spawn: The injected lane spawner.
        watch: The injected lane watcher.
        block_authority: The jury's earned authority for this run -- gates
            whether a high / ui lane may auto-close or must fork. An
            uncalibrated jury is :attr:`BlockAuthority.ADVISORY` (the default),
            so high / ui lanes fork under it.
        risk_tiers: The per-lane RiskTier badge registry, keyed by wave id. The
            tier is resolved + recorded when a lane is filled (the cockpit reads
            it for the lane badge) and drives the auto-close / fork gate when the
            lane finishes.
        spend: The injected per-lane spend reader. As each lane finishes its
            EU + USD spend is accumulated into the run's spend counters, so the
            DL-4 budget cap tests against live figures.
        fork_evidence: The injected fork-evidence reader. When a lane pauses to
            a DL-6 blocking fork the reader supplies the evidence ref the queued
            :class:`FleetFork` carries.
        cancel: Optional shutdown event the loop checks between rounds (W01).
        classify: Optional :class:`ErrorClassifier` for the bounded spawn ladder
            (W03). When set, a lane spawn that raises ``RuntimeSpawnError`` is
            routed through :func:`spawn_lane_or_fork` so a HARD failure
            terminates the lane to a typed fork on the first failure and a
            RECOVERABLE one retries up the bounded ladder -- a spawn error FORKS
            the lane rather than aborting the whole run. ``None`` (the default)
            keeps the pre-W03 direct-spawn path for the synchronous in-process
            callers + the W01 spawner fakes.
        runtime_preference: The ``Wave.runtime_preference`` runtime ladder the
            bounded spawn ladder's V5 switch walks past a failed runtime (W03).
        max_total_attempts: Total spawn ceiling for the bounded spawn ladder.
        repair: Optional repair hook the loop invokes on a failing-check fork
            (W03). When set, a lane the watcher reports ``"forked"`` on a failing
            check is routed through it to re-dispatch up the bounded grounded
            repair ladder (:func:`repair_lane_or_fork`); the resolved lane's
            dispatch attempt advances so the cockpit repair counter reflects the
            real attempts. ``None`` (the default) keeps the pre-W03 terminal-fork
            behaviour.
        recompute_frontier: When ``True`` (the default, W04) the loop recomputes
            the ready frontier from ``state.json`` each round and merges
            newly-unblocked waves onto the run frontier, so a dep chain drains to
            empty in one run as each layer closes. ``False`` freezes the frontier
            at arm time (the pre-W04 fixed-frontier behaviour) -- used where the
            on-disk wave graph is not the claim source (e.g. a synthetic-frontier
            test that does not flip wave status).
        claimed_ids: The wave ids the loop has already claimed in THIS run; the
            recompute excludes them so an already-dispatched wave never re-joins
            the frontier.
    """

    ctx: MethodContext
    run: FleetRun
    spawn: LaneSpawner
    watch: LaneWatcher
    block_authority: BlockAuthority = BlockAuthority.ADVISORY
    risk_tiers: dict[str, RiskTier] = field(default_factory=dict)
    spend: LaneSpendReader = _default_lane_spend
    fork_evidence: ForkEvidenceReader = _default_fork_evidence
    cancel: threading.Event | None = None
    classify: ErrorClassifier | None = None
    runtime_preference: list[str] = field(default_factory=list)
    max_total_attempts: int = DEFAULT_MAX_TOTAL_ATTEMPTS
    repair: LaneRepairHook | None = None
    recompute_frontier: bool = True
    #: Wave ids the loop has already claimed in THIS run (W04). The per-round
    #: frontier recompute excludes these so a wave the loop already dispatched is
    #: never re-claimed -- robust whether or not the spawner advanced the wave's
    #: ``state.json`` status (a fake spawner that leaves the wave PENDING would
    #: otherwise re-appear as ready every round).
    claimed_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Seed ``claimed_ids`` from any lane already in flight at construction.

        A run handed to the loop with lanes already registered (a resume or a
        reattach of a run that was mid-drain) has already claimed those waves;
        seed them so the W04 per-round recompute never re-adds an in-flight wave
        to the frontier (the live watcher flips a closed wave's status, but a
        resumed run's lane must be excluded from the recompute regardless).
        """
        self.claimed_ids |= set(self.run.lanes)

    def _persist(self) -> None:
        """Persist the current run snapshot through the daemon canonical writer.

        Operator intervention wins (W06): a ``fleet.pause`` / ``fleet.halt`` RPC
        can land on disk MID-round (between the round's top run-state read and
        this persist), so before writing a DRAINING in-memory run this re-reads
        the disk run-state and adopts a HELD (PAUSED / HALTED) state -- otherwise
        the loop's own persist would clobber the operator's pause / halt and the
        run would keep claiming. A run already in a HELD / terminal state, or a
        stateless context, writes unchanged.
        """
        if self.run.run_state is FleetRunState.DRAINING:
            disk = self._disk_run_state()
            if disk in {FleetRunState.PAUSED, FleetRunState.HALTED}:
                assert disk is not None
                self.run.run_state = disk
        _persist_fleet_run(self.ctx, self.run)

    def _recompute_frontier(self) -> None:
        """Recompute the ready frontier from state + merge newly-unblocked waves -- W04.

        The frontier is no longer frozen at arm time: each round the loop reloads
        the live wave graph off ``state.json`` (free read access), projects it
        into the shared :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`
        view (the SAME claim-time gate the autopilot pane + the claim transition
        use), and merges any newly-ready wave that is not already tracked onto the
        run frontier. So a dep chain armed at its first layer drains to empty in
        one run: as each layer's waves close, their dependents become ready and
        join the frontier on the next round.

        The merge is purely ADDITIVE + idempotent. A candidate is appended only
        when it is not already (a) in flight (:attr:`FleetRun.lanes`), (b) queued
        on the frontier, (c) already claimed this run (:attr:`claimed_ids` --
        robust even when the spawner does not flip the wave's on-disk status), or
        (d) carrying a queued fork (a forked wave is the operator's to resolve,
        never silently re-claimed). Newly-ready waves are appended in claim order
        (natural id order) so the frontier stays deterministic.

        A no-op when :attr:`recompute_frontier` is ``False`` or no ``state_path``
        is configured (a synthetic-frontier driver) -- the frontier then stays
        frozen at the armed list.
        """
        if not self.recompute_frontier or self.ctx.state_path is None:
            return
        state = load_state(Path(self.ctx.state_path))
        items = tuple(
            WaveFrontierItem(
                wave_id=wave.id,
                iter_id=wave.iter_id,
                status=wave.status,
                deps=tuple(wave.deps),
            )
            for wave in state.waves.values()
        )
        if not items:
            return
        ready = compute_ready_frontier(items)
        tracked = (
            set(self.run.lanes)
            | set(self.run.frontier)
            | self.claimed_ids
            | {fork.wave_id for fork in self.run.forks}
        )
        added = [wid for wid in ready.ready_ids if wid not in tracked]
        if added:
            self.run.frontier.extend(added)
            logger.info(
                f"_recompute_frontier added={added} frontier_depth={len(self.run.frontier)}"
            )

    def _fill_lanes(self) -> None:
        """Claim + dispatch frontier waves into every free lane.

        Pops from the head of the frontier until either the lane count hits
        the concurrency cap or the frontier empties. Each pop claims +
        dispatches the wave (via the injected spawner) and **registers** the
        live lane: the spawned child's ``pgid`` keyed by ``(wave_id, attempt)``
        lands on :attr:`FleetLane.pgid` so the kill / reattach paths resolve a
        real OS process group. A spawn that returned no pid records
        ``pgid=None`` (the lane is unkillable). The claimed / dispatched
        counters advance per wave.

        The DL-4 budget cap gates the claim step: once an armed spend cap is
        reached (:func:`budget_exhausted`) the fill claims no further wave even
        with free lanes and frontier work remaining, so the run stops growing
        spend past its cap. In-flight lanes are left for the drain / hard-halt
        branch to resolve.
        """
        while len(self.run.lanes) < self.run.concurrency and self.run.frontier:
            if budget_exhausted(self.run):
                # A spend cap fired: stop claiming new waves (the in-flight
                # lanes are resolved by the drain / hard-halt branch).
                break
            wave_id = self.run.frontier.pop(0)
            # Record the claim so the per-round frontier recompute (W04) never
            # re-adds a wave this run already dispatched (robust even when the
            # spawner does not flip the wave's on-disk status).
            self.claimed_ids.add(wave_id)
            # PARK a redispatch of a wave that is already terminal: an armed /
            # reattached frontier can carry a wave that has since CLOSED (or
            # FAILED / ABANDONED) -- e.g. a stale run whose armed wave the
            # operator closed by hand. Re-claiming it fails deep in the spawner
            # every round, so without this guard the run churns the closed wave
            # forever (reattach -> drained -> done -> paused every 1-2s). Count
            # it as a parked failure + drop it (claimed_ids already blocks a
            # re-add) so the run can converge instead of retrying indefinitely.
            if self._wave_terminal_on_disk(wave_id):
                self.run.counters.claimed += 1
                self.run.counters.dispatched += 1
                self.run.counters.failed += 1
                logger.info(f"_fill_lanes wave={wave_id} status=redispatch-parked reason=terminal")
                continue
            # Resolve the lane's RiskTier badge up front so a spawn that FORKS
            # (a HARD spawn failure / a retry-exhausted ladder) records the band
            # on the queued fork too.
            risk_tier = _lane_risk_tier(self.ctx, wave_id)
            try:
                dispatch = self._spawn_lane(wave_id, risk_tier)
            except RuntimeSpawnError, LaneRetryExhaustedError:
                # The two errors the bounded spawn ladder OWNS. When the ladder
                # is wired, _spawn_lane already absorbs both into a None return
                # (the fork path below), so they never reach here. Without a
                # classifier (the pre-W03 opt-out direct-spawn path), a raised
                # RuntimeSpawnError keeps PROPAGATING -- the ladder is opt-in, so
                # the existing synchronous callers + fakes stay unaffected.
                raise
            except Exception as exc:
                # A dispatch error the bounded spawn ladder does NOT model (it
                # only forks RuntimeSpawnError / retry-exhaustion -- both handled
                # above). An error raised DEEP in the dispatch (e.g. an LLM-assist
                # failure mid-prompt) otherwise escapes _fill_lanes, leaves the
                # wave stuck CLAIMED with zero counters bumped, and corrupts the
                # run summary. Record it as a terminal FAILED outcome (mirroring
                # the genuine-watcher fork accounting in _finish_lane), advance
                # the wave off CLAIMED so a reattach never re-claims it, and
                # continue the drive -- one wave's dispatch error never aborts
                # the whole run.
                #
                # Only claimed / dispatched / failed advance here -- the FAILED
                # is recorded directly, NOT enqueued as a FleetFork, so the run
                # summary invariant ``closed + failed + blocked == dispatched``
                # holds. ``forked`` is left for the fork-queue paths whose disk
                # enqueue the per-round ``_resync_forks_from_disk`` reloads (a
                # raw in-memory ``forked`` bump would be clobbered by a later
                # same-round resync), and ``blocked`` is the safety-hold tally,
                # not a genuine failure.
                self._fail_wave_on_disk(wave_id, exc)
                self.run.counters.claimed += 1
                self.run.counters.dispatched += 1
                self.run.counters.failed += 1
                logger.warning(
                    f"_fill_lanes wave={wave_id} status=dispatch-failed error={type(exc).__name__}"
                )
                continue
            if dispatch is None:
                # The bounded spawn ladder terminated the lane to a queued fork
                # (a HARD failure or a spent retry budget): the run is NOT
                # aborted -- the loop counts the fork + moves to the next wave.
                # The ladder enqueued the fork DIRECTLY through the canonical
                # writer (it bumped forked + blocked + appended to forks on
                # disk), so absorb that disk-side fork into the in-memory run
                # before the loop's next persist would otherwise clobber it.
                self._resync_forks_from_disk()
                self.run.counters.claimed += 1
                self.run.counters.dispatched += 1
                logger.info(f"_fill_lanes wave={wave_id} status=spawn-forked")
                continue
            self.run.lanes[wave_id] = FleetLane(
                wave_id=wave_id,
                attempt=dispatch.attempt,
                session_id=dispatch.session_id,
                pgid=dispatch.pgid,
                dispatched_at=datetime.now(UTC),
            )
            # The cockpit reads the lane's RiskTier badge + drives the drain-time
            # auto-close / fork gate from this registry.
            self.risk_tiers[wave_id] = risk_tier
            self.run.counters.claimed += 1
            self.run.counters.dispatched += 1
            logger.info(
                f"_fill_lanes wave={wave_id} attempt={dispatch.attempt} "
                f"session={dispatch.session_id!r} pgid={dispatch.pgid} "
                f"killable={dispatch.pgid is not None} risk_tier={risk_tier.value}"
            )

    def _spawn_lane(self, wave_id: str, risk_tier: RiskTier) -> LaneDispatch | None:
        """Spawn one lane, routing failures through the bounded spawn ladder -- W03.

        When no :class:`ErrorClassifier` is wired (the synchronous in-process
        callers + the W01 spawner fakes) this calls the injected spawner
        directly + normalises its return, unchanged from the pre-W03 path.

        When *classify* IS wired (the live drive), the spawn routes through the
        bounded spawn ladder (:func:`spawn_lane_or_fork`): a clean spawn returns
        its :class:`LaneDispatch`, a HARD spawn failure (ENOENT / OOM)
        TERMINATES the lane to a typed fork on the first failure, and a
        RECOVERABLE failure retries up the bounded ladder (RETRY_SAME / SWITCH)
        before HALTing to a ``RETRY_EXHAUSTED`` fork. Either terminal enqueues
        the fork through the daemon canonical writer + re-raises; this catches
        the re-raise and returns ``None`` so the loop counts the fork + advances
        to the next wave rather than ABORTING the whole run on one spawn error.

        Args:
            wave_id: ``W<NN>`` wave to spawn into a lane.
            risk_tier: The lane's resolved :class:`RiskTier`, recorded on any
                termination fork the ladder enqueues.

        Returns:
            The :class:`LaneDispatch` of a clean spawn, or ``None`` when the
            bounded ladder terminated the lane to a queued fork.
        """
        if self.classify is None:
            return _normalise_dispatch(self.spawn(self.ctx, wave_id))

        classify = self.classify

        async def _spawn_once(ctx: MethodContext, wid: str, _runtime: str) -> LaneDispatch:
            # The injected spawner is sync; a RuntimeSpawnError it raises is the
            # signal the bounded ladder classifies + bounds. A clean return is
            # normalised to a LaneDispatch.
            return _normalise_dispatch(self.spawn(ctx, wid))

        runtime = self.runtime_preference[0] if self.runtime_preference else "claude-code"
        try:
            dispatch: LaneDispatch | None = _drive_coro(
                spawn_lane_or_fork(
                    self.ctx,
                    runtime=runtime,
                    preference=list(self.runtime_preference),
                    spawn=_spawn_once,
                    classify=classify,
                    wave_id=wave_id,
                    attempt=1,
                    risk_tier=risk_tier,
                    max_total_attempts=self.max_total_attempts,
                )
            )
            return dispatch
        except RuntimeSpawnError, LaneRetryExhaustedError:
            # The bounded ladder already enqueued the typed termination fork
            # through the daemon canonical writer; the lane is forked, not
            # registered. The run continues (it is NOT aborted on a spawn error).
            return None

    def _resync_forks_from_disk(self) -> None:
        """Absorb a ladder-enqueued fork from disk into the in-memory run -- W03.

        The bounded spawn ladder (:func:`spawn_lane_or_fork`) and the grounded
        repair ladder (:func:`repair_lane_or_fork`) enqueue their termination
        forks DIRECTLY through the daemon canonical writer (:func:`_enqueue_fork`
        bumps ``forked`` + ``blocked`` + appends to ``forks`` on disk). The loop
        holds an in-memory run it re-persists each round, so without re-reading
        the disk fork the loop's next ``_persist()`` would clobber it. This
        reloads the persisted run's fork queue + the two safety tallies into the
        in-memory run so the disk-side fork survives the next persist. A
        stateless context (no on-disk run) is a no-op -- the ladder cannot have
        enqueued a fork there.
        """
        if self.ctx.state_path is None:
            return
        persisted = load_state(Path(self.ctx.state_path)).fleet_run
        if persisted is None:
            return
        self.run.forks = list(persisted.forks)
        self.run.counters.forked = persisted.counters.forked
        self.run.counters.blocked = persisted.counters.blocked

    def _wave_terminal_on_disk(self, wave_id: str) -> bool:
        """Return whether *wave_id* is already terminal (or gone) on disk.

        A wave that has reached ``CLOSED`` / ``FAILED`` / ``ABANDONED`` -- or
        vanished from state entirely (e.g. a plan edit removed it) -- cannot be
        (re)dispatched, so :meth:`_fill_lanes` parks it rather than churning a
        doomed claim every round. A stateless driver (no ``state_path`` -- the
        synchronous in-process fakes) returns ``False`` so the injected spawner
        stays in control of the lane's fate.

        Args:
            wave_id: ``W<NN>`` wave the fill is about to claim.

        Returns:
            ``True`` when the wave is terminal or absent on disk.
        """
        if self.ctx.state_path is None:
            return False
        wave = load_state(Path(self.ctx.state_path)).waves.get(wave_id)
        if wave is None:
            return True
        return wave.status in {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}

    def _fail_wave_on_disk(self, wave_id: str, exc: BaseException) -> None:
        """Advance a dispatch-failed wave off CLAIMED to terminal FAILED on disk.

        A wave whose dispatch raised after the claim landed is stuck CLAIMED on
        disk (the spawner claimed it, then the dispatch aborted). This drives the
        canonical :func:`eawf.workflow.lifecycle.wave.fail_wave` transition under
        the state portalock so the wave reaches a terminal FAILED status carrying
        the failure reason + drops off ``current.active_wave_ids`` -- a reattach
        then never re-claims it. The write goes through the daemon canonical
        writer (locked atomic write), never a hand-rolled state mutation. A
        stateless context (no ``state_path``) is a no-op; an already-terminal
        wave (a concurrent close) is left untouched rather than faulting the
        whole drive on the cleanup write.

        Args:
            wave_id: ``W<NN>`` wave whose claimed lane failed to dispatch.
            exc: The dispatch error driving the failure -- its type + message
                ground the recorded fail reason.
        """
        if self.ctx.state_path is None:
            return
        reason = f"fleet dispatch error: {type(exc).__name__}: {exc}"[:_SPAWN_FORK_DETAIL_CAP]
        state_path = Path(self.ctx.state_path)
        with portalock.acquire(state_path, timeout=5.0):
            state = load_state(state_path)
            wave = state.waves.get(wave_id)
            if wave is None or wave.status in {
                WaveStatus.CLOSED,
                WaveStatus.FAILED,
                WaveStatus.ABANDONED,
            }:
                # The wave is gone or already terminal (a concurrent close): the
                # cleanup has nothing to advance, so leave disk untouched.
                return
            fail_wave(state, wave_id=wave_id, reason=reason)
            state.updated_at = datetime.now(UTC)
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
        logger.info(f"_fail_wave_on_disk wave={wave_id} status=failed reason={reason!r}")

    def _close_wave_on_disk(self, wave_id: str) -> None:
        """Close a still-open wave whose lane resolved CLOSED -- W49.

        A lane the watcher resolved ``"closed"`` from a persisted close-ready
        report (a SANDBOXED headless agent that could not run ``eawf wave close``
        itself) is still IN_PROGRESS on disk; the gated outcome reaching the
        closed branch of :meth:`_finish_lane` means the DL-5 auto-close gate
        already permitted the close (a high-risk / ui close was downgraded to a
        fork upstream, so it never reaches here). Flip the wave to CLOSED through
        the canonical :func:`~eawf.workflow.lifecycle.wave.close_wave` transition
        under the state portalock so the wave matches the run's closed tally. A
        stateless context, a vanished wave, or a wave the agent DID self-close
        (already terminal) is a no-op rather than a fault.

        Args:
            wave_id: ``W<NN>`` wave whose lane resolved a clean close.
        """
        if self.ctx.state_path is None:
            return
        state_path = Path(self.ctx.state_path)
        with portalock.acquire(state_path, timeout=5.0):
            state = load_state(state_path)
            wave = state.waves.get(wave_id)
            if wave is None or wave.status not in {
                WaveStatus.CLAIMED,
                WaveStatus.IN_PROGRESS,
            }:
                # Gone or already terminal (the agent self-closed): nothing to flip.
                return
            # Convert the headless spawn's captured runtime (W50 stamped the
            # baseline + latest) into the close-time actuals: without this the
            # ActualSummary records elapsed_eu=0 / cost=0, so home effort reads
            # 0.0, variance -100%, and the cost tab has no model.
            delta = compute_runtime_delta(
                wave.runtime_baseline,
                wave.runtime_latest,
                eu_minutes=DEFAULT_EU_MINUTES,
                eu_basis=EuBasis.API_DURATION,
            )
            close_wave(
                state,
                wave_id=wave_id,
                outcome="autopilot: report close-ready; closed on behalf of sandboxed agent",
                tokens_consumed=delta.actual_tokens if delta is not None else None,
                actual_elapsed_eu=delta.elapsed_eu if delta is not None else None,
                actual_agent_runtime_eu=delta.agent_runtime_eu if delta is not None else None,
                actual_cost_usd=delta.actual_cost_usd if delta is not None else None,
            )
            now = datetime.now(UTC)
            closed_sessions = self._finalize_wave_sessions(state, wave_id=wave_id, now=now)
            state.updated_at = now
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
        logger.info(
            f"_close_wave_on_disk wave={wave_id} status=closed sessions_closed={closed_sessions}"
        )

    def _run_close_gates(self, wave_id: str) -> CloseGateResult | None:
        """Run *wave_id*'s deterministic close gates before a clean close -- W19.

        A lane the watcher resolved ``"closed"`` from the agent's self-report is
        about to flip to CLOSED via :meth:`_close_wave_on_disk` -- a pure status
        flip that never runs the wave's own gates. This scores the wave's
        required deterministic-gated criteria through the shared ordered oracle
        (:func:`eawf.workflow.verify.dispatch_close.run_close_gates`), so a wave
        carrying a ``command_exit_zero`` gate has that command RUN before the
        close. A failing gate yields ``passed=False`` (the caller routes the lane
        to the repair/fork ladder); a passing gate yields the minted
        deterministic-pass evidence rows the caller appends after the close
        commits. A stateless context or a vanished wave returns ``None`` (nothing
        to gate -- the close proceeds unchanged).

        The oracle is async; the synchronous drain drives it through
        :func:`_drive_coro`, the same bridge the spawn / repair ladders use.

        Args:
            wave_id: ``W<NN>`` wave whose clean close is gated.

        Returns:
            The :class:`~eawf.workflow.verify.dispatch_close.CloseGateResult`, or
            ``None`` when there is no state / wave to gate.
        """
        from eawf.runtime.daemon.methods.state import _config_root_for_state_path

        if self.ctx.state_path is None:
            return None
        state_path = Path(self.ctx.state_path)
        state = load_state(state_path)
        wave = state.waves.get(wave_id)
        if wave is None:
            return None
        events_path = (
            Path(self.ctx.event_path)
            if self.ctx.event_path is not None
            else store_path(state_path, StoreKind.EVENT)
        )
        result: CloseGateResult = _drive_coro(
            run_close_gates(
                wave,
                state=state,
                state_path=state_path,
                events_path=events_path,
                repo_root=_config_root_for_state_path(state_path),
            )
        )
        return result

    def _finalize_wave_sessions(self, state: State, *, wave_id: str, now: datetime) -> int:
        """Close the wave's still-ACTIVE executor session(s) on behalf of the agent.

        Close-on-behalf flips the WAVE to closed, but a sandboxed headless agent
        never ran its own session teardown, so its executor
        :class:`~eawf.kernel.state.models.AgentSession` stays ``ACTIVE``. The
        Watch parity grid lays out one tile per ACTIVE executor session, so an
        un-finalized session keeps surfacing a closed wave as a live lane. Move
        each matching session to ``CLOSED`` (emitting a ``session.close`` event)
        so the watch surface drops it. A stateless context (no event path) closes
        no session and is a no-op.

        Args:
            state: The loaded state, mutated in place under the caller's lock.
            wave_id: The wave whose executor sessions to finalize.
            now: The close timestamp stamped on each session.

        Returns:
            The number of executor sessions moved to ``CLOSED``.
        """
        if self.ctx.event_path is None:
            return 0
        from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
        from eawf.runtime.session.store import close_session

        stale = [
            sid
            for sid, sess in state.agent_sessions.items()
            if sess.scope_id == wave_id
            and sess.role is AgentSessionRole.EXECUTOR
            and sess.status is AgentSessionStatus.ACTIVE
        ]
        for sid in stale:
            close_session(
                state=state,
                events_path=Path(self.ctx.event_path),
                session_id=sid,
                status=AgentSessionStatus.CLOSED,
                summary="autopilot: closed on behalf of sandboxed agent",
                now=now,
            )
        return len(stale)

    def _drain_lanes(self) -> bool:
        """Watch in-flight lanes to their terminal outcome, freeing each slot.

        Watches each open lane to completion (the watcher blocks until
        terminal); a ``closed`` outcome increments the closed counter and frees
        the slot, while a ``forked`` outcome increments the fork counter
        (resetting the convergence streak) and frees the slot. Each finished
        lane's EU + USD spend accrues onto the run's spend counters so the DL-4
        budget cap tests against live figures.

        Freeing the slot **deregisters** the lane from the per-lane process
        registry (``del`` drops its ``(wave_id, attempt) -> pgid`` row), so the
        registry holds exactly the still-in-flight lanes -- a closed lane's
        pgid is no longer a live kill / reattach target.

        The drain stops EARLY the moment a finished lane's spend pushes the run
        over an armed cap WHILE the frontier still holds queued work (the DL-4
        budget stop): the remaining un-watched lanes are left in flight for the
        :meth:`_budget_terminal` branch to drain or kill, so a hard-halt run
        has live lanes to reap rather than already-finished slots. A drain that
        empties every lane without a budget stop counts a clean / forked round.

        Returns:
            ``True`` when the drain stopped early on a fired budget cap (with
            frontier work still queued), else ``False`` -- the full round
            completed and every lane was freed.
        """
        had_fork = False
        budget_stopped = False
        for wave_id in list(self.run.lanes):
            if self._finish_lane(wave_id) == "forked":
                had_fork = True
            if self.run.frontier and budget_exhausted(self.run):
                # The just-finished lane pushed spend over an armed cap and the
                # frontier still holds queued work: stop watching the remaining
                # in-flight lanes so the budget-terminal branch resolves them.
                budget_stopped = True
                break
        self.run.counters.rounds += 1
        if had_fork:
            self.run.counters.clean_rounds = 0
        else:
            self.run.counters.clean_rounds += 1
        return budget_stopped

    def _finish_lane(self, wave_id: str) -> LaneOutcome:
        """Watch one lane to terminal, gate it, deregister it, and accrue spend.

        The per-lane half of a drain: watches the lane to its terminal outcome,
        gates the outcome through the lane's RiskTier (a high / ui ``closed``
        under an unearned advisory jury is downgraded to a fork so it never
        silently auto-closes -- the DL-5 safety invariant), deregisters the lane
        from the registry, bumps the closed / forked counter, and accrues the
        finished lane's EU + USD spend onto the run's spend counters (a lane
        with no captured runtime adds zero, so the cap never moves on phantom
        spend). Shared by :meth:`_drain_lanes` and the :meth:`_budget_terminal`
        graceful drain so both finish a lane identically.

        A forked lane is split across the two FA7 run-summary tallies: a lane
        the watcher reported a genuine fork (``watched == "forked"``) bumps
        ``failed``, while a clean close DOWNGRADED to a fork by the DL-5 safety
        gate (``watched == "closed"`` but ``outcome == "forked"``) bumps
        ``blocked`` instead, so the summary distinguishes a real failure from a
        safety hold.

        DL-6: when a lane pauses to a BLOCKING fork -- a high-risk close, an
        uncalibrated-jury advisory (the downgraded clean close), or a
        needs-user split -- it is enqueued as a typed :class:`FleetFork` on
        :attr:`FleetRun.forks` (the lane is already removed from ``lanes``, so
        ONLY that lane pauses while the sibling lanes the drain still walks keep
        draining). A genuine watcher fork is NOT enqueued -- it is a terminal
        failure, not an operator pause.

        DL-7 (W03): when a genuine watcher fork (a failing check) occurs AND a
        repair hook is wired, the lane is first routed through the bounded
        grounded repair ladder (:func:`repair_lane_or_fork`): a resolved repair
        RE-REGISTERS the lane in flight under its incremented dispatch attempt
        (so the loop watches it again and the cockpit repair counter advances),
        and an exhausted ladder leaves the enqueued ``REPAIR_EXHAUSTED`` fork
        the hook already queued. A re-dispatched lane returns ``"running"`` so
        the round is not counted clean and the drain continues to watch it.

        Args:
            wave_id: ``W<NN>`` wave whose in-flight lane to finish.

        Returns:
            The gated :data:`LaneOutcome` (``"closed"``, ``"forked"``, or
            ``"needs_user"``). A needs-user lane reports ``"forked"`` to the
            convergence streak so the round is not counted clean.
        """
        lane = self.run.lanes[wave_id]
        attempt = lane.attempt
        watched = self.watch(self.ctx, lane)
        risk_tier = self.risk_tiers.pop(wave_id, RiskTier.MECH)
        outcome = gate_lane_outcome(watched, risk_tier, block_authority=self.block_authority)
        # W19: a lane DL-5 would clean-close on the agent's self-report must first
        # pass the wave's DETERMINISTIC gates -- a failing command_exit_zero gate
        # never flips status on a clean report; it routes to the repair/fork ladder
        # below. The gate runs only on the clean-close branch, so a genuine watcher
        # fork / needs-user pause is unaffected. A passing gate mints
        # deterministic-pass evidence appended after the close commits.
        close_gate_evidence: list[EvidenceRecord] = []
        if outcome == "closed":
            gate_result = self._run_close_gates(wave_id)
            if gate_result is not None and not gate_result.passed:
                logger.warning(
                    f"_finish_lane wave={wave_id} close_gate=blocked "
                    f"criterion={gate_result.failing_criterion_id!r} routes_to=repair_fork"
                )
                # A deterministic gate refusal is a genuine failing check, not a
                # DL-6 operator pause: route it through the existing failing-check
                # ladder below. classify_fork_reason(watched="closed", MECH/MED) is
                # already None, so overriding the gated outcome to "forked" reuses
                # the repair (or terminal-fork) arm without a spurious pause.
                outcome = "forked"
            elif gate_result is not None:
                close_gate_evidence = gate_result.evidence
        del self.run.lanes[wave_id]
        fork_reason = classify_fork_reason(watched, risk_tier, block_authority=self.block_authority)
        if fork_reason is not None:
            # A BLOCKING fork: pause ONLY this lane to the fork queue (the lane
            # is already deregistered above) and leave the sibling lanes
            # draining. The downgraded-close hold + the needs-user split both
            # land on the ``blocked`` safety tally.
            self.run.counters.forked += 1
            self.run.counters.blocked += 1
            self.run.forks.append(
                FleetFork(
                    wave_id=wave_id,
                    attempt=attempt,
                    risk_tier=risk_tier,
                    reason=fork_reason,
                    evidence_ref=self.fork_evidence(self.ctx, wave_id, fork_reason),
                    forked_at=datetime.now(UTC),
                )
            )
        elif outcome == "forked" and self.repair is not None:
            # A genuine watcher fork (a failing check) with a repair hook wired:
            # re-dispatch up the bounded grounded repair ladder (DL-7) before
            # counting it a terminal failure.
            repaired = self._repair_lane(wave_id, lane, risk_tier)
            if repaired is not None:
                # The repair re-dispatch resolved the refused check: re-register
                # the lane in flight under its incremented attempt (the cockpit
                # repair counter) so the loop watches it again. Restore the
                # RiskTier badge popped above.
                self.run.lanes[wave_id] = repaired
                self.risk_tiers[wave_id] = risk_tier
                logger.info(
                    f"_finish_lane wave={wave_id} status=repaired attempt={repaired.attempt}"
                )
                return "running"
            # The repair ladder was spent: the hook drove repair_lane_or_fork,
            # which enqueued the REPAIR_EXHAUSTED fork DIRECTLY through the
            # canonical writer (bumping forked + blocked on disk). Absorb that
            # disk-side fork so the loop's next persist does not clobber it +
            # does not double-count the failure.
            self._resync_forks_from_disk()
        elif outcome == "forked":
            # A genuine watcher fork with no repair hook: a terminal failure
            # (the pre-W03 behaviour), not an operator pause.
            self.run.counters.forked += 1
            self.run.counters.failed += 1
        else:
            # The lane closed clean. When the watcher resolved "closed" from a
            # persisted close-ready report (a sandboxed headless agent that could
            # not self-close), the wave is still IN_PROGRESS -- flip it to CLOSED
            # on the agent's behalf so the wave matches the tally. DL-5 already
            # ran above (a high-risk close was downgraded to "forked"), so only an
            # auto-closeable tier reaches here; a self-closed wave is a no-op.
            self._close_wave_on_disk(wave_id)
            if close_gate_evidence and self.ctx.state_path is not None:
                # W19: the deterministic gates passed -- append their
                # deterministic-pass evidence rows (bound to the wave) AFTER the
                # close flip commits, so a refused close never leaves a stray pass
                # row. The evidence append acquires the sibling evidence.jsonl
                # lock, distinct from the state lock the close flip held.
                from eawf.runtime.daemon.methods.state import _append_close_evidence

                _append_close_evidence(close_gate_evidence, state_path=Path(self.ctx.state_path))
            self.run.counters.closed += 1
        lane_spend = self.spend(self.ctx, wave_id)
        self.run.counters.spent_eu += lane_spend.eu
        self.run.counters.spent_usd += lane_spend.usd
        logger.info(
            f"_finish_lane wave={wave_id} watched={watched} outcome={outcome} "
            f"risk_tier={risk_tier.value} "
            f"fork_reason={fork_reason.value if fork_reason else None} "
            f"eu={lane_spend.eu} usd={lane_spend.usd}"
        )
        # A needs-user pause must reset the convergence streak too: report it as
        # a fork to the drain loop's clean-round accounting.
        return "forked" if fork_reason is not None else outcome

    def _repair_lane(self, wave_id: str, lane: FleetLane, risk_tier: RiskTier) -> FleetLane | None:
        """Re-dispatch a failing lane up the bounded grounded repair ladder -- W03.

        Invoked from :meth:`_finish_lane` when the watcher reported a genuine
        failing-check fork AND a :attr:`repair` hook is wired. The hook drives
        the bounded grounded repair ladder (:func:`repair_lane_or_fork`): a
        resolved repair yields a re-dispatched :class:`FleetLane` carrying the
        INCREMENTED dispatch attempt (the cockpit's ``repair n/<budget>``
        counter), and an exhausted ladder has already enqueued the
        ``REPAIR_EXHAUSTED`` fork so the lane stays forked.

        When no hook is wired (the synchronous in-process callers + the existing
        tests) this returns ``None`` so the lane keeps its pre-W03 terminal-fork
        behaviour.

        Args:
            wave_id: ``W<NN>`` wave whose failing lane to repair.
            lane: The forked lane (carrying its pre-repair attempt).
            risk_tier: The lane's resolved :class:`RiskTier`, carried onto the
                re-dispatched lane's badge.

        Returns:
            The re-dispatched :class:`FleetLane` on a resolved repair (its
            ``attempt`` advanced), or ``None`` when no hook is wired or the
            repair ladder was exhausted (a ``REPAIR_EXHAUSTED`` fork queued).
        """
        if self.repair is None:
            return None
        outcome = self.repair(self.ctx, lane)
        if not outcome.resolved or outcome.dispatch is None:
            logger.info(
                f"_repair_lane wave={wave_id} status=exhausted attempts={outcome.attempts_used}"
            )
            return None
        dispatch = outcome.dispatch
        logger.info(
            f"_repair_lane wave={wave_id} status=resolved attempt={dispatch.attempt} "
            f"attempts_used={outcome.attempts_used}"
        )
        return FleetLane(
            wave_id=wave_id,
            attempt=dispatch.attempt,
            session_id=dispatch.session_id,
            pgid=dispatch.pgid,
            dispatched_at=datetime.now(UTC),
        )

    def _finish_run(self, reason: FleetTerminalReason) -> None:
        """Stamp the terminal run-summary fields at the DONE transition -- DL-10.

        The single terminal-transition path every stop reason routes through:
        sets ``run_state=DONE``, the *reason*, and the FA7 run-summary
        derivations that the cockpit READS rather than recomputes -- the
        ``ended_at`` stamp, the ``elapsed_hours`` window from ``armed_at``, and
        the ``throughput`` (closed waves per hour). The throughput is computed
        DAEMON-side as ``counters.closed / elapsed_hours``; a degenerate
        (zero-hour) window yields ``0.0`` so the division never divides by zero.

        Args:
            reason: Why the run reached :data:`FleetRunState.DONE`.
        """
        self.run.run_state = FleetRunState.DONE
        self.run.terminal_reason = reason
        ended_at = datetime.now(UTC)
        self.run.ended_at = ended_at
        elapsed_hours = (ended_at - self.run.armed_at).total_seconds() / 3600.0
        elapsed_hours = max(elapsed_hours, 0.0)
        self.run.elapsed_hours = elapsed_hours
        closed = self.run.counters.closed
        self.run.throughput = closed / elapsed_hours if elapsed_hours > 0.0 else 0.0
        logger.info(
            f"_finish_run reason={reason.value} closed={self.run.counters.closed} "
            f"elapsed_hours={self.run.elapsed_hours} throughput={self.run.throughput}"
        )

    def _converged(self) -> bool:
        """Return whether the ``kclean`` convergence criterion is met.

        Only the ``kclean`` mode can converge early; ``drain`` always returns
        ``False`` (it stops solely on an empty frontier + empty lanes).
        """
        if self.run.convergence != "kclean":
            return False
        return self.run.counters.clean_rounds >= self.run.kclean_k

    def _budget_terminal(self) -> FleetRun:
        """End the run on a fired budget cap -- graceful-drain or hard-halt.

        The DL-4 budget HALT teeth. The claim gate has already stopped claiming
        new waves; this resolves the still-in-flight lanes per the run's
        :attr:`~eawf.kernel.state.models.FleetRun.hard_halt` toggle:

        - Graceful drain (the default, ``hard_halt`` False): watch every
          remaining in-flight lane to completion (no further claim), then end
          ``DONE`` + ``terminal_reason=budget``. The in-flight work finishes.
        - Hard halt (``hard_halt`` True): KILL every remaining in-flight lane
          via the DL-3 :func:`kill_lane` (each kill deregisters the lane +
          bumps the fork counter), then end ``DONE`` +
          ``terminal_reason=budget``. The in-flight work is reaped at the cap.

        Returns:
            The terminal :class:`FleetRun` snapshot (``DONE`` / ``budget``).
        """
        if self.run.hard_halt:
            # Persist the in-memory run first so the on-disk registry reflects
            # the lanes that already finished this round (kill_lane + the
            # re-read below both read through the canonical writer, so disk must
            # be current before the kills land).
            self._persist()
            # Kill every in-flight lane at the cap (DL-3). Snapshot the lanes
            # first because kill_lane deregisters each lane it reaps.
            for lane in list(self.run.lanes.values()):
                kill_lane(
                    self.ctx,
                    wave_id=lane.wave_id,
                    attempt=lane.attempt,
                    hard=True,
                )
                self.risk_tiers.pop(lane.wave_id, None)
            # kill_lane wrote each deregistered lane through the canonical
            # writer; re-read the run so the in-memory snapshot reflects the
            # reaped lanes before the terminal transition.
            refreshed = _require_run(self.ctx) if self.ctx.state_path is not None else None
            if refreshed is not None:
                self.run = refreshed
            else:
                self.run.lanes = {}
        else:
            # Graceful drain: finish every remaining in-flight lane to
            # completion without claiming any further wave (the budget cap has
            # already fired, so this drains unconditionally to empty).
            for wave_id in list(self.run.lanes):
                self._finish_lane(wave_id)
            self._persist()
        self._finish_run(FleetTerminalReason.BUDGET)
        self._persist()
        logger.info(
            f"_budget_terminal hard_halt={self.run.hard_halt} "
            f"spent_eu={self.run.counters.spent_eu} spent_usd={self.run.counters.spent_usd} "
            f"claimed={self.run.counters.claimed}"
        )
        return self.run

    def _cancelled(self) -> bool:
        """Return whether daemon shutdown has signalled this run to stop -- W01.

        The background drive thread checks this between rounds: a set cancel
        event (raised by :func:`shutdown_drive` on daemon shutdown) stops the
        loop claiming new waves so the thread can join cleanly. The in-flight
        lanes are left for a later reattach to recover (the run stays DRAINING
        on disk rather than transitioning DONE), so shutdown never reaps live
        work. ``False`` when no cancel event is wired (the synchronous in-process
        callers).
        """
        return self.cancel is not None and self.cancel.is_set()

    def _disk_run_state(self) -> FleetRunState | None:
        """Re-read the persisted run state from disk, or ``None`` when stateless -- W06.

        The loop runs on the W01 worker thread; a ``fleet.pause`` / ``fleet.halt``
        RPC mutates ``run_state`` on disk from the event-loop thread. This
        point-read (free read access) lets the loop observe that operator
        intervention each round so it can cooperate (stop claiming on a pause,
        drain to done on a halt) WITHOUT the RPC having to abort the run. A
        stateless context (no on-disk run) returns ``None`` so the in-memory loop
        runs uninterrupted.

        Returns:
            The persisted :class:`FleetRunState`, or ``None`` when no on-disk run
            is configured.
        """
        if self.ctx.state_path is None:
            return None
        run = load_state(Path(self.ctx.state_path)).fleet_run
        return run.run_state if run is not None else None

    def _drain_in_flight_no_claim(self) -> None:
        """Finish every in-flight lane WITHOUT claiming any further wave -- W06.

        The shared graceful-stop drain the pause-hold + halt paths use: watch
        each open lane to completion (freeing + deregistering its slot) so the
        operator's pause / halt lets the in-flight work finish, then leaves the
        frontier un-claimed. Mirrors the budget-graceful-drain body.
        """
        for wave_id in list(self.run.lanes):
            self._finish_lane(wave_id)

    def run_to_terminal(self) -> FleetRun:
        """Drive the loop until it reaches a terminal or held state.

        The round structure: fill every free lane from the frontier, drain the
        in-flight lanes, then test the stop conditions. The loop stops with
        ``DONE`` + ``terminal_reason=converged`` when the ``kclean`` criterion
        is met (before draining to empty), with ``DONE`` +
        ``terminal_reason=budget`` when a spend cap fires with frontier work
        still queued (DL-4 -- graceful-drain or hard-halt per the run toggle),
        and with ``DONE`` + ``terminal_reason=drained`` when the frontier AND
        every lane have emptied. Each transition re-persists the run.

        A set cancel event (daemon shutdown -- W01) stops the loop BEFORE the
        next round's claim so the run is left DRAINING on disk for a later
        reattach to recover, rather than transitioning DONE; the in-flight lanes
        are never reaped on shutdown.

        W06 loop cooperation: each round the loop re-reads the persisted
        ``run_state`` so a ``fleet.pause`` / ``fleet.halt`` RPC (mutating it from
        the event-loop thread) is observed WITHOUT aborting the run. A PAUSED run
        claims no further wave, lets the in-flight lanes finish, then HOLDS
        (blocking on a short re-check sleep) until a resume flips it back to
        DRAINING or a halt ends it -- so a resume continues the same run. A
        HALTED run drains the in-flight lanes then transitions to DONE so the
        cockpit run-summary card opens.

        Returns:
            The terminal (or held) :class:`FleetRun` snapshot.
        """
        while True:
            if self._cancelled():
                # Daemon shutdown: stop claiming, leave the run DRAINING on disk
                # for a reattach to recover the in-flight lanes.
                self._persist()
                logger.info("run_to_terminal cancelled run_state=draining")
                return self.run
            disk_state = self._disk_run_state()
            if disk_state is FleetRunState.HALTED:
                # Operator halt: block new claims, let the in-flight lanes finish,
                # then transition to DONE so the run-summary card opens.
                self.run.run_state = FleetRunState.HALTED
                self._drain_in_flight_no_claim()
                self._finish_run(FleetTerminalReason.DRAINED)
                self._persist()
                logger.info("run_to_terminal halted run_state=done")
                return self.run
            if disk_state is FleetRunState.PAUSED:
                # Operator pause: claim no further wave; let the in-flight lanes
                # finish, then HOLD (re-check each tick) until resume / halt. The
                # run stays PAUSED on disk for the operator to resume the SAME run.
                self.run.run_state = FleetRunState.PAUSED
                self._drain_in_flight_no_claim()
                self._persist()
                if self._cancelled():
                    return self.run
                time.sleep(_WATCH_POLL_SECONDS)
                continue
            # Not held (DRAINING, or stateless): a resume flipped the disk state
            # back to DRAINING, so absorb it into the in-memory run before the
            # next claim -- otherwise a lingering in-memory PAUSED would make
            # _persist re-write PAUSED and re-hold the run (a resume deadlock).
            self.run.run_state = FleetRunState.DRAINING
            # W04: recompute the ready frontier off live state BEFORE filling, so
            # a wave newly unblocked by a just-closed dep joins the frontier this
            # round rather than being stranded off the armed list.
            self._recompute_frontier()
            self._fill_lanes()
            self._persist()
            if not self.run.lanes:
                # Nothing in flight and the fill found no frontier wave: the
                # frontier has drained empty.
                self._finish_run(FleetTerminalReason.DRAINED)
                self._persist()
                return self.run
            budget_stopped = self._drain_lanes()
            if self._converged():
                self._finish_run(FleetTerminalReason.CONVERGED)
                self._persist()
                return self.run
            if budget_stopped:
                # A spend cap fired mid-drain with frontier work still queued:
                # resolve the still-in-flight lanes (drain or kill) and end on
                # the budget cap.
                return self._budget_terminal()
            self._persist()


def arm_drive(
    ctx: MethodContext,
    *,
    frontier: list[str],
    concurrency: int = 1,
    convergence: str = "drain",
    kclean_k: int = 2,
    eu_cap: float | None = None,
    usd_cap: float | None = None,
    waves_cap: int | None = None,
    hard_halt: bool = False,
    spawn: LaneSpawner | None = None,
    watch: LaneWatcher | None = None,
    spend: LaneSpendReader | None = None,
    fork_evidence: ForkEvidenceReader | None = None,
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
    cancel: threading.Event | None = None,
    classify: ErrorClassifier | None = None,
    runtime_preference: list[str] | None = None,
    max_total_attempts: int = DEFAULT_MAX_TOTAL_ATTEMPTS,
    repair: LaneRepairHook | None = None,
    recompute_frontier: bool = True,
) -> FleetRun:
    """Arm + run the fleet auto-drain loop over *frontier*.

    Validates the frontier is non-empty (an empty frontier refuses to arm),
    then transitions the run from :data:`FleetRunState.IDLE`:

    - When ``state.dispatch_paused`` is set, the run is persisted IDLE with the
      frontier staged but claims no wave -- the operator stop wins over the
      arm.
    - Otherwise the run transitions IDLE -> DRAINING and the loop fills
      ``min(concurrency, len(frontier))`` lanes, watches them, and advances the
      frontier until it empties (``terminal_reason=drained``), the ``kclean``
      criterion is met (``terminal_reason=converged``), or a spend cap fires
      (``terminal_reason=budget`` -- DL-4).

    The run is persisted through the daemon canonical state writer on arm and
    on every subsequent transition (the loop never writes ``state.json``
    directly).

    Args:
        ctx: Daemon method context.
        frontier: Ready ``W<NN>`` wave ids to drain, in claim order.
        concurrency: Maximum lanes held at once.
        convergence: ``drain`` or ``kclean``.
        kclean_k: K threshold for ``kclean``.
        eu_cap: Optional cumulative EU spend cap; ``None`` leaves the run
            uncapped. At the cap the loop stops claiming new waves (DL-4).
        usd_cap: Optional cumulative USD spend cap; ``None`` leaves the run
            uncapped.
        waves_cap: Optional claimed-wave count cap; ``None`` leaves the run
            uncapped.
        hard_halt: The arm-modal budget toggle. ``False`` (the default) lets
            the in-flight lanes drain at the cap; ``True`` KILLS them (DL-3).
        spawn: Optional :class:`LaneSpawner` override (tests inject a fake);
            defaults to the live claim + ``agent.dispatch`` spawner.
        watch: Optional :class:`LaneWatcher` override (tests inject a fake);
            defaults to the on-disk wave-status watcher.
        spend: Optional :class:`LaneSpendReader` override (tests inject a fake);
            defaults to the live runtime-delta reader so the budget cap tests
            against real EU / USD figures.
        fork_evidence: Optional :class:`ForkEvidenceReader` override (tests pin
            a specific ref); defaults to the synthetic per-wave fork URN so a
            DL-6 blocking fork always carries a non-empty evidence ref.
        block_authority: The jury's earned authority for this run -- gates
            whether a high / ui lane may auto-close or must fork. Defaults to
            :attr:`BlockAuthority.ADVISORY` (an uncalibrated jury), so a
            high / ui lane forks rather than silently auto-closing.
        cancel: Optional shutdown event the loop checks between rounds (W01).
            When set the loop stops claiming + returns the run still DRAINING
            for a later reattach to recover; ``None`` (the synchronous callers)
            runs uninterrupted.
        classify: Optional :class:`ErrorClassifier` enabling the bounded spawn
            ladder (W03). When set, a lane spawn that raises ``RuntimeSpawnError``
            routes through :func:`spawn_lane_or_fork` so a spawn error FORKS the
            lane rather than aborting the run; ``None`` keeps the direct-spawn
            path.
        runtime_preference: The ``Wave.runtime_preference`` runtime ladder the
            bounded spawn ladder's V5 switch walks (W03); empty when the wave
            pins one runtime.
        max_total_attempts: Total spawn ceiling for the bounded spawn ladder.
        repair: Optional :class:`LaneRepairHook` enabling the bounded grounded
            repair ladder (W03). When set, a failing-check fork re-dispatches up
            the ladder; ``None`` keeps the terminal-fork behaviour.
        recompute_frontier: When ``True`` (the default, W04) the loop recomputes
            the ready frontier off ``state.json`` each round so dep-unblocked
            waves join as their deps close; ``False`` freezes the frontier at the
            armed list (the pre-W04 behaviour).

    Returns:
        The :class:`FleetRun` snapshot after the loop returns -- ``DONE`` on a
        drained / converged / budget-capped run, or ``IDLE`` when dispatch was
        paused on arm.

    Raises:
        LifecycleError: When *frontier* is empty -- the loop refuses to arm a
            ``DRAINING``-with-zero-lanes run that can never make progress.
    """
    if not frontier:
        raise LifecycleError("cannot arm fleet drive: ready frontier is empty")
    now = datetime.now(UTC)
    run = FleetRun(
        run_state=FleetRunState.IDLE,
        concurrency=concurrency,
        frontier=list(frontier),
        lanes={},
        counters=FleetCounters(),
        convergence="kclean" if convergence == "kclean" else "drain",
        kclean_k=kclean_k,
        eu_cap=eu_cap,
        usd_cap=usd_cap,
        waves_cap=waves_cap,
        hard_halt=hard_halt,
        terminal_reason=None,
        armed_at=now,
    )
    if _dispatch_paused(ctx):
        # The operator stop wins: stage the frontier but claim nothing and stay
        # IDLE. A later resume re-arms the drive.
        _persist_fleet_run(ctx, run)
        logger.info(f"arm_drive paused frontier={len(frontier)} run_state=idle")
        return run
    run.run_state = FleetRunState.DRAINING
    _persist_fleet_run(ctx, run)
    loop = _Loop(
        ctx=ctx,
        run=run,
        spawn=spawn if spawn is not None else _default_spawner,
        watch=watch if watch is not None else _default_watcher,
        block_authority=block_authority,
        spend=spend if spend is not None else _default_lane_spend,
        fork_evidence=fork_evidence if fork_evidence is not None else _default_fork_evidence,
        cancel=cancel,
        classify=classify,
        runtime_preference=list(runtime_preference) if runtime_preference else [],
        max_total_attempts=max_total_attempts,
        repair=repair,
        recompute_frontier=recompute_frontier,
    )
    terminal = loop.run_to_terminal()
    logger.info(
        f"arm_drive done run_state={terminal.run_state.value} "
        f"reason={terminal.terminal_reason.value if terminal.terminal_reason else None} "
        f"rounds={terminal.counters.rounds}"
    )
    return terminal


def pause_all(ctx: MethodContext) -> FleetRun:
    """Pause a DRAINING fleet run: set PAUSED, claim no further waves.

    A pause-all on a ``DRAINING`` run sets
    :data:`FleetRunState.PAUSED` and leaves the in-flight lanes intact -- the
    loop claims zero further waves while paused. A resume returns the run to
    ``DRAINING`` and claiming restarts. The transition persists through the
    daemon canonical state writer.

    Args:
        ctx: Daemon method context.

    Returns:
        The updated :class:`FleetRun` snapshot.

    Raises:
        LifecycleError: When no fleet run is armed.
    """
    run = _require_run(ctx)
    run.run_state = FleetRunState.PAUSED
    _persist_fleet_run(ctx, run)
    logger.info(f"pause_all lanes={len(run.lanes)} frontier={len(run.frontier)}")
    return run


def resume(
    ctx: MethodContext,
    *,
    spawn: LaneSpawner | None = None,
    watch: LaneWatcher | None = None,
    spend: LaneSpendReader | None = None,
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
) -> FleetRun:
    """Resume a PAUSED fleet run: return to DRAINING and restart claiming.

    Flips a ``PAUSED`` run back to :data:`FleetRunState.DRAINING` and re-runs
    the loop over the remaining frontier + in-flight lanes. The transition +
    every subsequent round persists through the daemon canonical writer. The
    DL-4 spend caps carry through on the persisted run, so a resumed run still
    halts on a fired budget cap.

    Args:
        ctx: Daemon method context.
        spawn: Optional :class:`LaneSpawner` override.
        watch: Optional :class:`LaneWatcher` override.
        spend: Optional :class:`LaneSpendReader` override; defaults to the live
            runtime-delta reader.
        block_authority: The jury's earned authority for the resumed run --
            gates whether a high / ui lane may auto-close or must fork. Defaults
            to :attr:`BlockAuthority.ADVISORY`, so a high / ui lane forks rather
            than silently auto-closing.

    Returns:
        The :class:`FleetRun` snapshot after the resumed loop returns.

    Raises:
        LifecycleError: When no fleet run is armed.
    """
    run = _require_run(ctx)
    run.run_state = FleetRunState.DRAINING
    _persist_fleet_run(ctx, run)
    loop = _Loop(
        ctx=ctx,
        run=run,
        spawn=spawn if spawn is not None else _default_spawner,
        watch=watch if watch is not None else _default_watcher,
        block_authority=block_authority,
        spend=spend if spend is not None else _default_lane_spend,
    )
    terminal = loop.run_to_terminal()
    logger.info(f"resume run_state={terminal.run_state.value}")
    return terminal


def halt_all(ctx: MethodContext) -> FleetRun:
    """Halt a fleet run: set HALTED, block new claims, let in-flight lanes finish.

    A halt-all sets :data:`FleetRunState.HALTED` and blocks new claims while
    leaving the in-flight lanes to finish on their own -- distinct from a
    kill-all, which would reap the in-flight work. The transition persists
    through the daemon canonical state writer.

    Args:
        ctx: Daemon method context.

    Returns:
        The updated :class:`FleetRun` snapshot.

    Raises:
        LifecycleError: When no fleet run is armed.
    """
    run = _require_run(ctx)
    run.run_state = FleetRunState.HALTED
    _persist_fleet_run(ctx, run)
    logger.info(f"halt_all lanes={len(run.lanes)} frontier={len(run.frontier)}")
    return run


def resume_cooperative(ctx: MethodContext) -> FleetRun:
    """Flip a PAUSED run back to DRAINING so a HELD live loop continues -- W06.

    The cooperative resume the ``fleet.resume`` RPC takes when a background drive
    is already in flight (the W01 worker thread is HOLDING the run paused). The
    loop re-reads ``run_state`` each round, so flipping the persisted state back
    to :data:`FleetRunState.DRAINING` is all the held loop needs to continue the
    SAME run -- no second loop is started (which would race the held one). When
    NO drive is in flight (the held loop already returned, e.g. after a daemon
    restart) the operator-facing RPC falls back to :func:`resume`, which re-runs
    the loop inline over the remaining frontier.

    Args:
        ctx: Daemon method context.

    Returns:
        The :class:`FleetRun` snapshot after the resume.

    Raises:
        LifecycleError: When no fleet run is armed.
    """
    run = _require_run(ctx)
    run.run_state = FleetRunState.DRAINING
    _persist_fleet_run(ctx, run)
    logger.info(f"resume_cooperative lanes={len(run.lanes)} frontier={len(run.frontier)}")
    return run


def _require_run(ctx: MethodContext) -> FleetRun:
    """Return the armed :class:`FleetRun`, or raise when none is armed.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.

    Returns:
        The current ``State.fleet_run``.

    Raises:
        LifecycleError: When ``ctx.state_path`` is unset or no fleet run is
            armed.
    """
    if ctx.state_path is None:
        raise LifecycleError("no fleet run armed: state_path not configured")
    run = load_state(Path(ctx.state_path)).fleet_run
    if run is None:
        raise LifecycleError("no fleet run armed")
    return run


def resolve_lane(run: FleetRun | None, *, wave_id: str, attempt: int) -> FleetLane | None:
    """Resolve the live in-flight lane for ``(wave_id, attempt)`` from the registry.

    The named lookup the kill (DL-3) / reattach (DL-8) paths share: the
    ``(wave_id, attempt)`` pair is the lane registry key, so a re-dispatch
    of the same wave registers a distinct lane and only the matching attempt
    resolves. A lane is returned only when its ``attempt`` also matches, so a
    stale attempt number never resolves a fresher lane.

    Args:
        run: The armed :class:`FleetRun` (or ``None`` when no run is armed).
        wave_id: ``W<NN>`` wave whose lane to resolve.
        attempt: 1-based dispatch attempt -- the second half of the registry
            key.

    Returns:
        The matching :class:`FleetLane`, or ``None`` when no run is armed or
        no lane matches the ``(wave_id, attempt)`` pair.
    """
    if run is None:
        return None
    lane = run.lanes.get(wave_id)
    if lane is None or lane.attempt != attempt:
        return None
    return lane


@dataclass(frozen=True)
class LaneKillResult:
    """Outcome of :func:`kill_lane` -- whether a lane was signalled + reaped.

    Attributes:
        killed: ``True`` when a live killable lane resolved and its group was
            signalled (including the already-dead race, which still counts as
            reaped since the group is gone); ``False`` when no live lane
            resolved (the typed not-found path) so nothing was signalled.
        reason: A short not-found cause when *killed* is ``False`` (e.g.
            ``no-fleet-run`` / ``no-lane`` / ``unkillable-lane``); ``None``
            on a successful kill.
        cancel: The raw :class:`~eawf.runtime.runtimes.cancel.CancelResult`
            from the one-shot group signal when *killed* is ``True``; ``None``
            on the not-found path.
    """

    killed: bool
    reason: str | None
    cancel: CancelResult | None


def kill_lane(
    ctx: MethodContext,
    *,
    wave_id: str,
    attempt: int,
    hard: bool,
    cancel: Callable[..., CancelResult] | None = None,
) -> LaneKillResult:
    """Resolve the ``(wave_id, attempt)`` lane, signal its pgid, then deregister.

    The DL-3 real-kill path: read the W02 lane registry off ``State.fleet_run``
    via :func:`resolve_lane`, and -- when a live **killable** lane resolves --
    signal its ``pgid`` (SIGKILL when *hard*, SIGTERM otherwise) through the
    one-shot group primitive
    :func:`eawf.runtime.runtimes.cancel.cancel_process_group`, then transition
    the lane to a killed terminal that **deregisters** it (drop its
    ``(wave_id, attempt) -> pgid`` row from ``FleetRun.lanes`` + bump the fork
    counter) through the daemon canonical state writer. A group that is already
    dead (``ProcessLookupError``) still counts as reaped (the primitive reports
    ``delivered=False`` rather than raising), so the lane is deregistered and
    the kill is reported successful.

    When no live fleet lane resolves (no fleet run armed, or no lane for the
    pair) the kill FALLS BACK to the wave's single-wave dispatched session (W09):
    a wave dispatched via ``eawf dispatch wave`` (no fleet run) records its child
    pid on the matching :class:`~eawf.kernel.state.models.SessionAttempt`, so the
    kill resolves that ``subprocess_pid`` and signals its group -- a single-wave
    spawn is killable even without a fleet run. Only when NEITHER a live lane NOR
    a session pid resolves -- or a lane / session carrying no addressable pid --
    does the function return a typed not-found (``killed=False`` + ``reason``) and
    signal nothing: it never fakes a kill on an unaddressable target.

    Args:
        ctx: Daemon method context -- supplies ``state_path`` (the registry +
            the deregister write target).
        wave_id: ``W<NN>`` wave whose lane to kill.
        attempt: 1-based dispatch attempt -- the second half of the registry
            key.
        hard: When ``True`` send SIGKILL (the ``kill`` signal); otherwise send
            SIGTERM (the graceful ``halt`` signal).
        cancel: Injectable one-shot group-signal seam. ``None`` (the default)
            resolves the module-level
            :func:`eawf.runtime.runtimes.cancel.cancel_process_group` at call
            time so a test can patch the module symbol; tests may also pass a
            fake directly so no real signal is delivered.

    Returns:
        A :class:`LaneKillResult` -- ``killed=True`` + the cancel result on a
        signalled lane / session, or ``killed=False`` + a not-found reason
        otherwise.
    """
    signal_group = cancel if cancel is not None else cancel_process_group
    run = load_state(Path(ctx.state_path)).fleet_run if ctx.state_path is not None else None
    lane = resolve_lane(run, wave_id=wave_id, attempt=attempt)
    if lane is None:
        # No live fleet lane: fall back to the single-wave dispatched session's
        # recorded child pid (W09) so a non-fleet ``eawf dispatch wave`` spawn is
        # still killable.
        return _kill_session_pid(
            ctx, wave_id=wave_id, attempt=attempt, hard=hard, signal_group=signal_group
        )
    if not lane.killable or lane.pgid is None:
        logger.info(
            f"kill_lane wave={wave_id} attempt={attempt} killed=false reason=unkillable-lane"
        )
        return LaneKillResult(killed=False, reason="unkillable-lane", cancel=None)
    # Signal the group first (outside the state lock so the kill ladder never
    # signals while holding the state portalock), then deregister the lane.
    result = signal_group(lane.pgid, hard=hard)
    _deregister_lane(ctx, wave_id=wave_id, attempt=attempt)
    logger.info(
        f"kill_lane wave={wave_id} attempt={attempt} pgid={lane.pgid} "
        f"hard={hard} delivered={result.delivered} killed=true"
    )
    return LaneKillResult(killed=True, reason=None, cancel=result)


def _kill_session_pid(
    ctx: MethodContext,
    *,
    wave_id: str,
    attempt: int,
    hard: bool,
    signal_group: Callable[..., CancelResult],
) -> LaneKillResult:
    """Kill a single-wave dispatched session's spawned process group -- W09.

    The non-fleet kill fallback: a wave dispatched via ``eawf dispatch wave`` (no
    fleet run) records its spawned child pid on the matching
    :attr:`~eawf.kernel.state.models.SessionAttempt.subprocess_pid` rather than a
    :class:`FleetLane`. This resolves that pid for ``(wave_id, attempt)`` and
    signals its group (SIGKILL when *hard*, else SIGTERM), so the operator can
    halt a single dispatched session even with no armed fleet run. The child is
    its own group leader (the adapter spawns ``start_new_session=True``), so the
    pid IS the pgid.

    Returns a typed not-found when no state is configured (``no-fleet-run``, the
    no-run reason the pre-W09 path used), the wave / attempt has no recorded
    session (``no-session``), or the session recorded no pid (``unkillable-
    session``) -- it never fakes a kill on an unaddressable session.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        wave_id: ``W<NN>`` wave whose dispatched session to kill.
        attempt: 1-based dispatch attempt keying the session row.
        hard: SIGKILL when ``True``, else SIGTERM.
        signal_group: The resolved one-shot group-signal seam.

    Returns:
        A :class:`LaneKillResult` -- ``killed=True`` + the cancel result on a
        signalled session, or ``killed=False`` + a not-found reason otherwise.
    """
    if ctx.state_path is None:
        logger.info(f"kill_lane wave={wave_id} attempt={attempt} killed=false reason=no-fleet-run")
        return LaneKillResult(killed=False, reason="no-fleet-run", cancel=None)
    wave = load_state(Path(ctx.state_path)).waves.get(wave_id)
    session = wave.sessions.get(attempt) if wave is not None else None
    if session is None:
        logger.info(f"kill_lane wave={wave_id} attempt={attempt} killed=false reason=no-session")
        return LaneKillResult(killed=False, reason="no-session", cancel=None)
    if session.subprocess_pid is None:
        logger.info(
            f"kill_lane wave={wave_id} attempt={attempt} killed=false reason=unkillable-session"
        )
        return LaneKillResult(killed=False, reason="unkillable-session", cancel=None)
    result = signal_group(session.subprocess_pid, hard=hard)
    logger.info(
        f"kill_lane wave={wave_id} attempt={attempt} session_pid={session.subprocess_pid} "
        f"hard={hard} delivered={result.delivered} killed=true source=session"
    )
    return LaneKillResult(killed=True, reason=None, cancel=result)


def _deregister_lane(ctx: MethodContext, *, wave_id: str, attempt: int) -> None:
    """Drop the killed lane from ``FleetRun.lanes`` through the canonical writer.

    Transitions a signalled lane to its killed terminal: removes its
    ``(wave_id, attempt) -> pgid`` row from the registry (so the registry holds
    exactly the still-in-flight lanes -- a killed lane's pgid is no longer a
    live target) and bumps the fork counter, mirroring how
    :meth:`_Loop._drain_lanes` deregisters a forked lane. The mutation routes
    through the daemon canonical state writer (``portalock(state.json)`` +
    locked atomic write). A stateless context is a no-op.

    A concurrent advance may have already dropped the lane between the kill
    signal and this write; the deregister tolerates a missing lane (it is the
    desired terminal either way) and only re-counts a fork when the lane was
    still present.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        wave_id: ``W<NN>`` wave whose lane to deregister.
        attempt: 1-based dispatch attempt -- the registry key's second half.
    """
    if ctx.state_path is None:
        return
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        run = state.fleet_run
        if run is None:
            return
        lane = resolve_lane(run, wave_id=wave_id, attempt=attempt)
        if lane is None:
            return
        del run.lanes[wave_id]
        run.counters.forked += 1
        state.fleet_run = run
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(f"_deregister_lane wave={wave_id} attempt={attempt}")


def resolve_fork_in_queue(run: FleetRun | None, *, wave_id: str, attempt: int) -> FleetFork | None:
    """Resolve the queued :class:`FleetFork` for ``(wave_id, attempt)`` -- pure, DL-6.

    The named lookup :func:`resolve_fork` shares with its tests: the
    ``(wave_id, attempt)`` pair keys a paused fork on :attr:`FleetRun.forks`, so
    a re-dispatch of the same wave under a fresh attempt queues a distinct fork
    and only the matching attempt resolves. Returns the first matching fork (the
    queue holds at most one open fork per ``(wave_id, attempt)``), or ``None``
    when no run is armed or no fork matches.

    Args:
        run: The armed :class:`FleetRun` (or ``None`` when no run is armed).
        wave_id: ``W<NN>`` wave whose queued fork to resolve.
        attempt: 1-based dispatch attempt -- the second half of the fork key.

    Returns:
        The matching :class:`FleetFork`, or ``None`` when none matches.
    """
    if run is None:
        return None
    for fork in run.forks:
        if fork.wave_id == wave_id and fork.attempt == attempt:
            return fork
    return None


def _reset_wave_pending(state: State, wave_id: str) -> None:
    """Flip a claimed / in-progress wave back to PENDING through the legal edge.

    The skip / re-dispatch fork resolutions free the held wave back to its
    plannable PENDING status so the loop (re-dispatch) or a later operator
    decision (skip) can re-claim it. The flip is guarded by the wave status
    machine -- a wave already terminal (CLOSED / FAILED) has no legal edge to
    PENDING and raises rather than silently regressing a closed wave.

    Args:
        state: State to mutate in place.
        wave_id: ``W<NN>`` wave to reset.

    Raises:
        LifecycleError: When *wave_id* is unknown, or its current status has no
            legal edge to PENDING.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave: {wave_id!r}")
    validate_transition(
        WAVE_TRANSITIONS,
        wave.status,
        WaveStatus.PENDING,
        illegal_message=(
            f"wave {wave_id!r} is terminal (status={wave.status.value!r}); cannot reset to pending"
        ),
    )
    wave.status = WaveStatus.PENDING
    wave.claim_session_id = None
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)


class FleetForkResolveResult(BaseModel):
    """Outcome of :func:`resolve_fork` -- the run state after a fork resolution.

    Attributes:
        resolution: The :class:`FleetForkResolution` that was applied.
        run_state: The :class:`FleetRunState` after the resolution (``HALTED``
            on an abort, else the unchanged run state).
        forks_open: The count of forks still queued after the resolution.
    """

    model_config = ConfigDict(extra="forbid")
    resolution: FleetForkResolution
    run_state: FleetRunState
    forks_open: int


def resolve_fork(
    ctx: MethodContext,
    *,
    wave_id: str,
    attempt: int,
    resolution: FleetForkResolution,
) -> FleetForkResolveResult:
    """Resolve a paused :class:`FleetFork` via one of the four DL-6 paths.

    Reads the armed :class:`FleetRun` off ``state.json``, resolves the queued
    fork for ``(wave_id, attempt)`` via :func:`resolve_fork_in_queue`, applies
    the operator's *resolution*, and persists the mutated run through the daemon
    canonical state writer (this function never opens ``state.json`` directly).
    The four resolutions:

    - :attr:`FleetForkResolution.APPROVE_CLOSE` -- close the held wave
      (:func:`close_wave`), dequeue the fork, and bump ``forks_resolved``.
    - :attr:`FleetForkResolution.RE_DISPATCH` -- reset the wave to PENDING and
      re-queue it onto the run frontier so the loop re-claims it on a later
      round; dequeue the fork and bump ``forks_resolved``.
    - :attr:`FleetForkResolution.SKIP` -- reset the wave to PENDING (freeing the
      lane) WITHOUT re-queuing it, and dequeue the fork -- the wave is left for
      a later operator decision.
    - :attr:`FleetForkResolution.ABORT_RUN` -- abandon EVERY queued fork and
      transition the run to ``HALTED``.

    Args:
        ctx: Daemon method context -- supplies ``state_path`` (the fork queue +
            the resolution write target).
        wave_id: ``W<NN>`` wave whose queued fork to resolve.
        attempt: 1-based dispatch attempt -- the second half of the fork key.
        resolution: The operator's :class:`FleetForkResolution`.

    Returns:
        A :class:`FleetForkResolveResult` carrying the applied resolution, the
        run state after it, and the count of forks still queued.

    Raises:
        LifecycleError: When ``ctx.state_path`` is unset, no fleet run is armed,
            no fork matches ``(wave_id, attempt)``, or *resolution* is not a
            recognized :class:`FleetForkResolution`.
    """
    if ctx.state_path is None:
        raise LifecycleError("no fleet run armed: state_path not configured")
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        run = state.fleet_run
        if run is None:
            raise LifecycleError("no fleet run armed")
        fork = resolve_fork_in_queue(run, wave_id=wave_id, attempt=attempt)
        if fork is None:
            raise LifecycleError(f"no fork queued for wave: {wave_id!r} attempt={attempt}")
        if resolution is FleetForkResolution.APPROVE_CLOSE:
            close_wave(state, wave_id=wave_id, outcome="fork approve-close")
            run.forks.remove(fork)
            run.counters.forks_resolved += 1
        elif resolution is FleetForkResolution.RE_DISPATCH:
            _reset_wave_pending(state, wave_id)
            if wave_id not in run.frontier:
                run.frontier.append(wave_id)
            run.forks.remove(fork)
            run.counters.forks_resolved += 1
        elif resolution is FleetForkResolution.SKIP:
            _reset_wave_pending(state, wave_id)
            run.forks.remove(fork)
        elif resolution is FleetForkResolution.ABORT_RUN:
            run.forks.clear()
            run.run_state = FleetRunState.HALTED
        else:
            raise LifecycleError(f"unknown fork resolution: {resolution!r}")
        state.fleet_run = run
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    logger.info(
        f"resolve_fork wave={wave_id} attempt={attempt} resolution={resolution.value} "
        f"run_state={run.run_state.value} forks_open={len(run.forks)}"
    )
    return FleetForkResolveResult(
        resolution=resolution,
        run_state=run.run_state,
        forks_open=len(run.forks),
    )


#: Probe one lane's process group for liveness (DL-8 reattach). Given a
#: ``pgid``, returns ``True`` when the OS still owns the group, ``False`` when
#: it is gone -- the ``os.kill(pgid, 0)`` / ``os.getpgid(pgid)`` style check the
#: reattach sweep uses to tell a still-running lane from one whose child died
#: during the TUI / daemon blip. The default probes ``os.getpgid``; tests
#: inject a deterministic fake so no real process is consulted.
LivenessProbe = Callable[[int], bool]


def _default_liveness(pgid: int) -> bool:
    """Return whether the OS still owns the process group *pgid* -- the live probe.

    Probes the group **leader** via :func:`os.getpgid` (mirroring the cancel
    ladder's group-liveness poll): the call resolving means the group is still
    alive, a :class:`ProcessLookupError` means it is gone. A
    :class:`PermissionError` (the group exists but is owned by another user) is
    treated as alive -- the group is present, just not ours to address.

    Args:
        pgid: Process-group id to probe.

    Returns:
        ``True`` when the group leader is still resolvable, else ``False``.
    """
    try:
        os.getpgid(pgid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LaneReattachOutcome(StrEnum):
    """Per-lane resolution of one :func:`reattach` sweep -- the closed outcome set.

    A lane is never left dangling as falsely running: the sweep resolves each
    in-flight lane to exactly one of these terminal outcomes.

    - ``reattached`` -- the lane's ``pgid`` is still live, so the loop re-binds
      to it and the run continues driving it (no re-claim).
    - ``redispatched`` -- the lane's child died during the blip, so the lane was
      transitioned through the transient ``reattaching`` state and resolved by
      re-dispatching the wave (a fresh lane registers with a new ``pgid``).
    - ``failed`` -- the lane's child died during the blip and was resolved as a
      fork (the fork counter bumps) rather than re-dispatched -- e.g. its wave
      already reached a terminal status during the blip, so re-dispatch would
      re-claim a closed wave.
    """

    REATTACHED = "reattached"
    REDISPATCHED = "redispatched"
    FAILED = "failed"


class ReattachLaneResult(BaseModel):
    """Resolution of one lane in a :func:`reattach` sweep.

    Attributes:
        wave_id: ``W<NN>`` wave the lane was driving.
        attempt: Dispatch attempt of the pre-blip lane.
        outcome: The lane's :class:`LaneReattachOutcome`.
        pgid: The live pgid the lane re-bound to (``reattached``), the fresh
            pgid of the re-dispatched lane (``redispatched``), or the dead pgid
            that was reaped (``failed``); ``None`` when the lane carried no pgid.
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str
    attempt: int = Field(ge=1)
    outcome: LaneReattachOutcome
    pgid: int | None = None


class ReattachResult(BaseModel):
    """Result of :func:`reattach` -- the recovered run + per-lane resolutions.

    Attributes:
        run_state: The :class:`FleetRunState` of the recovered run after the
            sweep re-binds / resolves its lanes.
        reattached: Lanes whose pgid was still live and were re-bound.
        redispatched: Lanes whose child died and were re-dispatched.
        failed: Lanes whose child died and were resolved as a fork.
    """

    model_config = ConfigDict(extra="forbid")
    run_state: FleetRunState
    reattached: list[ReattachLaneResult] = Field(default_factory=list)
    redispatched: list[ReattachLaneResult] = Field(default_factory=list)
    failed: list[ReattachLaneResult] = Field(default_factory=list)


def _wave_terminal_in_state(ctx: MethodContext, wave_id: str) -> bool:
    """Return whether *wave_id* already reached a terminal status in ``state.json``.

    The reattach sweep consults this so a lane whose wave CLOSED (or
    failed / abandoned) during the blip is never re-dispatched -- re-claiming a
    closed wave would regress the run. A stateless context or a vanished wave
    reports ``True`` (no live wave to re-dispatch).

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        wave_id: ``W<NN>`` wave to inspect.

    Returns:
        ``True`` when the wave is closed / failed / abandoned / vanished, else
        ``False``.
    """
    if ctx.state_path is None:
        return True
    wave = load_state(Path(ctx.state_path)).waves.get(wave_id)
    if wave is None:
        return True
    return wave.status in {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}


def reattach(
    ctx: MethodContext,
    *,
    is_alive: LivenessProbe | None = None,
    spawn: LaneSpawner | None = None,
    watch: LaneWatcher | None = None,
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
    drive_after: bool = True,
) -> ReattachResult:
    """Recover the persisted FleetRun + re-bind its lanes after a TUI / daemon bounce.

    The DL-8 session-resume path the S12 scenario depends on. The FleetRun is
    daemon-owned, so it survives a TUI close and a daemon restart on the
    optional :attr:`~eawf.kernel.state.models.State.fleet_run` field. On
    reconnect this reloads the persisted run off ``state.json`` and walks every
    in-flight lane against the W02 pid registry:

    - A lane whose ``pgid`` is still **live** (per *is_alive*) is re-bound and
      the run continues driving it WITHOUT re-claiming -- the wave keeps its
      single dispatch attempt (C1).
    - A lane whose child **died** during the blip is transitioned through the
      transient ``reattaching`` state and resolved (never left dangling as
      falsely running, C2): re-dispatched (a fresh lane registers with a new
      pgid) when its wave is still in flight, or resolved as a fork (the fork
      counter bumps) when its wave already reached a terminal status during the
      blip -- so the sweep never re-claims a closed wave.

    A lane carrying no addressable ``pgid`` (a plan-only / unkillable lane) is
    re-bound as-is: there is no OS group to probe, so the loop simply continues
    driving it. After the sweep re-binds / resolves the registry, the recovered
    run is persisted through the daemon canonical state writer and -- when
    *drive_after* -- the loop resumes draining the remaining frontier + live
    lanes from its recovered state.

    Args:
        ctx: Daemon method context -- supplies ``state_path`` (the persisted run
            + the re-bind write target).
        is_alive: Injectable liveness probe (``pgid -> bool``). ``None`` (the
            default) probes :func:`os.getpgid`; tests inject a fake so no real
            process is consulted.
        spawn: Optional :class:`LaneSpawner` override used to re-dispatch a dead
            lane; defaults to the live claim + ``agent.dispatch`` spawner.
        watch: Optional :class:`LaneWatcher` override for the resumed drive.
        block_authority: The jury's earned authority for the resumed run.
        drive_after: When ``True`` (the default) the loop resumes draining after
            the re-bind sweep; tests pass ``False`` to inspect the re-bound run
            without driving it to terminal.

    Returns:
        A :class:`ReattachResult` carrying the recovered run state + the
        per-lane resolutions (re-bound / re-dispatched / failed).

    Raises:
        LifecycleError: When ``ctx.state_path`` is unset or no fleet run is
            armed to reattach to.
    """
    probe = is_alive if is_alive is not None else _default_liveness
    spawner = spawn if spawn is not None else _default_spawner
    run = _require_run(ctx)
    reattached: list[ReattachLaneResult] = []
    redispatched: list[ReattachLaneResult] = []
    failed: list[ReattachLaneResult] = []

    # Walk every in-flight lane against the pid registry. A live lane re-binds;
    # a dead lane is dropped from the registry (so it never stays falsely
    # running) and resolved -- re-dispatched when its wave is still in flight,
    # else forked.
    for wave_id in list(run.lanes):
        lane = run.lanes[wave_id]
        if lane.pgid is None or probe(lane.pgid):
            # Live (or plan-only / unaddressable): re-bind and keep driving.
            reattached.append(
                ReattachLaneResult(
                    wave_id=wave_id,
                    attempt=lane.attempt,
                    outcome=LaneReattachOutcome.REATTACHED,
                    pgid=lane.pgid,
                )
            )
            logger.info(
                f"reattach wave={wave_id} attempt={lane.attempt} pgid={lane.pgid} "
                f"state=reattaching outcome=reattached"
            )
            continue
        # Dead during the blip: drop the falsely-running lane from the registry.
        del run.lanes[wave_id]
        if _wave_terminal_in_state(ctx, wave_id):
            # The wave already terminated during the blip -- resolve as a fork
            # rather than re-claiming a closed wave.
            run.counters.forked += 1
            failed.append(
                ReattachLaneResult(
                    wave_id=wave_id,
                    attempt=lane.attempt,
                    outcome=LaneReattachOutcome.FAILED,
                    pgid=lane.pgid,
                )
            )
            logger.info(
                f"reattach wave={wave_id} attempt={lane.attempt} pgid={lane.pgid} "
                f"state=reattaching outcome=failed"
            )
            continue
        # The wave is still in flight: re-dispatch it as a fresh lane. A dead
        # lane re-dispatched back into flight is a resolved fork (the FA7
        # ``fork resolved`` tally).
        dispatch = _normalise_dispatch(spawner(ctx, wave_id))
        run.lanes[wave_id] = FleetLane(
            wave_id=wave_id,
            attempt=dispatch.attempt,
            session_id=dispatch.session_id,
            pgid=dispatch.pgid,
            dispatched_at=datetime.now(UTC),
        )
        run.counters.dispatched += 1
        run.counters.forks_resolved += 1
        redispatched.append(
            ReattachLaneResult(
                wave_id=wave_id,
                attempt=dispatch.attempt,
                outcome=LaneReattachOutcome.REDISPATCHED,
                pgid=dispatch.pgid,
            )
        )
        logger.info(
            f"reattach wave={wave_id} attempt={dispatch.attempt} pgid={dispatch.pgid} "
            f"state=reattaching outcome=redispatched"
        )

    run.run_state = FleetRunState.DRAINING
    _persist_fleet_run(ctx, run)
    logger.info(
        f"reattach run_state={run.run_state.value} reattached={len(reattached)} "
        f"redispatched={len(redispatched)} failed={len(failed)}"
    )
    if drive_after:
        loop = _Loop(
            ctx=ctx,
            run=run,
            spawn=spawner,
            watch=watch if watch is not None else _default_watcher,
            block_authority=block_authority,
        )
        run = loop.run_to_terminal()
    return ReattachResult(
        run_state=run.run_state,
        reattached=reattached,
        redispatched=redispatched,
        failed=failed,
    )


@register("fleet.reattach")
async def reattach_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Recover + re-bind the persisted FleetRun after a TUI / daemon bounce.

    The ``fleet.reattach`` RPC: reloads the persisted run off ``state.json``,
    re-binds every still-live lane against the W02 pid registry, resolves the
    lanes whose child died during the blip (re-dispatched or forked, never left
    falsely running), then resumes draining. Params are ignored -- the run is
    recovered entirely from persisted state, so the operator-facing surface
    needs only the bare RPC.

    Args:
        ctx: Daemon method context. Needs ``state_path`` to recover + re-bind +
            persist the run.
        params: Unused (the run is recovered from persisted state).

    Returns:
        Dict matching :class:`ReattachResult`.

    Raises:
        LifecycleError: When no fleet run is armed to reattach to.
    """
    result = reattach(ctx)
    logger.info(
        f"reattach_rpc run_state={result.run_state.value} "
        f"reattached={len(result.reattached)} redispatched={len(result.redispatched)} "
        f"failed={len(result.failed)}"
    )
    return result.model_dump(mode="json")


def drive_in_flight() -> bool:
    """Return whether a background drive is currently in flight -- W01.

    A second ``fleet.drive`` while one is DRAINING is rejected (the registry
    holds at most one active run), so the RPC consults this before starting a
    thread. ``True`` while the worker drive thread is alive; ``False`` once it
    has reached a terminal state and cleared the registry.

    Returns:
        ``True`` when a drive thread is running, else ``False``.
    """
    with _DRIVE_LOCK:
        active = _ACTIVE_DRIVE[0]
        return active is not None and active.thread.is_alive()


def shutdown_drive(*, timeout: float = 5.0) -> None:
    """Signal + join the active background drive thread on daemon shutdown -- W01.

    Daemon shutdown raises the active drive's cancel event so the loop stops
    claiming new waves between rounds, then joins the worker thread so the
    process does not exit while a drain is mid-write. The in-flight lanes are
    left for a later reattach to recover (the FleetRun is daemon-owned + durable
    on ``state.json``); shutdown never reaps them. A no-op when no drive is in
    flight, so an idle daemon shuts down immediately.

    Args:
        timeout: Seconds to wait for the drive thread to join before giving up
            (a still-blocked watcher cannot be force-killed from here).
    """
    with _DRIVE_LOCK:
        active = _ACTIVE_DRIVE[0]
    if active is None:
        return
    active.cancel.set()
    active.thread.join(timeout=timeout)
    logger.info(f"shutdown_drive handle={active.handle_id!r} joined={not active.thread.is_alive()}")


def _resolve_run_block_authority(ctx: MethodContext) -> BlockAuthority:
    """Resolve the run's jury block authority through the close-gate resolver -- W09.

    The fleet loop's DL-5 safety gate downgrades a high / ui lane's clean close to
    a fork UNLESS the cross-vendor jury has EARNED blocking authority. Before W09
    the loop always ran under the uncalibrated :attr:`BlockAuthority.ADVISORY`
    default, so a calibrated jury could never enable high-tier auto-close even
    after passing its trust floors. This resolves the run's authority through the
    SAME resolver the wave-close gate uses
    (:func:`eawf.runtime.daemon.methods.state._resolve_jury_block_authority` over
    the active :class:`~eawf.platform.profiles.models.VerifyBlock`), so a drive
    auto-closes a high-tier lane exactly when the close gate would -- one
    authority source, not a drive-local default that drifts from the gate.

    Default-advisory by construction: the validation substrate is empty today, so
    the resolver returns :attr:`BlockAuthority.ADVISORY` and high / ui lanes fork.
    A stateless context (no ``state_path``) likewise resolves advisory.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.

    Returns:
        The :class:`BlockAuthority` the jury has earned for this run.
    """
    if ctx.state_path is None:
        return BlockAuthority.ADVISORY
    from eawf.runtime.daemon.methods.state import _resolve_jury_block_authority
    from eawf.workflow.verify.readiness import load_active_verify_block

    state_path = Path(ctx.state_path)
    state = load_state(state_path)
    scope_id = state.current.project_code
    if scope_id is None:
        return BlockAuthority.ADVISORY
    verify_block = load_active_verify_block(scope_id, state, repo_root=state_path.parent.parent)
    authority = _resolve_jury_block_authority(
        state, state_path=state_path, verify_block=verify_block
    )
    logger.info(f"_resolve_run_block_authority authority={authority.value}")
    return authority


def start_background_drive(ctx: MethodContext, args: DriveParams) -> FleetDriveHandle:
    """Arm a drive + run its drain on a worker thread, returning a handle -- W01.

    The drain is SYNCHRONOUS (claim -> dispatch -> blocking watch -> advance),
    so running it inside the awaited ``fleet.drive`` RPC handler blocks the
    whole daemon event loop and times out the TUI arm. This arms the run on the
    calling (event-loop) thread -- a fast IDLE / DRAINING transition + one
    canonical-writer persist -- then hands the blocking drain to a worker thread
    and returns a :class:`FleetDriveHandle` immediately, so the RPC answers in
    well under a second while the drain continues in the background.

    The handle's run state reflects the arm: a run armed while
    ``state.dispatch_paused`` is set stays ``idle`` (the frontier is staged but
    nothing claimed -- no worker thread is started), and a normal arm is
    ``draining`` (the worker thread is claiming). A second call while a drive is
    already in flight raises rather than starting a concurrent run.

    The worker thread captures the daemon event loop so a bus publish from the
    drain marshals onto it through ``loop.call_soon_threadsafe`` (W01), persists
    every transition through the daemon canonical writer exactly as the
    synchronous form did, and clears the active-drive registry on terminal so a
    later drive can start.

    Args:
        ctx: Daemon method context -- supplies ``state_path`` + ``bus``.
        args: The validated drive params (frontier + caps + toggles).

    Returns:
        The :class:`FleetDriveHandle` for the started drive.

    Raises:
        LifecycleError: When a drive is already in flight (single-active-run
            guard), or the resolved frontier is empty.
    """
    if drive_in_flight():
        raise LifecycleError("a fleet drive is already in flight; halt or wait for it to drain")
    if not args.frontier:
        raise LifecycleError("cannot arm fleet drive: ready frontier is empty")
    handle_id = f"fleet-run-{uuid.uuid4().hex[:12]}"
    # W09: resolve the run's jury block authority through the SAME resolver the
    # close gate uses, so a high / ui lane auto-closes exactly when the close
    # gate would (default-advisory until the jury is calibrated).
    block_authority = _resolve_run_block_authority(ctx)
    # Paused arm: stage the frontier IDLE on the calling thread + return; no
    # worker thread is started (a paused state claims nothing).
    # The live drive enables the bounded spawn ladder (DL-11) + the grounded
    # repair ladder (DL-7) by injecting the production classifier + repair hook,
    # so a real autopilot run FORKS a failed spawn (rather than aborting the
    # whole run) and RE-DISPATCHES a failing check up the bounded repair ladder
    # (rather than counting it a terminal failure on the first refusal). Without
    # these the ladders are built-but-dormant on the live path (they only fire
    # when a test injects the hooks).
    repair_hook = _build_live_lane_repair_hook(ctx)
    if _dispatch_paused(ctx):
        run = arm_drive(
            ctx,
            frontier=args.frontier,
            concurrency=args.concurrency,
            convergence=args.convergence,
            kclean_k=args.kclean_k,
            eu_cap=args.eu_cap,
            usd_cap=args.usd_cap,
            waves_cap=args.waves_cap,
            hard_halt=args.hard_halt,
            block_authority=block_authority,
            classify=_live_lane_error_classifier,
            repair=repair_hook,
        )
        logger.info(
            f"start_background_drive paused handle={handle_id!r} run_state={run.run_state.value}"
        )
        return FleetDriveHandle(handle_id=handle_id, run_state=run.run_state, backgrounded=False)
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (a synchronous test caller): publishes run directly.
        loop = None
    cancel = threading.Event()

    def _drain() -> None:
        try:
            arm_drive(
                ctx,
                frontier=args.frontier,
                concurrency=args.concurrency,
                convergence=args.convergence,
                kclean_k=args.kclean_k,
                eu_cap=args.eu_cap,
                usd_cap=args.usd_cap,
                waves_cap=args.waves_cap,
                hard_halt=args.hard_halt,
                cancel=cancel,
                block_authority=block_authority,
                classify=_live_lane_error_classifier,
                repair=repair_hook,
            )
        except Exception:  # pragma: no cover - defensive: never leak from the thread
            logger.exception(f"start_background_drive drain failed handle={handle_id!r}")
        finally:
            with _DRIVE_LOCK:
                if _ACTIVE_DRIVE[0] is not None and _ACTIVE_DRIVE[0].handle_id == handle_id:
                    _ACTIVE_DRIVE[0] = None
            logger.info(f"start_background_drive drain done handle={handle_id!r}")

    thread = threading.Thread(target=_drain, name=f"fleet-drive-{handle_id}", daemon=True)
    with _DRIVE_LOCK:
        _LOOP_THREAD[0] = threading.current_thread()
        _ACTIVE_DRIVE[0] = _ActiveDrive(
            thread=thread, loop=loop, cancel=cancel, handle_id=handle_id
        )
    thread.start()
    logger.info(
        f"start_background_drive started handle={handle_id!r} frontier={len(args.frontier)}"
    )
    return FleetDriveHandle(
        handle_id=handle_id, run_state=FleetRunState.DRAINING, backgrounded=True
    )


@register("fleet.drive")
async def drive(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Arm the fleet auto-drain loop + background its drain, returning a handle -- W01.

    Validates params per :class:`DriveParams`, then starts the drain on a worker
    thread (:func:`start_background_drive`) so the RPC returns a
    :class:`FleetDriveHandle` in under a second while the drain continues in the
    background -- the synchronous drain no longer blocks the daemon event loop,
    so a concurrent ``daemon.ping`` answers mid-run. The loop claims, dispatches
    spawn=True, watches, closes-or-forks, and advances the frontier unattended
    until it empties / converges / hits a spend cap, honouring
    ``state.dispatch_paused`` (a paused state stays IDLE + claims nothing). Every
    transition is persisted only through the daemon canonical state writer, and
    the run transition is published on the bus (marshalled onto the loop thread
    from the drive thread). A second drive while one is DRAINING is rejected.

    Args:
        ctx: Daemon method context. Needs ``state_path`` (+ ``event_path`` for
            the live spawn path) to claim + dispatch + persist.
        params: JSON-RPC params per :class:`DriveParams` (including the optional
            DL-4 ``eu_cap`` / ``usd_cap`` / ``waves_cap`` spend caps + the
            ``hard_halt`` drain-vs-kill toggle).

    Returns:
        Dict matching :class:`FleetDriveHandle`.

    Raises:
        ValueError: When *params* fails :class:`DriveParams` validation (an
            empty frontier is rejected by the ``min_length=1`` constraint).
        LifecycleError: When a drive is already in flight, or the resolved
            frontier is empty (defence in depth beyond the param constraint).
    """
    args = DriveParams.model_validate(params)
    handle = start_background_drive(ctx, args)
    logger.info(
        f"drive handle={handle.handle_id!r} run_state={handle.run_state.value} "
        f"backgrounded={handle.backgrounded}"
    )
    return handle.model_dump(mode="json")


class FleetControlParams(BaseModel):
    """Params for the parameterless ``fleet.pause`` / ``fleet.halt`` / ``fleet.resume`` RPCs.

    The three control RPCs act on the single armed run, so they carry no fields;
    the model exists to enforce ``extra="forbid"`` so a caller that ships a stray
    key is rejected rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")


class FleetControlResult(BaseModel):
    """Result of a ``fleet.pause`` / ``fleet.halt`` / ``fleet.resume`` RPC -- W06.

    Attributes:
        run_state: The :class:`FleetRunState` after the control transition --
            ``paused`` after a pause, ``halted`` after a halt, ``draining`` after
            a cooperative resume.
    """

    model_config = ConfigDict(extra="forbid")
    run_state: FleetRunState


@register("fleet.pause")
async def pause_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Pause the armed fleet run: set PAUSED so the loop stops claiming -- W06.

    The ``fleet.pause`` RPC flips the persisted ``run_state`` to
    :data:`FleetRunState.PAUSED`; the background drive loop re-reads it each round
    and stops claiming new waves while the in-flight lanes finish, WITHOUT the RPC
    having to abort the run. A resume returns it to ``DRAINING`` and the SAME loop
    continues. The transition persists only through the daemon canonical writer.

    Args:
        ctx: Daemon method context. Needs ``state_path`` to read + persist the run.
        params: JSON-RPC params per :class:`FleetControlParams` (parameterless).

    Returns:
        Dict matching :class:`FleetControlResult` with ``run_state=paused``.

    Raises:
        ValueError: When *params* carries an unexpected key.
        LifecycleError: When no fleet run is armed.
    """
    FleetControlParams.model_validate(params)
    run = pause_all(ctx)
    logger.info(f"pause_rpc run_state={run.run_state.value}")
    return FleetControlResult(run_state=run.run_state).model_dump(mode="json")


@register("fleet.halt")
async def halt_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Halt the armed fleet run: set HALTED so the loop drains to the summary -- W06.

    The ``fleet.halt`` RPC flips the persisted ``run_state`` to
    :data:`FleetRunState.HALTED`; the background drive loop re-reads it each round,
    blocks new claims, lets the in-flight lanes finish, then transitions the run to
    DONE so the cockpit run-summary card opens. Distinct from a kill-all (the
    in-flight work is NOT reaped). The transition persists only through the daemon
    canonical writer.

    Args:
        ctx: Daemon method context. Needs ``state_path`` to read + persist the run.
        params: JSON-RPC params per :class:`FleetControlParams` (parameterless).

    Returns:
        Dict matching :class:`FleetControlResult` with ``run_state=halted``.

    Raises:
        ValueError: When *params* carries an unexpected key.
        LifecycleError: When no fleet run is armed.
    """
    FleetControlParams.model_validate(params)
    run = halt_all(ctx)
    logger.info(f"halt_rpc run_state={run.run_state.value}")
    return FleetControlResult(run_state=run.run_state).model_dump(mode="json")


@register("fleet.resume")
async def resume_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resume a PAUSED fleet run -- W06.

    The ``fleet.resume`` RPC continues a paused run by flipping the persisted
    ``run_state`` back to :data:`FleetRunState.DRAINING`
    (:func:`resume_cooperative`). When a background drive is in flight (the W01
    worker thread is HOLDING the run paused) the held loop re-reads that state on
    its next round and continues the SAME run -- no second loop is started (which
    would race the held one). When no drive is in flight (the held loop already
    returned, e.g. after a daemon restart) the run is left DRAINING for a
    ``fleet.reattach`` to recover + resume its lanes; the resume RPC never blocks
    the handler by re-running the loop inline.

    Args:
        ctx: Daemon method context. Needs ``state_path`` to read + persist the run.
        params: JSON-RPC params per :class:`FleetControlParams` (parameterless).

    Returns:
        Dict matching :class:`FleetControlResult` with ``run_state=draining``.

    Raises:
        ValueError: When *params* carries an unexpected key.
        LifecycleError: When no fleet run is armed.
    """
    FleetControlParams.model_validate(params)
    run = resume_cooperative(ctx)
    logger.info(f"resume_rpc run_state={run.run_state.value} in_flight={drive_in_flight()}")
    return FleetControlResult(run_state=run.run_state).model_dump(mode="json")


@register("fleet.resolve_fork")
async def resolve_fork_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a paused fleet fork via one of the four DL-6 resolutions.

    The ``fleet.resolve_fork`` RPC: validates params per
    :class:`ResolveForkParams`, applies the resolution through
    :func:`resolve_fork` (close the wave / re-queue it / skip it / abort the
    run), and returns the run state after the resolution. The mutation persists
    only through the daemon canonical state writer.

    Args:
        ctx: Daemon method context. Needs ``state_path`` to read the fork queue
            and persist the resolution.
        params: JSON-RPC params per :class:`ResolveForkParams`.

    Returns:
        Dict matching :class:`FleetForkResolveResult`.

    Raises:
        ValueError: When *params* fails :class:`ResolveForkParams` validation.
        LifecycleError: When no fleet run is armed or no fork matches the
            ``(wave_id, attempt)`` pair.
    """
    args = ResolveForkParams.model_validate(params)
    result = resolve_fork(
        ctx,
        wave_id=args.wave_id,
        attempt=args.attempt,
        resolution=args.resolution,
    )
    logger.info(
        f"resolve_fork_rpc wave={args.wave_id} attempt={args.attempt} "
        f"resolution={args.resolution.value} run_state={result.run_state.value}"
    )
    return result.model_dump(mode="json")
