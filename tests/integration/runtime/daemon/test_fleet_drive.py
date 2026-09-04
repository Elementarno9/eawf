"""Tests: daemon-owned fleet auto-drain loop (``fleet.drive`` + FleetRun state).

Exercises :mod:`eawf.runtime.daemon.methods.fleet` -- the daemon-owned loop
that claims, dispatches, watches, and advances the ready wave frontier
unattended until it empties / converges. Every assertion drives the loop with
injectable fakes for spawn + watch so the loop runs deterministically without
real subprocesses, and reads the persisted
:class:`~eawf.kernel.state.models.FleetRun` back off the on-disk ``state.json``
to confirm the run is persisted ONLY through the daemon canonical writer.

The success criteria under test:

- C1: arming a drain over a 3-wave frontier with concurrency 2 transitions
  IDLE -> DRAINING and fills exactly ``min(concurrency, frontier)`` lanes; on a
  lane close the next frontier wave auto-claims into the freed lane.
- C2: a drive armed with ``dispatch_paused`` True claims no wave + stays IDLE;
  an empty frontier refuses to arm with a typed ``LifecycleError``.
- C3: ``FleetRun`` is strict (extra=forbid), persisted only through the daemon
  RPC; ``run_state`` is the closed StrEnum IDLE|DRAINING|PAUSED|HALTED|DONE.
- C4: pause-all on a DRAINING run -> PAUSED + zero further claims, in-flight
  lanes intact; resume -> DRAINING + claiming restarts. Halt-all -> HALTED,
  blocks new claims, lets in-flight lanes finish (distinct from kill-all).
- C5: ``kclean`` K=2 over a non-empty frontier stops after two consecutive
  clean rounds with DONE + ``terminal_reason=converged`` WITHOUT draining to
  empty; ``drain`` mode ends ``terminal_reason=drained`` only when the frontier
  empties.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import (
    FleetLane,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
    State,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    LaneDispatch,
    LaneOutcome,
    _default_spawner,
    _normalise_dispatch,
    arm_drive,
    drive,
    halt_all,
    pause_all,
    resume,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError, LifecycleGuardError

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I12-W01", "P30-I12-W02", "P30-I12-W03"]


def _state_payload(*, dispatch_paused: bool = False) -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid in _WAVE_IDS:
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I12",
            "title": f"Frontier wave {wid[-3:]}",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "agent_role": "executor",
            "effort_bucket": "M",
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "opened_at": "2026-06-11T00:00:00Z",
            "closed_at": None,
        }
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-11T00:00:00Z",
        "dispatch_paused": dispatch_paused,
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I12",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Binding pass",
                "status": "active",
                "iter_ids": ["P30-I12"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I12": {
                "id": "P30-I12",
                "phase_id": "P30",
                "title": "Fleet auto-drain loop",
                "status": "active",
                "wave_ids": list(_WAVE_IDS),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, dispatch_paused: bool = False) -> Path:
    """Serialise a valid :class:`State` with a 3-wave PENDING frontier."""
    state = State.model_validate(_state_payload(dispatch_paused=dispatch_paused))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path | None) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl" if state_path is not None else None
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=event_path,
        state_path=state_path,
    )


def _persisted_run(state_path: Path) -> FleetRun | None:
    return load_state(state_path).fleet_run


class _RecordingSpawner:
    """Deterministic :class:`LaneSpawner` fake that records claim order."""

    def __init__(self) -> None:
        self.spawned: list[str] = []

    def __call__(self, ctx: MethodContext, wave_id: str) -> str | None:
        self.spawned.append(wave_id)
        return f"ses-{wave_id}"


# ---- C1: arm a 3-wave/concurrency-2 drain; lanes fill + refill --------------


def test_arm_fills_min_concurrency_then_refills_freed_lane(tmp_path: Path) -> None:
    """C1: IDLE -> DRAINING; fill min(concurrency, frontier); refill on close.

    A 3-wave frontier at concurrency 2 fills exactly 2 lanes simultaneously,
    and as each lane closes the next frontier wave auto-claims into the freed
    slot, so all 3 waves dispatch and the run drains to DONE.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=spawner,
        watch=lambda c, lane: "closed",
    )

    # All three waves were claimed + dispatched, in frontier order.
    assert spawner.spawned == _WAVE_IDS
    # The run drained to DONE/drained (frontier emptied, lanes emptied).
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.claimed == 3
    assert run.counters.dispatched == 3
    assert run.counters.closed == 3
    assert run.counters.forked == 0
    # frontier + lanes both empty at terminal.
    assert run.frontier == []
    assert run.lanes == {}


def test_arm_holds_exactly_concurrency_lanes_simultaneously(tmp_path: Path) -> None:
    """C1: at most ``concurrency`` lanes are in flight at once.

    A watcher that returns ``running`` on the first sweep freezes the
    in-flight set so the test can read the persisted run and confirm exactly
    ``min(concurrency, frontier)`` == 2 lanes are open before any drain.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    sweeps: list[int] = []

    def _watch_once_running(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        # Record how many lanes exist when the first lane is watched, then
        # close so the loop can still terminate.
        run = _persisted_run(state_path)
        assert run is not None
        sweeps.append(len(run.lanes))
        return "closed"

    arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=_watch_once_running,
    )
    # The first drain sweep saw exactly 2 lanes (min(concurrency=2, frontier=3)).
    assert sweeps[0] == 2


# ---- C2: paused stays IDLE; empty frontier refuses to arm -------------------


def test_drive_paused_claims_nothing_stays_idle(tmp_path: Path) -> None:
    """C2: a drive armed while dispatch_paused stays IDLE + claims no wave."""
    state_path = _write_state(tmp_path, dispatch_paused=True)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=spawner,
        watch=lambda c, lane: "closed",
    )
    assert run.run_state is FleetRunState.IDLE
    assert spawner.spawned == []
    # The frontier is staged but no lane opened, persisted IDLE on disk.
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.run_state is FleetRunState.IDLE
    assert persisted.lanes == {}
    assert persisted.frontier == _WAVE_IDS


def test_empty_frontier_refuses_to_arm(tmp_path: Path) -> None:
    """C2: an empty ready frontier raises LifecycleError, not DRAINING-with-zero."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    with pytest.raises(LifecycleError, match="ready frontier is empty"):
        arm_drive(ctx, frontier=[], concurrency=2, spawn=lambda c, wid: None)
    # No run was persisted (the arm refused before any write).
    assert _persisted_run(state_path) is None


def test_drive_rpc_empty_frontier_rejected_by_param_constraint(tmp_path: Path) -> None:
    """C2: the fleet.drive RPC rejects an empty frontier at param validation."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    with pytest.raises(ValidationError):
        asyncio.run(drive(ctx, {"frontier": [], "concurrency": 2}))


def test_drive_rpc_rejects_concurrency_over_configured_cap_before_write(tmp_path: Path) -> None:
    """Fleet arm shares the claim cap and rejects before state/event/process mutation."""
    state_path = _write_state(tmp_path)
    (tmp_path / ".ea" / "config.yaml").write_text(
        "planning:\n  max_parallel_waves: 2\n",
        encoding="utf-8",
    )
    before = state_path.read_bytes()

    with pytest.raises(LifecycleGuardError, match="claim_parallel_limit_reached"):
        asyncio.run(drive(_ctx(state_path), {"frontier": list(_WAVE_IDS), "concurrency": 3}))

    assert state_path.read_bytes() == before
    assert not (tmp_path / ".ea" / "store" / "event.jsonl").exists()


def test_drive_wire_rejection_maps_guard_to_validation_failed(tmp_path: Path) -> None:
    """An over-cap fleet arm surfaces as ``-32002``, never an internal error."""
    from eawf.runtime.daemon.methods import VALIDATION_FAILED
    from eawf.runtime.daemon.server import _process_frame

    state_path = _write_state(tmp_path)
    (tmp_path / ".ea" / "config.yaml").write_text(
        "planning:\n  max_parallel_waves: 2\n",
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    request = {
        "jsonrpc": "2.0",
        "id": "fleet-cap-wire",
        "method": "fleet.drive",
        "params": {"frontier": list(_WAVE_IDS), "concurrency": 3},
    }

    response = asyncio.run(_process_frame(orjson.dumps(request), _ctx(state_path)))

    assert response["error"]["code"] == VALIDATION_FAILED == -32002
    assert "validation_failed: claim_parallel_limit_reached" in response["error"]["message"]
    assert state_path.read_bytes() == before
    assert not (tmp_path / ".ea" / "store" / "event.jsonl").exists()


# ---- C3: FleetRun strict + closed run_state enum ----------------------------


def test_fleet_run_is_strict_extra_forbid() -> None:
    """C3: FleetRun forbids unknown keys (extra=forbid)."""
    with pytest.raises(ValidationError):
        FleetRun(
            run_state=FleetRunState.IDLE,
            armed_at="2026-06-11T00:00:00Z",  # type: ignore[arg-type]
            bogus_field="x",  # type: ignore[call-arg]
        )


def test_run_state_is_closed_strenum() -> None:
    """C3: run_state is the closed StrEnum IDLE|DRAINING|PAUSED|HALTED|DONE."""
    assert {s.value for s in FleetRunState} == {
        "idle",
        "draining",
        "paused",
        "halted",
        "done",
    }
    with pytest.raises(ValueError, match="not a valid"):
        FleetRunState("running")


def test_fleet_run_persists_only_through_daemon_writer(tmp_path: Path) -> None:
    """C3: the loop never writes state.json directly -- the run round-trips on disk.

    After a drained run the persisted ``state.fleet_run`` carries the terminal
    snapshot, written through the daemon canonical writer (the same
    portalock + atomic-write path every mutator takes).
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=lambda c, lane: "closed",
    )
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.run_state is FleetRunState.DONE
    # Round-trips through State validation (strict) off disk.
    reloaded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert reloaded.fleet_run is not None
    assert reloaded.fleet_run.terminal_reason is FleetTerminalReason.DRAINED


# ---- C4: pause vs halt distinction ------------------------------------------


_FROZEN_TS = "2026-06-11T00:00:00Z"


def _seed_draining_run(state_path: Path, *, lanes: list[str], frontier: list[str]) -> None:
    """Persist a frozen DRAINING run with the given in-flight lanes + frontier."""
    state = load_state(state_path)
    lane_rows = {
        wid: FleetLane(wave_id=wid, session_id=f"ses-{wid}", dispatched_at=_FROZEN_TS)  # type: ignore[arg-type]
        for wid in lanes
    }
    state.fleet_run = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=2,
        frontier=list(frontier),
        lanes=lane_rows,
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def test_pause_all_sets_paused_keeps_lanes_no_new_claims(tmp_path: Path) -> None:
    """C4: pause-all on DRAINING -> PAUSED, in-flight lanes intact, zero new claims."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    _seed_draining_run(state_path, lanes=["P30-I12-W01"], frontier=["P30-I12-W02", "P30-I12-W03"])

    run = pause_all(ctx)
    assert run.run_state is FleetRunState.PAUSED
    # In-flight lane is left intact; the frontier (un-claimed) is untouched.
    assert set(run.lanes) == {"P30-I12-W01"}
    assert run.frontier == ["P30-I12-W02", "P30-I12-W03"]
    # No further claim happened (claimed counter still zero on the seeded run).
    assert run.counters.claimed == 0
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.run_state is FleetRunState.PAUSED


def test_resume_returns_to_draining_and_claims_restart(tmp_path: Path) -> None:
    """C4: resume on a PAUSED run -> DRAINING and claiming restarts over the frontier."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # Seed a PAUSED run with one in-flight lane + two queued frontier waves.
    _seed_draining_run(state_path, lanes=["P30-I12-W01"], frontier=["P30-I12-W02", "P30-I12-W03"])
    paused = pause_all(ctx)
    assert paused.run_state is FleetRunState.PAUSED

    spawned: list[str] = []

    def _spawn(c: MethodContext, wid: str) -> str | None:
        spawned.append(wid)
        return f"ses-{wid}"

    run = resume(ctx, spawn=_spawn, watch=lambda c, lane: "closed")
    # Resume restarted claiming: the queued frontier waves were dispatched.
    assert spawned == ["P30-I12-W02", "P30-I12-W03"]
    # The run drained to DONE after the frontier emptied.
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED


def test_halt_all_blocks_new_claims_lets_lanes_finish(tmp_path: Path) -> None:
    """C4: halt-all -> HALTED, blocks new claims, in-flight lanes are left to finish.

    Distinct from a kill-all: the in-flight lanes are NOT reaped -- the run
    keeps its lanes and the frontier is held un-claimed.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    _seed_draining_run(
        state_path,
        lanes=["P30-I12-W01", "P30-I12-W02"],
        frontier=["P30-I12-W03"],
    )
    run = halt_all(ctx)
    assert run.run_state is FleetRunState.HALTED
    # In-flight lanes survive the halt (not reaped, unlike a kill-all).
    assert set(run.lanes) == {"P30-I12-W01", "P30-I12-W02"}
    # New claims are blocked: the queued frontier wave stays un-claimed.
    assert run.frontier == ["P30-I12-W03"]
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.run_state is FleetRunState.HALTED


# ---- C5: kclean convergence vs drain-to-empty -------------------------------


def test_kclean_converges_after_two_clean_rounds_without_draining(tmp_path: Path) -> None:
    """C5: kclean K=2 stops after two clean rounds, DONE/converged, frontier not empty.

    A wide frontier (concurrency 1, three+ waves) drained one wave per round
    with every lane closing clean hits two consecutive clean rounds before the
    frontier empties, so the loop stops early with ``terminal_reason=converged``
    and a non-empty frontier remaining.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),  # 3 waves
        concurrency=1,
        convergence="kclean",
        kclean_k=2,
        spawn=spawner,
        watch=lambda c, lane: "closed",
    )
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.CONVERGED
    # Two clean rounds at concurrency 1 dispatched exactly 2 of the 3 waves --
    # the loop stopped WITHOUT draining the frontier to empty.
    assert spawner.spawned == _WAVE_IDS[:2]
    assert run.counters.clean_rounds == 2
    assert run.frontier == [_WAVE_IDS[2]]


def test_kclean_streak_resets_on_fork(tmp_path: Path) -> None:
    """C5: a fork resets the clean-round streak so convergence needs K *consecutive*.

    Round 1 forks (streak -> 0), rounds 2 + 3 close clean (streak -> 2) and the
    loop converges -- proving the streak counts consecutive clean rounds.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    outcomes = iter(["forked", "closed", "closed"])

    def _watch(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        return next(outcomes)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        convergence="kclean",
        kclean_k=2,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=_watch,
    )
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.CONVERGED
    assert run.counters.forked == 1
    assert run.counters.closed == 2
    assert run.counters.clean_rounds == 2


def test_drain_mode_ends_drained_only_when_frontier_empties(tmp_path: Path) -> None:
    """C5: drain mode never converges early -- it ends DONE/drained at empty frontier."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        convergence="drain",
        spawn=spawner,
        watch=lambda c, lane: "closed",
    )
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # drain mode dispatched ALL three waves (no early convergence stop).
    assert spawner.spawned == _WAVE_IDS
    assert run.frontier == []


# ---- W02 C1: live per-lane pgid registry -- record on dispatch, free on close


class _PgidSpawner:
    """Spawner fake yielding a real pgid per lane keyed by (wave_id, attempt)."""

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self._next_pgid = 90_000

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch:
        self.spawned.append(wave_id)
        self._next_pgid += 1
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=self._next_pgid, attempt=2)


def test_dispatch_records_real_pgid_keyed_by_wave_and_attempt(tmp_path: Path) -> None:
    """C1: a dispatched lane records the spawned child's pgid + (wave, attempt) key.

    A watcher that freezes the in-flight set on the first sweep lets the test
    read the live registry: each lane carries a real ``pgid``, the dispatch
    ``attempt``, and is ``killable`` -- the kill / reattach seam resolves to an
    OS process group rather than a bare label.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _PgidSpawner()
    seen: list[dict[str, object]] = []

    def _watch_record(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        seen.append(
            {
                "wave_id": lane.wave_id,
                "attempt": lane.attempt,
                "pgid": lane.pgid,
                "killable": lane.killable,
            }
        )
        return "closed"

    arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=spawner,
        watch=_watch_record,
    )
    # Every dispatched lane recorded a real pgid keyed by (wave_id, attempt=2).
    by_wave = {row["wave_id"]: row for row in seen}
    assert set(by_wave) == set(_WAVE_IDS)
    for wid in _WAVE_IDS:
        row = by_wave[wid]
        assert row["attempt"] == 2
        assert isinstance(row["pgid"], int) and row["pgid"] >= 90_000
        assert row["killable"] is True
    # Distinct lanes hold distinct pgids -- the registry is per-lane.
    pgids = [row["pgid"] for row in seen]
    assert len(set(pgids)) == len(pgids)


def test_close_deregisters_lane_so_registry_holds_only_in_flight(tmp_path: Path) -> None:
    """C1: closing a lane deregisters it -- the registry holds exactly in-flight lanes.

    Concurrency 1 over a 3-wave frontier means at most one lane is registered
    at a time; reading the persisted registry on each watch shows exactly the
    single in-flight lane, and the terminal run holds no lanes (all closed +
    deregistered).
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    registry_sizes: list[int] = []

    def _watch_size(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        run = _persisted_run(state_path)
        assert run is not None
        registry_sizes.append(len(run.lanes))
        return "closed"

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=_PgidSpawner(),
        watch=_watch_size,
    )
    # The registry never held more than the single in-flight lane.
    assert registry_sizes == [1, 1, 1]
    # Every lane closed + deregistered: the terminal registry is empty.
    assert run.lanes == {}
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.lanes == {}


# ---- W02 C2: a no-pid spawn -> pgid None + unkillable (no fabricated pid) ----


def test_no_pid_spawn_leaves_pgid_none_and_unkillable(tmp_path: Path) -> None:
    """C2: a spawn that returned no pid records pgid=None + marks the lane unkillable.

    The registry never holds a pid the OS does not own: a plan-only dispatch
    (no subprocess) leaves ``lane.pgid`` None and ``lane.killable`` False
    rather than fabricating a pid.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    seen: list[FleetLane] = []

    def _watch_capture(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        seen.append(lane)
        return "closed"

    arm_drive(
        ctx,
        frontier=list(_WAVE_IDS[:1]),
        concurrency=1,
        # A LaneDispatch with no pgid -- the spawn produced no subprocess.
        spawn=lambda c, wid: LaneDispatch(session_id=f"ses-{wid}", pgid=None),
        watch=_watch_capture,
    )
    assert len(seen) == 1
    lane = seen[0]
    assert lane.pgid is None
    assert lane.killable is False


def test_bare_str_spawner_normalises_to_unkillable_lane(tmp_path: Path) -> None:
    """C2: a bare-str spawner fake (W01 form) records a session id but no pgid.

    The W01 spawner fakes return ``str | None``; the loop normalises that to a
    :class:`LaneDispatch` with the string as the session id and ``pgid=None``,
    so a fake that recorded no real subprocess yields an unkillable lane.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    seen: list[FleetLane] = []

    def _watch_capture(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        seen.append(lane)
        return "closed"

    arm_drive(
        ctx,
        frontier=list(_WAVE_IDS[:1]),
        concurrency=1,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=_watch_capture,
    )
    assert len(seen) == 1
    assert seen[0].session_id == f"ses-{_WAVE_IDS[0]}"
    assert seen[0].pgid is None
    assert seen[0].killable is False


def test_normalise_dispatch_wraps_bare_and_passes_through_typed() -> None:
    """C2: _normalise_dispatch wraps a bare session id + passes a LaneDispatch through."""
    wrapped = _normalise_dispatch("ses-x")
    assert wrapped.session_id == "ses-x"
    assert wrapped.pgid is None
    assert wrapped.attempt == 1
    none_wrapped = _normalise_dispatch(None)
    assert none_wrapped.session_id is None
    assert none_wrapped.pgid is None
    typed = LaneDispatch(session_id="ses-y", pgid=4242, attempt=3)
    assert _normalise_dispatch(typed) is typed


def test_default_spawner_derives_pgid_from_plan_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1/C2: the live spawner records the plan child pid as the lane pgid.

    The spawned child is its own group leader, so its pgid equals the child
    pid the dispatch plan surfaces. A real pid is recorded; a plan-only
    dispatch (``pid==0``) records ``pgid=None`` so the lane is unkillable.
    """
    import eawf.runtime.daemon.methods.fleet as fleet_mod

    ctx = _ctx(None)
    # A live spawn surfaces a real child pid -> recorded as the pgid.
    monkeypatch.setattr(
        fleet_mod,
        "_run_dispatch_threaded",
        lambda c, wid, *, out_of_order: {
            "session_id": "ses-live",
            "pid": 54321,
            "attempt": 2,
        },
    )
    live = _default_spawner(ctx, "P30-I12-W02")
    assert live.session_id == "ses-live"
    assert live.pgid == 54321
    assert live.attempt == 2

    # A plan-only dispatch surfaces pid==0 -> no pgid (unkillable lane).
    monkeypatch.setattr(
        fleet_mod,
        "_run_dispatch_threaded",
        lambda c, wid, *, out_of_order: {
            "session_id": "ses-plan",
            "pid": 0,
            "attempt": 1,
        },
    )
    plan_only = _default_spawner(ctx, "P30-I12-W02")
    assert plan_only.session_id == "ses-plan"
    assert plan_only.pgid is None
    assert plan_only.attempt == 1
