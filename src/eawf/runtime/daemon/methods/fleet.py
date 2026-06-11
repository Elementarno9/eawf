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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.schema import EuBasis
from eawf.kernel.state.enums import RiskTier, WaveStatus
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
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.observability.telemetry.join import DEFAULT_EU_MINUTES
from eawf.runtime.daemon.methods import register
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.cancel import CancelResult, cancel_process_group
from eawf.workflow.dispatch.retry import (
    RepairExhaustedError,
    RepairSpawnFn,
    RepairVerifier,
    repair_until_resolved,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.spec import WAVE_TRANSITIONS, validate_transition
from eawf.workflow.lifecycle.wave import claim_wave, close_wave, compute_runtime_delta
from eawf.workflow.verify.oracle import classify_risk_tier, risk_tier_auto_closes

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec
    from eawf.runtime.daemon.methods import MethodContext
    from eawf.runtime.runtimes.adapter import SpawnResult

logger = logging.getLogger(__name__)


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


def _default_fork_evidence(
    ctx: MethodContext, wave_id: str, reason: FleetForkReason
) -> str | None:
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
        fork = repair_exhausted_fork(
            exc, wave_id=wave_id, attempt=attempt, risk_tier=risk_tier
        )
        _enqueue_fork(ctx, fork)
        logger.warning(
            f"repair_lane_or_fork wave={wave_id} attempt={attempt} "
            f"criterion={criterion.id!r} status=escalated-to-fork"
        )
        raise


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
    """

    ctx: MethodContext
    run: FleetRun
    spawn: LaneSpawner
    watch: LaneWatcher
    block_authority: BlockAuthority = BlockAuthority.ADVISORY
    risk_tiers: dict[str, RiskTier] = field(default_factory=dict)
    spend: LaneSpendReader = _default_lane_spend
    fork_evidence: ForkEvidenceReader = _default_fork_evidence

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
        del self.run.lanes[wave_id]
        fork_reason = classify_fork_reason(
            watched, risk_tier, block_authority=self.block_authority
        )
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
        elif outcome == "forked":
            # A genuine watcher fork: a terminal failure, not an operator pause.
            self.run.counters.forked += 1
            self.run.counters.failed += 1
        else:
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

        Returns:
            The terminal :class:`FleetRun` snapshot.
        """
        while True:
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


def resolve_fork_in_queue(
    run: FleetRun | None, *, wave_id: str, attempt: int
) -> FleetFork | None:
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
            f"wave {wave_id!r} is terminal (status={wave.status.value!r}); "
            f"cannot reset to pending"
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


@register("fleet.drive")
async def drive(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Arm + run the fleet auto-drain loop over the supplied ready frontier.

    Validates params per :class:`DriveParams`, arms the loop via
    :func:`arm_drive`, and returns the terminal :class:`FleetRun` snapshot. The
    loop claims, dispatches spawn=True, watches, closes-or-forks, and advances
    the frontier unattended until it empties / converges / hits a spend cap,
    honouring ``state.dispatch_paused`` (a paused state stays IDLE + claims
    nothing). The run is persisted only through the daemon canonical state
    writer.

    Args:
        ctx: Daemon method context. Needs ``state_path`` (+ ``event_path`` for
            the live spawn path) to claim + dispatch + persist.
        params: JSON-RPC params per :class:`DriveParams` (including the optional
            DL-4 ``eu_cap`` / ``usd_cap`` / ``waves_cap`` spend caps + the
            ``hard_halt`` drain-vs-kill toggle).

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
        eu_cap=args.eu_cap,
        usd_cap=args.usd_cap,
        waves_cap=args.waves_cap,
        hard_halt=args.hard_halt,
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
