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
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import RiskTier, WaveStatus
from eawf.kernel.state.models import (
    FleetCounters,
    FleetLane,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.runtime.daemon.methods import register
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.cancel import CancelResult, cancel_process_group
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.wave import claim_wave
from eawf.workflow.verify.oracle import classify_risk_tier, risk_tier_auto_closes

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
        authority.
    """
    if outcome != "closed":
        return outcome
    if risk_tier_auto_closes(risk_tier, block_authority=block_authority):
        return "closed"
    return "forked"


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
    """

    ctx: MethodContext
    run: FleetRun
    spawn: LaneSpawner
    watch: LaneWatcher
    block_authority: BlockAuthority = BlockAuthority.ADVISORY
    risk_tiers: dict[str, RiskTier] = field(default_factory=dict)

    def _persist(self) -> None:
        """Persist the current run snapshot through the daemon canonical writer."""
        _persist_fleet_run(self.ctx, self.run)

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
        """
        while len(self.run.lanes) < self.run.concurrency and self.run.frontier:
            wave_id = self.run.frontier.pop(0)
            dispatch = _normalise_dispatch(self.spawn(self.ctx, wave_id))
            self.run.lanes[wave_id] = FleetLane(
                wave_id=wave_id,
                attempt=dispatch.attempt,
                session_id=dispatch.session_id,
                pgid=dispatch.pgid,
                dispatched_at=datetime.now(UTC),
            )
            # Resolve + record the lane's RiskTier badge from the wave's gate
            # kinds so the cockpit can render it and the drain-time auto-close /
            # fork gate can consult it.
            risk_tier = _lane_risk_tier(self.ctx, wave_id)
            self.risk_tiers[wave_id] = risk_tier
            self.run.counters.claimed += 1
            self.run.counters.dispatched += 1
            logger.info(
                f"_fill_lanes wave={wave_id} attempt={dispatch.attempt} "
                f"session={dispatch.session_id!r} pgid={dispatch.pgid} "
                f"killable={dispatch.pgid is not None} risk_tier={risk_tier.value}"
            )

    def _drain_lanes(self) -> None:
        """Watch every in-flight lane to its terminal outcome, freeing each slot.

        Watches each open lane to completion (the watcher blocks until
        terminal); a ``closed`` outcome increments the closed counter and frees
        the slot, while a ``forked`` outcome increments the fork counter
        (resetting the convergence streak) and frees the slot. Every lane is
        freed by round end, so the next round's fill refills from the frontier.

        Freeing the slot **deregisters** the lane from the per-lane process
        registry (``del`` drops its ``(wave_id, attempt) -> pgid`` row), so the
        registry holds exactly the still-in-flight lanes -- a closed lane's
        pgid is no longer a live kill / reattach target.
        """
        had_fork = False
        for wave_id in list(self.run.lanes):
            watched = self.watch(self.ctx, self.run.lanes[wave_id])
            # Gate the watcher outcome through the lane's RiskTier: a high / ui
            # lane that "closed" under an unearned (advisory) jury is downgraded
            # to a fork so it never silently auto-closes -- the DL-5 safety
            # invariant.
            risk_tier = self.risk_tiers.pop(wave_id, RiskTier.MECH)
            outcome = gate_lane_outcome(
                watched, risk_tier, block_authority=self.block_authority
            )
            del self.run.lanes[wave_id]
            if outcome == "forked":
                self.run.counters.forked += 1
                had_fork = True
            else:
                self.run.counters.closed += 1
            logger.info(
                f"_drain_lanes wave={wave_id} watched={watched} outcome={outcome} "
                f"risk_tier={risk_tier.value}"
            )
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
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
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
        block_authority: The jury's earned authority for this run -- gates
            whether a high / ui lane may auto-close or must fork. Defaults to
            :attr:`BlockAuthority.ADVISORY` (an uncalibrated jury), so a
            high / ui lane forks rather than silently auto-closing.

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
        block_authority=block_authority,
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
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
) -> FleetRun:
    """Resume a PAUSED fleet run: return to DRAINING and restart claiming.

    Flips a ``PAUSED`` run back to :data:`FleetRunState.DRAINING` and re-runs
    the loop over the remaining frontier + in-flight lanes. The transition +
    every subsequent round persists through the daemon canonical writer.

    Args:
        ctx: Daemon method context.
        spawn: Optional :class:`LaneSpawner` override.
        watch: Optional :class:`LaneWatcher` override.
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

    When no live killable lane resolves -- no fleet run armed, no lane for the
    pair, or a lane carrying no addressable ``pgid`` (``killable=False``) --
    the function returns a typed not-found (``killed=False`` + ``reason``) and
    signals nothing: it never fakes a kill on an unaddressable lane.

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
        signalled lane, or ``killed=False`` + a not-found reason otherwise.
    """
    signal_group = cancel if cancel is not None else cancel_process_group
    run = load_state(Path(ctx.state_path)).fleet_run if ctx.state_path is not None else None
    if run is None:
        logger.info(f"kill_lane wave={wave_id} attempt={attempt} killed=false reason=no-fleet-run")
        return LaneKillResult(killed=False, reason="no-fleet-run", cancel=None)
    lane = resolve_lane(run, wave_id=wave_id, attempt=attempt)
    if lane is None:
        logger.info(f"kill_lane wave={wave_id} attempt={attempt} killed=false reason=no-lane")
        return LaneKillResult(killed=False, reason="no-lane", cancel=None)
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
        # The wave is still in flight: re-dispatch it as a fresh lane.
        dispatch = _normalise_dispatch(spawner(ctx, wave_id))
        run.lanes[wave_id] = FleetLane(
            wave_id=wave_id,
            attempt=dispatch.attempt,
            session_id=dispatch.session_id,
            pgid=dispatch.pgid,
            dispatched_at=datetime.now(UTC),
        )
        run.counters.dispatched += 1
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
