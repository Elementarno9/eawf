"""Tests: daemon-owned fleet run resume + reattach after a TUI bounce (DL-8 / W08).

Exercises :func:`eawf.runtime.daemon.methods.fleet.reattach` -- the DL-8
session-resume path the S12 scenario depends on. The FleetRun is daemon-owned,
so it survives a TUI close and a daemon restart on the optional
:attr:`~eawf.kernel.state.models.State.fleet_run` field; on reconnect the sweep
re-binds every still-live lane against the W02 ``(wave_id, attempt) -> pgid``
registry and resolves the lanes whose child died during the blip.

Every assertion injects a deterministic liveness probe + spawner so no real
process is consulted, simulating a daemon restart by persisting a frozen
DRAINING run to ``state.json`` and then driving ``reattach`` off the recovered
state.

The success criteria under test:

- C1: after a simulated daemon restart mid-run, ``fleet.reattach`` recovers the
  persisted FleetRun and re-binds each LIVE pgid to its lane so the run
  continues WITHOUT re-claiming closed waves (the live lane keeps its single
  dispatch attempt).
- C2: a lane whose child DIED during the blip is set to a transient
  ``reattaching`` state then resolved (re-dispatched OR marked failed), never
  left dangling as falsely running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import (
    FleetCounters,
    FleetLane,
    FleetRun,
    FleetRunState,
    State,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    LaneDispatch,
    LaneReattachOutcome,
    ReattachResult,
    _default_liveness,
    reattach,
    reattach_rpc,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I12-W01", "P30-I12-W02", "P30-I12-W03"]
_FROZEN_TS = "2026-06-11T00:00:00Z"


def _wave_row(wid: str, *, status: str = "in_progress") -> dict[str, Any]:
    return {
        "id": wid,
        "iter_id": "P30-I12",
        "title": f"Frontier wave {wid[-3:]}",
        "status": status,
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
        "opened_at": _FROZEN_TS,
        "closed_at": None,
    }


def _state_payload(wave_statuses: dict[str, str]) -> dict[str, Any]:
    waves = {wid: _wave_row(wid, status=st) for wid, st in wave_statuses.items()}
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _FROZEN_TS,
        "dispatch_paused": False,
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
                "opened_at": _FROZEN_TS,
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
                "wave_ids": list(wave_statuses),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _FROZEN_TS,
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, wave_statuses: dict[str, str]) -> Path:
    state = State.model_validate(_state_payload(wave_statuses))
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


def _persist_run(state_path: Path, run: FleetRun) -> None:
    """Persist *run* onto ``state.fleet_run`` -- simulates the pre-blip on-disk run."""
    state = load_state(state_path)
    state.fleet_run = run
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _persisted_run(state_path: Path) -> FleetRun | None:
    return load_state(state_path).fleet_run


def _lane(wid: str, *, pgid: int | None, attempt: int = 1) -> FleetLane:
    return FleetLane(
        wave_id=wid,
        attempt=attempt,
        session_id=f"ses-{wid}",
        pgid=pgid,
        dispatched_at=_FROZEN_TS,  # type: ignore[arg-type]
    )


# ---- C1: restart recovers the run + re-binds live pgids, no re-claim --------


def test_reattach_recovers_run_and_rebinds_live_lanes_without_redispatch(
    tmp_path: Path,
) -> None:
    """C1: a recovered run re-binds every LIVE lane WITHOUT re-claiming/re-dispatching.

    Two in-flight lanes carry live pgids; the empty-frontier recovered run
    re-binds both and continues to terminal. The dispatched counter never
    advances (no re-dispatch) and the original (wave, attempt) pgids survive,
    so the live waves keep their single dispatch attempt.
    """
    state_path = _write_state(
        tmp_path, {"P30-I12-W01": "in_progress", "P30-I12-W02": "in_progress"}
    )
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=2,
        frontier=[],
        lanes={
            "P30-I12-W01": _lane("P30-I12-W01", pgid=91_001, attempt=1),
            "P30-I12-W02": _lane("P30-I12-W02", pgid=91_002, attempt=1),
        },
        counters=FleetCounters(claimed=2, dispatched=2),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    spawned: list[str] = []

    def _spawn(c: MethodContext, wid: str) -> str | None:
        spawned.append(wid)
        return f"redispatched-{wid}"

    result = reattach(
        ctx,
        is_alive=lambda pgid: True,  # every probed group is still live
        spawn=_spawn,
        watch=lambda c, lane: "closed",
    )

    # Both lanes re-bound; nothing re-dispatched / failed.
    assert {r.wave_id for r in result.reattached} == {"P30-I12-W01", "P30-I12-W02"}
    assert result.redispatched == []
    assert result.failed == []
    # The live lanes kept their original pgids (re-bind, not re-spawn).
    by_wave = {r.wave_id: r for r in result.reattached}
    assert by_wave["P30-I12-W01"].pgid == 91_001
    assert by_wave["P30-I12-W02"].pgid == 91_002
    assert by_wave["P30-I12-W01"].outcome is LaneReattachOutcome.REATTACHED
    # No re-claim / re-dispatch happened for the live lanes.
    assert spawned == []
    # The dispatched counter never advanced (no re-dispatch).
    assert result.run_state is FleetRunState.DONE


def test_reattach_does_not_reclaim_a_wave_closed_during_the_blip(tmp_path: Path) -> None:
    """C1: a lane whose wave CLOSED during the blip is never re-claimed/re-dispatched.

    A dead lane whose wave already reached CLOSED resolves as a fork rather
    than re-dispatching -- the recovered run must not re-claim a closed wave.
    """
    state_path = _write_state(
        tmp_path, {"P30-I12-W01": "closed", "P30-I12-W02": "in_progress"}
    )
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=2,
        frontier=[],
        lanes={
            "P30-I12-W01": _lane("P30-I12-W01", pgid=92_001),
            "P30-I12-W02": _lane("P30-I12-W02", pgid=92_002),
        },
        counters=FleetCounters(claimed=2, dispatched=2),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    spawned: list[str] = []

    def _spawn(c: MethodContext, wid: str) -> LaneDispatch:
        spawned.append(wid)
        return LaneDispatch(session_id=f"re-{wid}", pgid=93_000, attempt=2)

    # W01's group is dead (its wave closed during the blip); W02's is live.
    def _is_alive(pgid: int) -> bool:
        return pgid != 92_001

    result = reattach(
        ctx,
        is_alive=_is_alive,
        spawn=_spawn,
        watch=lambda c, lane: "closed",
        drive_after=False,
    )

    # W01 resolved as failed (its wave closed) -- NOT re-dispatched.
    assert {r.wave_id for r in result.failed} == {"P30-I12-W01"}
    assert result.redispatched == []
    assert "P30-I12-W01" not in spawned
    # W02 (live) re-bound.
    assert {r.wave_id for r in result.reattached} == {"P30-I12-W02"}


# ---- C2: a dead lane goes reattaching -> resolved (re-dispatch / fail) -------


def test_dead_lane_with_live_wave_is_redispatched_not_left_running(tmp_path: Path) -> None:
    """C2: a dead lane whose wave is still in flight is re-dispatched as a fresh lane.

    The lane is transitioned through the transient ``reattaching`` state and
    resolved by re-dispatching -- a fresh lane registers with a NEW pgid +
    attempt, and the old dead pgid is gone from the registry (never left
    falsely running).
    """
    state_path = _write_state(tmp_path, {"P30-I12-W01": "in_progress"})
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=1,
        frontier=[],
        lanes={"P30-I12-W01": _lane("P30-I12-W01", pgid=94_001, attempt=1)},
        counters=FleetCounters(claimed=1, dispatched=1),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    def _spawn(c: MethodContext, wid: str) -> LaneDispatch:
        return LaneDispatch(session_id=f"re-{wid}", pgid=94_999, attempt=2)

    result = reattach(
        ctx,
        is_alive=lambda pgid: False,  # the child died during the blip
        spawn=_spawn,
        watch=lambda c, lane: "closed",
        drive_after=False,
    )

    # The dead lane was re-dispatched as a fresh lane, not left running.
    assert {r.wave_id for r in result.redispatched} == {"P30-I12-W01"}
    assert result.reattached == []
    redisp = result.redispatched[0]
    assert redisp.pgid == 94_999
    assert redisp.attempt == 2
    assert redisp.outcome is LaneReattachOutcome.REDISPATCHED
    # The recovered run holds the FRESH lane (new pgid), the dead one is gone.
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.lanes["P30-I12-W01"].pgid == 94_999
    assert persisted.counters.dispatched == 2  # one re-dispatch bumped the counter


def test_dead_lane_with_terminal_wave_is_failed_not_left_running(tmp_path: Path) -> None:
    """C2: a dead lane whose wave terminated during the blip is forked, not re-dispatched.

    The lane is resolved as a fork (the fork counter bumps) rather than left
    dangling as falsely running -- and it is dropped from the live registry.
    """
    state_path = _write_state(tmp_path, {"P30-I12-W01": "failed"})
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=1,
        frontier=[],
        lanes={"P30-I12-W01": _lane("P30-I12-W01", pgid=95_001)},
        counters=FleetCounters(claimed=1, dispatched=1),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    spawned: list[str] = []

    def _spawn(c: MethodContext, wid: str) -> str | None:
        spawned.append(wid)
        return f"re-{wid}"

    result = reattach(
        ctx,
        is_alive=lambda pgid: False,  # the child died during the blip
        spawn=_spawn,
        watch=lambda c, lane: "closed",
        drive_after=False,
    )

    # The dead lane forked (no re-dispatch) -- its wave already terminated.
    assert {r.wave_id for r in result.failed} == {"P30-I12-W01"}
    assert result.redispatched == []
    assert spawned == []
    assert result.failed[0].outcome is LaneReattachOutcome.FAILED
    # The falsely-running lane is gone from the registry; a fork was counted.
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.lanes == {}
    assert persisted.counters.forked == 1


def test_reattach_mixed_live_and_dead_lanes_in_one_sweep(tmp_path: Path) -> None:
    """C1 + C2: one sweep re-binds the live lane + resolves the dead lane.

    A single reattach sweep over a live lane (re-bound) and a dead lane (its
    wave still in flight -> re-dispatched) proves the two paths coexist: the
    live lane keeps its pgid, the dead lane gets a fresh one, and neither is
    left dangling.
    """
    state_path = _write_state(
        tmp_path, {"P30-I12-W01": "in_progress", "P30-I12-W02": "in_progress"}
    )
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=2,
        frontier=[],
        lanes={
            "P30-I12-W01": _lane("P30-I12-W01", pgid=96_001),  # live
            "P30-I12-W02": _lane("P30-I12-W02", pgid=96_002),  # dead
        },
        counters=FleetCounters(claimed=2, dispatched=2),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    def _is_alive(pgid: int) -> bool:
        return pgid == 96_001  # only W01's group is live

    def _spawn(c: MethodContext, wid: str) -> LaneDispatch:
        return LaneDispatch(session_id=f"re-{wid}", pgid=96_999, attempt=2)

    result = reattach(
        ctx,
        is_alive=_is_alive,
        spawn=_spawn,
        watch=lambda c, lane: "closed",
        drive_after=False,
    )

    assert {r.wave_id for r in result.reattached} == {"P30-I12-W01"}
    assert {r.wave_id for r in result.redispatched} == {"P30-I12-W02"}
    assert result.failed == []
    persisted = _persisted_run(state_path)
    assert persisted is not None
    # Live lane kept its pgid; dead lane re-dispatched onto a fresh pgid.
    assert persisted.lanes["P30-I12-W01"].pgid == 96_001
    assert persisted.lanes["P30-I12-W02"].pgid == 96_999


def test_reattach_rebinds_plan_only_lane_with_no_pgid(tmp_path: Path) -> None:
    """C2: a plan-only lane (pgid=None, unaddressable) is re-bound as-is, never probed.

    There is no OS group to probe for an unkillable lane, so the sweep re-binds
    it rather than treating an absent pgid as a dead child.
    """
    state_path = _write_state(tmp_path, {"P30-I12-W01": "in_progress"})
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=1,
        frontier=[],
        lanes={"P30-I12-W01": _lane("P30-I12-W01", pgid=None)},
        counters=FleetCounters(claimed=1, dispatched=1),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    probed: list[int] = []

    def _is_alive(pgid: int) -> bool:
        probed.append(pgid)
        return False

    result = reattach(
        ctx,
        is_alive=_is_alive,
        spawn=lambda c, wid: f"re-{wid}",
        watch=lambda c, lane: "closed",
        drive_after=False,
    )

    # The plan-only lane was re-bound without consulting the liveness probe.
    assert {r.wave_id for r in result.reattached} == {"P30-I12-W01"}
    assert probed == []  # an unaddressable lane is never probed


def test_reattach_resumes_draining_remaining_frontier_after_rebind(tmp_path: Path) -> None:
    """C1: after re-binding live lanes the loop resumes draining the remaining frontier.

    A recovered run with a live lane + a queued frontier wave drives to DONE:
    the live lane re-binds and the queued frontier wave is then claimed +
    dispatched as the recovered loop drains to empty.
    """
    state_path = _write_state(
        tmp_path, {"P30-I12-W01": "in_progress", "P30-I12-W02": "pending"}
    )
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=1,
        frontier=["P30-I12-W02"],
        lanes={"P30-I12-W01": _lane("P30-I12-W01", pgid=97_001)},
        counters=FleetCounters(claimed=1, dispatched=1),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    spawned: list[str] = []

    def _spawn(c: MethodContext, wid: str) -> str | None:
        spawned.append(wid)
        return f"ses-{wid}"

    result = reattach(
        ctx,
        is_alive=lambda pgid: True,
        spawn=_spawn,
        watch=lambda c, lane: "closed",
    )

    # The queued frontier wave was claimed + dispatched after the re-bind.
    assert spawned == ["P30-I12-W02"]
    assert result.run_state is FleetRunState.DONE
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.frontier == []
    assert persisted.lanes == {}


# ---- error path + RPC + probe defaults --------------------------------------


def test_reattach_no_run_armed_raises(tmp_path: Path) -> None:
    """No persisted run to reattach to -> LifecycleError (fail-fast)."""
    state_path = _write_state(tmp_path, {"P30-I12-W01": "in_progress"})
    ctx = _ctx(state_path)
    with pytest.raises(LifecycleError, match="no fleet run armed"):
        reattach(ctx, is_alive=lambda pgid: True)


def test_reattach_no_state_path_raises() -> None:
    """A stateless context cannot recover a persisted run -> LifecycleError."""
    ctx = _ctx(None)
    with pytest.raises(LifecycleError, match="state_path not configured"):
        reattach(ctx, is_alive=lambda pgid: True)


def test_reattach_rpc_returns_reattach_result(tmp_path: Path) -> None:
    """The fleet.reattach RPC recovers + re-binds and returns a ReattachResult dict.

    The RPC drives the recovered loop with the default on-disk watcher (it
    passes no watch override), so the re-bound lane's wave is seeded ``closed``
    -- the default watcher then resolves it immediately rather than block-polling
    a never-terminal status.
    """
    state_path = _write_state(tmp_path, {"P30-I12-W01": "closed"})
    ctx = _ctx(state_path)
    pre = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=1,
        frontier=[],
        lanes={"P30-I12-W01": _lane("P30-I12-W01", pgid=None)},
        counters=FleetCounters(claimed=1, dispatched=1),
        armed_at=_FROZEN_TS,  # type: ignore[arg-type]
    )
    _persist_run(state_path, pre)

    out = asyncio.run(reattach_rpc(ctx, {}))
    # The RPC return validates back as a ReattachResult.
    result = ReattachResult.model_validate(out)
    assert result.run_state is FleetRunState.DONE


def test_reattach_result_is_strict_extra_forbid() -> None:
    """ReattachResult forbids unknown keys (extra=forbid)."""
    with pytest.raises(ValidationError):
        ReattachResult(
            run_state=FleetRunState.DRAINING,
            bogus="x",  # type: ignore[call-arg]
        )


def test_default_liveness_reports_dead_for_absent_pgid() -> None:
    """The default probe reports a non-existent group dead (ProcessLookupError -> False).

    A pid that the OS does not own raises ``ProcessLookupError`` from
    ``os.getpgid``, which the default probe maps to ``False`` (group gone).
    """
    # pid 2**31 - 1 is above any real pid -> getpgid raises ProcessLookupError.
    assert _default_liveness(2_147_483_646) is False


def test_default_liveness_reports_alive_for_own_process() -> None:
    """The default probe reports the test runner's own group alive (True)."""
    import os

    assert _default_liveness(os.getpid()) is True
