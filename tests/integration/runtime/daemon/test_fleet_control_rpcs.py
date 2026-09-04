"""Tests: fleet pause / halt / resume RPCs + loop cooperation.

``pause_all`` / ``halt_all`` / ``resume`` existed UNREGISTERED with zero callers,
and the drive loop never re-read the run state, so a cockpit pause aborted the
whole run. These assertions confirm the W06 wiring:

- C1: ``fleet.pause`` / ``fleet.halt`` / ``fleet.resume`` are registered RPCs.
- C2: the loop re-reads run_state each round so a pause stops new claims while
  in-flight lanes finish, a resume continues the SAME run, and a halt drains the
  in-flight lanes to DONE (the summary card).

The loop cooperation is exercised through ``arm_drive`` on a worker thread with
a gated watcher freezing a lane in flight, while the control transition is
applied from the test thread -- mirroring the daemon (loop on the W01 thread,
RPC on the event-loop thread).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import FleetLane, FleetRunState, FleetTerminalReason, State
from eawf.runtime.daemon.methods import MethodContext, registered_methods
from eawf.runtime.daemon.methods.fleet import (
    arm_drive,
    halt_all,
    pause_all,
    resume_cooperative,
)
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I17-W01", "P30-I17-W02", "P30-I17-W03"]


def _state_payload() -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid in _WAVE_IDS:
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I17",
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
            "iter_id": "P30-I17",
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
                "iter_ids": ["P30-I17"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I17": {
                "id": "P30-I17",
                "phase_id": "P30",
                "title": "Autopilot full-wire",
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


def _write_state(tmp_path: Path) -> Path:
    state = State.model_validate(_state_payload())
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def _persisted(state_path: Path) -> Any:
    return load_state(state_path).fleet_run


# ---- C1: the three control RPCs are registered -------------------------------


def test_fleet_control_rpcs_registered() -> None:
    """C1: fleet.pause / fleet.halt / fleet.resume are registered RPCs."""
    methods = registered_methods()
    assert "fleet.pause" in methods
    assert "fleet.halt" in methods
    assert "fleet.resume" in methods


# ---- C2: a pause stops new claims while in-flight lanes finish, resume continues


def test_pause_stops_claims_resume_continues_same_run(tmp_path: Path) -> None:
    """C2: a pause holds the running loop; resume continues the SAME run.

    A concurrency-1 drive over a 3-wave frontier runs on a worker thread with a
    gated watcher. After the first lane is in flight the test pauses the run: the
    loop finishes the in-flight lane, claims NO further wave, and holds PAUSED.
    A resume then continues the same loop, which drains the remaining waves to
    DONE -- the pause never aborted the run.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawned: list[str] = []
    spawn_lock = threading.Lock()
    first_in_flight = threading.Event()
    release = threading.Event()

    def _spawn(c: MethodContext, wave_id: str) -> str:
        with spawn_lock:
            spawned.append(wave_id)
        return f"ses-{wave_id}"

    def _watch(c: MethodContext, lane: FleetLane) -> str:
        # Freeze the first lane in flight so the test can pause mid-run; later
        # lanes close immediately once released.
        if lane.wave_id == _WAVE_IDS[0]:
            first_in_flight.set()
            release.wait(timeout=5.0)
        return "closed"

    result: dict[str, Any] = {}

    def _drive() -> None:
        result["run"] = arm_drive(
            ctx,
            frontier=list(_WAVE_IDS),
            concurrency=1,
            spawn=_spawn,
            watch=_watch,
        )

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    try:
        assert first_in_flight.wait(timeout=5.0)
        # Pause while the first lane is in flight.
        pause_all(ctx)
        # Release the gated lane: the loop finishes it, then HOLDS paused (no
        # further claim) because the disk state reads PAUSED.
        release.set()
        # Give the loop time to finish the in-flight lane + observe the pause.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            run = _persisted(state_path)
            if run is not None and run.run_state is FleetRunState.PAUSED and not run.lanes:
                break
            time.sleep(0.02)
        held = _persisted(state_path)
        assert held is not None
        assert held.run_state is FleetRunState.PAUSED
        # The pause stopped claiming: only the first wave was ever claimed while
        # the run held paused (waves 2 + 3 not yet claimed).
        with spawn_lock:
            assert spawned == [_WAVE_IDS[0]]
        # Resume: the held loop continues the SAME run and drains the rest.
        resume_cooperative(ctx)
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    finally:
        release.set()
    run = result.get("run")
    assert run is not None
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # All three waves were eventually claimed + closed (resume continued).
    assert sorted(spawned) == sorted(_WAVE_IDS)
    assert run.counters.closed == 3


# ---- C2: a halt drains the in-flight lanes to DONE (the summary card) --------


def test_halt_drains_in_flight_to_done(tmp_path: Path) -> None:
    """C2: a halt blocks new claims, lets in-flight lanes finish, ends DONE.

    After the first lane is in flight the test halts the run: the loop finishes
    the in-flight lane, claims no further wave, and transitions to DONE so the
    cockpit run-summary card opens -- the in-flight work is NOT reaped.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawned: list[str] = []
    spawn_lock = threading.Lock()
    first_in_flight = threading.Event()
    release = threading.Event()

    def _spawn(c: MethodContext, wave_id: str) -> str:
        with spawn_lock:
            spawned.append(wave_id)
        return f"ses-{wave_id}"

    def _watch(c: MethodContext, lane: FleetLane) -> str:
        if lane.wave_id == _WAVE_IDS[0]:
            first_in_flight.set()
            release.wait(timeout=5.0)
        return "closed"

    result: dict[str, Any] = {}

    def _drive() -> None:
        result["run"] = arm_drive(
            ctx,
            frontier=list(_WAVE_IDS),
            concurrency=1,
            spawn=_spawn,
            watch=_watch,
        )

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    try:
        assert first_in_flight.wait(timeout=5.0)
        halt_all(ctx)
        release.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    finally:
        release.set()
    run = result.get("run")
    assert run is not None
    # The halt drained the in-flight lane then ended DONE (the summary card).
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # New claims were blocked: only the first wave was claimed (2 + 3 never were).
    with spawn_lock:
        assert spawned == [_WAVE_IDS[0]]
    assert run.lanes == {}
