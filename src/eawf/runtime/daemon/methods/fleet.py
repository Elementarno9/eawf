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
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import (
    FleetCounters,
    FleetLane,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.daemon.methods import register
from eawf.runtime.lock import portalock
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.wave import claim_wave

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods import MethodContext

logger = logging.getLogger(__name__)


#: Terminal outcome of watching one in-flight lane to completion. The watcher
#: contract is **blocking**: it returns only once the lane has finished, so the
#: loop never busy-spins on a still-running lane.
#:
#: ``"closed"`` -- the lane's wave reached :data:`WaveStatus.CLOSED` (a clean
#: round contribution). ``"forked"`` -- the lane failed or was re-planned, which
#: resets the convergence streak.
LaneOutcome = str

#: Spawn a claimed wave's dispatch. Given the daemon context + wave id, returns
#: the registered dispatch session id (or ``None`` on a plan-only / stateless
#: dispatch). The daemon wires the live ``agent.dispatch`` spawn=True path;
#: tests inject a deterministic fake.
LaneSpawner = Callable[["MethodContext", str], "str | None"]

#: Watch one in-flight lane to its terminal :data:`LaneOutcome` (blocking).
#: Given the daemon context + the lane, returns ``"closed"`` or ``"forked"``.
#: The daemon wires a status-poll watcher; tests inject a deterministic fake.
LaneWatcher = Callable[["MethodContext", FleetLane], LaneOutcome]


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
    """

    model_config = ConfigDict(extra="forbid")
    frontier: list[str] = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1)
    convergence: str = "drain"
    kclean_k: int = Field(default=2, ge=1)


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


def _persist_fleet_run(ctx: MethodContext, fleet_run: FleetRun | None) -> None:
    """Write *fleet_run* onto ``State.fleet_run`` through the canonical writer.

    Routes the mutation through the daemon canonical state writer
    (``portalock(state.json)`` + locked atomic write, mirroring
    :func:`eawf.runtime.daemon.dispatch_runner._mark_wave_in_progress`): acquire
    the sibling lock, load the typed state, set ``fleet_run``, stamp
    ``updated_at``, then ``atomic_write_json_locked`` under the held lock. The
    loop never opens ``state.json`` directly.

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
    logger.info(f"_persist_fleet_run run_state={run_state!r}")


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


def _default_spawner(ctx: MethodContext, wave_id: str) -> str | None:
    """Claim then live-dispatch *wave_id*, returning the dispatch session id.

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

    Args:
        ctx: Daemon method context.
        wave_id: ``W<NN>`` wave to claim + dispatch.

    Returns:
        The registered dispatch session id, or ``None`` when the dispatch
        ran plan-only.
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
    return plan.get("session_id")


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


#: Seconds the default watcher sleeps between on-disk status polls.
_WATCH_POLL_SECONDS = 1.0


def _default_watcher(ctx: MethodContext, lane: FleetLane) -> LaneOutcome:
    """Block-poll one lane to its terminal outcome via the on-disk wave status.

    The daemon-wired default: poll the lane's wave status from ``state.json``
    until it reaches a terminal status, mapping ``CLOSED`` -> a clean close and
    ``FAILED`` / ``ABANDONED`` (or a vanished wave) -> a fork. The poll sleeps
    between reads so the loop never busy-spins. Tests inject a
    :class:`LaneWatcher` fake instead, so this blocking poll never runs under
    test.

    Args:
        ctx: Daemon method context -- supplies ``state_path``.
        lane: The in-flight lane to inspect.

    Returns:
        The lane's terminal :data:`LaneOutcome` (``"closed"`` or ``"forked"``).
    """
    if ctx.state_path is None:
        # Stateless context cannot poll a status -- treat the lane as forked so
        # the loop frees the slot rather than spinning.
        return "forked"
    state_path = Path(ctx.state_path)
    while True:
        wave = load_state(state_path).waves.get(lane.wave_id)
        if wave is None:
            return "forked"
        if wave.status is WaveStatus.CLOSED:
            return "closed"
        if wave.status in {WaveStatus.FAILED, WaveStatus.ABANDONED}:
            return "forked"
        time.sleep(_WATCH_POLL_SECONDS)


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
    """

    ctx: MethodContext
    run: FleetRun
    spawn: LaneSpawner
    watch: LaneWatcher

    def _persist(self) -> None:
        """Persist the current run snapshot through the daemon canonical writer."""
        _persist_fleet_run(self.ctx, self.run)

    def _fill_lanes(self) -> None:
        """Claim + dispatch frontier waves into every free lane.

        Pops from the head of the frontier until either the lane count hits
        the concurrency cap or the frontier empties. Each pop claims +
        dispatches the wave (via the injected spawner) and opens a lane;
        the claimed / dispatched counters advance per wave.
        """
        while len(self.run.lanes) < self.run.concurrency and self.run.frontier:
            wave_id = self.run.frontier.pop(0)
            session_id = self.spawn(self.ctx, wave_id)
            self.run.lanes[wave_id] = FleetLane(
                wave_id=wave_id,
                session_id=session_id,
                dispatched_at=datetime.now(UTC),
            )
            self.run.counters.claimed += 1
            self.run.counters.dispatched += 1
            logger.info(f"_fill_lanes wave={wave_id} session={session_id!r}")

    def _drain_lanes(self) -> None:
        """Watch every in-flight lane to its terminal outcome, freeing each slot.

        Watches each open lane to completion (the watcher blocks until
        terminal); a ``closed`` outcome increments the closed counter and frees
        the slot, while a ``forked`` outcome increments the fork counter
        (resetting the convergence streak) and frees the slot. Every lane is
        freed by round end, so the next round's fill refills from the frontier.
        """
        had_fork = False
        for wave_id in list(self.run.lanes):
            outcome = self.watch(self.ctx, self.run.lanes[wave_id])
            del self.run.lanes[wave_id]
            if outcome == "forked":
                self.run.counters.forked += 1
                had_fork = True
            else:
                self.run.counters.closed += 1
            logger.info(f"_drain_lanes wave={wave_id} outcome={outcome}")
        self.run.counters.rounds += 1
        if had_fork:
            self.run.counters.clean_rounds = 0
        else:
            self.run.counters.clean_rounds += 1

    def _converged(self) -> bool:
        """Return whether the ``kclean`` convergence criterion is met.

        Only the ``kclean`` mode can converge early; ``drain`` always returns
        ``False`` (it stops solely on an empty frontier + empty lanes).
        """
        if self.run.convergence != "kclean":
            return False
        return self.run.counters.clean_rounds >= self.run.kclean_k

    def run_to_terminal(self) -> FleetRun:
        """Drive the loop until it reaches a terminal or held state.

        The round structure: fill every free lane from the frontier, drain the
        in-flight lanes, then test the stop conditions. The loop stops with
        ``DONE`` + ``terminal_reason=converged`` when the ``kclean`` criterion
        is met (before draining to empty), and with ``DONE`` +
        ``terminal_reason=drained`` when the frontier AND every lane have
        emptied. Each transition re-persists the run.

        Returns:
            The terminal :class:`FleetRun` snapshot.
        """
        while True:
            self._fill_lanes()
            self._persist()
            if not self.run.lanes:
                # Nothing in flight and the fill found no frontier wave: the
                # frontier has drained empty.
                self.run.run_state = FleetRunState.DONE
                self.run.terminal_reason = FleetTerminalReason.DRAINED
                self._persist()
                return self.run
            self._drain_lanes()
            if self._converged():
                self.run.run_state = FleetRunState.DONE
                self.run.terminal_reason = FleetTerminalReason.CONVERGED
                self._persist()
                return self.run
            self._persist()


def arm_drive(
    ctx: MethodContext,
    *,
    frontier: list[str],
    concurrency: int = 1,
    convergence: str = "drain",
    kclean_k: int = 2,
    spawn: LaneSpawner | None = None,
    watch: LaneWatcher | None = None,
) -> FleetRun:
    """Arm + run the fleet auto-drain loop over *frontier*.

    Validates the frontier is non-empty (an empty frontier refuses to arm),
    then transitions the run from :data:`FleetRunState.IDLE`:

    - When ``state.dispatch_paused`` is set, the run is persisted IDLE with the
      frontier staged but claims no wave -- the operator stop wins over the
      arm.
    - Otherwise the run transitions IDLE -> DRAINING and the loop fills
      ``min(concurrency, len(frontier))`` lanes, watches them, and advances the
      frontier until it empties (``terminal_reason=drained``) or the ``kclean``
      criterion is met (``terminal_reason=converged``).

    The run is persisted through the daemon canonical state writer on arm and
    on every subsequent transition (the loop never writes ``state.json``
    directly).

    Args:
        ctx: Daemon method context.
        frontier: Ready ``W<NN>`` wave ids to drain, in claim order.
        concurrency: Maximum lanes held at once.
        convergence: ``drain`` or ``kclean``.
        kclean_k: K threshold for ``kclean``.
        spawn: Optional :class:`LaneSpawner` override (tests inject a fake);
            defaults to the live claim + ``agent.dispatch`` spawner.
        watch: Optional :class:`LaneWatcher` override (tests inject a fake);
            defaults to the on-disk wave-status watcher.

    Returns:
        The :class:`FleetRun` snapshot after the loop returns -- ``DONE`` on a
        drained / converged run, or ``IDLE`` when dispatch was paused on arm.

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
) -> FleetRun:
    """Resume a PAUSED fleet run: return to DRAINING and restart claiming.

    Flips a ``PAUSED`` run back to :data:`FleetRunState.DRAINING` and re-runs
    the loop over the remaining frontier + in-flight lanes. The transition +
    every subsequent round persists through the daemon canonical writer.

    Args:
        ctx: Daemon method context.
        spawn: Optional :class:`LaneSpawner` override.
        watch: Optional :class:`LaneWatcher` override.

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


@register("fleet.drive")
async def drive(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Arm + run the fleet auto-drain loop over the supplied ready frontier.

    Validates params per :class:`DriveParams`, arms the loop via
    :func:`arm_drive`, and returns the terminal :class:`FleetRun` snapshot. The
    loop claims, dispatches spawn=True, watches, closes-or-forks, and advances
    the frontier unattended until it empties / converges, honouring
    ``state.dispatch_paused`` (a paused state stays IDLE + claims nothing). The
    run is persisted only through the daemon canonical state writer.

    Args:
        ctx: Daemon method context. Needs ``state_path`` (+ ``event_path`` for
            the live spawn path) to claim + dispatch + persist.
        params: JSON-RPC params per :class:`DriveParams`.

    Returns:
        Dict matching :class:`DriveResult`.

    Raises:
        ValueError: When *params* fails :class:`DriveParams` validation (an
            empty frontier is rejected by the ``min_length=1`` constraint).
        LifecycleError: When the resolved frontier is empty (defence in depth
            beyond the param constraint).
    """
    args = DriveParams.model_validate(params)
    run = arm_drive(
        ctx,
        frontier=args.frontier,
        concurrency=args.concurrency,
        convergence=args.convergence,
        kclean_k=args.kclean_k,
    )
    result = DriveResult(
        run_state=run.run_state,
        terminal_reason=run.terminal_reason,
        counters=run.counters,
    )
    logger.info(
        f"drive run_state={run.run_state.value} "
        f"reason={run.terminal_reason.value if run.terminal_reason else None}"
    )
    return result.model_dump(mode="json")
