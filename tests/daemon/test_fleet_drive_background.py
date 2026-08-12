"""Tests: fleet.drive backgrounded off the daemon event loop.

The fleet drain is SYNCHRONOUS (claim -> dispatch -> blocking watch -> advance),
so it would block the awaited ``fleet.drive`` RPC handler -- and every other RPC
on the daemon event loop -- if it ran inline. These assertions confirm the W01
backgrounding contract:

- C1: ``fleet.drive`` returns a run handle in under one second while the drain
  continues on a worker thread, and the run reaches its terminal state once the
  drain finishes (read off the persisted ``state.fleet_run``).
- C2: a concurrent ``daemon.ping`` answers on the daemon event loop while the
  drive thread is mid-drain (a blocking watcher freezes a lane in flight).
- C3: every run transition still persists through the daemon canonical writer
  exactly as the synchronous form did (the run round-trips on disk).
- C4: the bus publishes the run transition; a publish from the drive thread
  marshals onto the daemon loop through ``loop.call_soon_threadsafe`` rather
  than touching the bus' ``asyncio.Event`` off the loop thread.
- C5: a second ``fleet.drive`` while a run is DRAINING is rejected.
- C6: daemon shutdown (:func:`shutdown_drive`) signals the drive thread + joins
  it cleanly, leaving the run DRAINING on disk for a reattach to recover.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import FleetLane, FleetRunState, FleetTerminalReason, State
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import fleet as fleet_mod
from eawf.runtime.daemon.methods.daemon import ping
from eawf.runtime.daemon.methods.fleet import (
    drive,
    drive_in_flight,
    shutdown_drive,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I17-W01", "P30-I17-W02", "P30-I17-W03"]


def _state_payload(*, dispatch_paused: bool = False) -> dict[str, Any]:
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


def _write_state(tmp_path: Path, *, dispatch_paused: bool = False) -> Path:
    state = State.model_validate(_state_payload(dispatch_paused=dispatch_paused))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


class _RecordingBus:
    """Captures published envelopes + the thread each publish ran on."""

    def __init__(self) -> None:
        self.published: list[Envelope] = []
        self.publish_threads: list[threading.Thread] = []

    def publish(self, envelope: Envelope) -> None:
        self.published.append(envelope)
        self.publish_threads.append(threading.current_thread())


def _ctx(state_path: Path | None, *, bus: Any = None) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl" if state_path is not None else None
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=event_path,
        state_path=state_path,
        bus=bus,
    )


def _persisted_run(state_path: Path):  # type: ignore[no-untyped-def]
    return load_state(state_path).fleet_run


@pytest.fixture(autouse=True)
def _clear_active_drive() -> Any:
    """Ensure no active drive leaks across tests (the registry is module-level)."""
    fleet_mod._ACTIVE_DRIVE[0] = None
    fleet_mod._LOOP_THREAD[0] = None
    yield
    shutdown_drive(timeout=2.0)
    fleet_mod._ACTIVE_DRIVE[0] = None
    fleet_mod._LOOP_THREAD[0] = None


# ---- C1: the RPC returns a handle in <1s while the drain runs in the background


def test_drive_returns_handle_fast_while_drain_continues(tmp_path: Path) -> None:
    """C1: fleet.drive returns a DRAINING handle quickly; the drain finishes async.

    A gated watcher blocks each lane until the test releases it, so the drain is
    still in flight when the RPC returns. The RPC answers in well under a second
    (it only arms + starts the worker thread), and once the gate opens the drain
    completes and persists the terminal DONE/drained run.
    """
    state_path = _write_state(tmp_path)
    release = threading.Event()
    fleet_mod._ACTIVE_DRIVE[0] = None

    def _gated_watch(c: MethodContext, lane: FleetLane) -> str:
        release.wait(timeout=5.0)
        return "closed"

    async def _scenario() -> dict[str, Any]:
        ctx = _ctx(state_path)
        return await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})

    # The RPC default-watcher path blocks on disk polling; inject a gated watcher
    # by patching the module default so the live-default path uses it.
    original = fleet_mod._default_watcher
    fleet_mod._default_watcher = _gated_watch  # type: ignore[assignment]
    # Also stub the default spawner so no real subprocess is spawned.
    original_spawner = fleet_mod._default_spawner
    fleet_mod._default_spawner = lambda c, wid: f"ses-{wid}"  # type: ignore[assignment]
    try:
        start = time.monotonic()
        handle = asyncio.run(_scenario())
        elapsed = time.monotonic() - start
        # The RPC returned before the gated watcher's five-second timeout, so it
        # did not block on the drain. Leave scheduler headroom for parallel CI.
        assert elapsed < 4.0
        assert handle["run_state"] == "draining"
        assert handle["backgrounded"] is True
        assert handle["handle_id"].startswith("fleet-run-")
        # The drive thread is still in flight (the watcher is gated).
        assert drive_in_flight() is True
        # Release the gate so the drain completes, then wait for the thread.
        release.set()
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert drive_in_flight() is False
    finally:
        release.set()
        fleet_mod._default_watcher = original  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawner  # type: ignore[assignment]
    # The terminal run persisted DONE/drained through the canonical writer.
    run = _persisted_run(state_path)
    assert run is not None
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED


# ---- C2: a concurrent daemon.ping answers mid-drain --------------------------


def test_concurrent_ping_answers_during_background_drain(tmp_path: Path) -> None:
    """C2: daemon.ping answers on the loop while the drive thread is mid-drain.

    A gated watcher freezes a lane in flight, so the drive thread is blocked
    inside the drain. The daemon event loop is free (the drain is off-loop), so
    a concurrent ``daemon.ping`` returns its ``ok`` payload immediately rather
    than waiting for the drain to finish.
    """
    state_path = _write_state(tmp_path)
    release = threading.Event()

    def _gated_watch(c: MethodContext, lane: FleetLane) -> str:
        release.wait(timeout=5.0)
        return "closed"

    async def _scenario() -> dict[str, Any]:
        ctx = _ctx(state_path)
        handle = await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})
        assert handle["run_state"] == "draining"
        # Give the worker thread a moment to enter the gated watch.
        await asyncio.sleep(0.05)
        # The loop is free: ping answers immediately even though the drain blocks.
        pong = await asyncio.wait_for(ping(ctx, {}), timeout=1.0)
        return pong

    original = fleet_mod._default_watcher
    original_spawner = fleet_mod._default_spawner
    fleet_mod._default_watcher = _gated_watch  # type: ignore[assignment]
    fleet_mod._default_spawner = lambda c, wid: f"ses-{wid}"  # type: ignore[assignment]
    try:
        pong = asyncio.run(_scenario())
        # The ping returned its live payload (the daemon pid) while the drain was
        # still gated -- the loop answered it off-loop from the worker drive.
        assert pong["pid"] == 4321
        assert drive_in_flight() is True
    finally:
        release.set()
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            time.sleep(0.02)
        fleet_mod._default_watcher = original  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawner  # type: ignore[assignment]


# ---- C3: every transition persists through the canonical writer --------------


def test_background_drive_persists_every_transition_on_disk(tmp_path: Path) -> None:
    """C3: the backgrounded run round-trips on disk through the canonical writer.

    After the drain completes the persisted ``state.fleet_run`` carries the
    terminal snapshot, written through the daemon canonical writer (the same
    portalock + atomic-write path every mutator takes) -- the drive thread never
    opens ``state.json`` directly.
    """
    state_path = _write_state(tmp_path)

    async def _scenario() -> None:
        ctx = _ctx(state_path)
        await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})

    original = fleet_mod._default_watcher
    original_spawner = fleet_mod._default_spawner
    fleet_mod._default_watcher = lambda c, lane: "closed"  # type: ignore[assignment]
    fleet_mod._default_spawner = lambda c, wid: f"ses-{wid}"  # type: ignore[assignment]
    try:
        asyncio.run(_scenario())
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        fleet_mod._default_watcher = original  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawner  # type: ignore[assignment]
    # Round-trips through strict State validation off disk.
    reloaded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert reloaded.fleet_run is not None
    assert reloaded.fleet_run.run_state is FleetRunState.DONE
    assert reloaded.fleet_run.terminal_reason is FleetTerminalReason.DRAINED
    assert reloaded.fleet_run.counters.closed == 3


# ---- C4: bus publishes marshal through loop.call_soon_threadsafe -------------


def test_bus_publishes_marshal_onto_loop_thread(tmp_path: Path) -> None:
    """C4: a drive-thread publish marshals onto the daemon loop thread.

    The bus' ``asyncio.Event.set`` is only safe on the loop thread, so a publish
    from the worker drive thread must hop back via ``loop.call_soon_threadsafe``.
    The recording bus captures the thread each publish ran on; every captured
    publish ran on the daemon event-loop thread, never the worker drive thread.
    """
    state_path = _write_state(tmp_path)
    bus = _RecordingBus()
    loop_thread_holder: list[threading.Thread] = []

    async def _scenario() -> None:
        loop_thread_holder.append(threading.current_thread())
        ctx = _ctx(state_path, bus=bus)
        await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})
        # Wait for the worker drive thread to finish, draining the loop's
        # call_soon_threadsafe callbacks each tick so the marshalled publishes run.
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        # One more tick so any trailing marshalled publishes flush.
        await asyncio.sleep(0.05)

    original = fleet_mod._default_watcher
    original_spawner = fleet_mod._default_spawner
    fleet_mod._default_watcher = lambda c, lane: "closed"  # type: ignore[assignment]
    fleet_mod._default_spawner = lambda c, wid: f"ses-{wid}"  # type: ignore[assignment]
    try:
        asyncio.run(_scenario())
    finally:
        fleet_mod._default_watcher = original  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawner  # type: ignore[assignment]
    loop_thread = loop_thread_holder[0]
    # The drive published run transitions on the bus (at least the DRAINING arm +
    # the terminal DONE).
    assert len(bus.published) >= 2
    assert any(e.payload.get("event_kind") == "state_mutated" for e in bus.published)
    # EVERY publish ran on the daemon event-loop thread -- the worker drive thread
    # marshalled its publishes back through call_soon_threadsafe.
    assert all(t is loop_thread for t in bus.publish_threads)


# ---- C5: a second drive while DRAINING is rejected ---------------------------


def test_second_drive_while_draining_is_rejected(tmp_path: Path) -> None:
    """C5: a second fleet.drive while a run is in flight raises LifecycleError."""
    state_path = _write_state(tmp_path)
    release = threading.Event()

    def _gated_watch(c: MethodContext, lane: FleetLane) -> str:
        release.wait(timeout=5.0)
        return "closed"

    async def _scenario() -> None:
        ctx = _ctx(state_path)
        first = await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})
        assert first["run_state"] == "draining"
        await asyncio.sleep(0.05)
        # A second drive while the first is in flight is rejected.
        with pytest.raises(LifecycleError, match="already in flight"):
            await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})

    original = fleet_mod._default_watcher
    original_spawner = fleet_mod._default_spawner
    fleet_mod._default_watcher = _gated_watch  # type: ignore[assignment]
    fleet_mod._default_spawner = lambda c, wid: f"ses-{wid}"  # type: ignore[assignment]
    try:
        asyncio.run(_scenario())
    finally:
        release.set()
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            time.sleep(0.02)
        fleet_mod._default_watcher = original  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawner  # type: ignore[assignment]


# ---- C6: daemon shutdown signals + joins the drive thread cleanly ------------


def test_shutdown_drive_signals_and_joins_cleanly(tmp_path: Path) -> None:
    """C6: shutdown_drive raises the cancel event + joins the worker thread.

    A gated watcher freezes the first lane; shutdown_drive sets the cancel event
    so the loop stops claiming further waves between rounds and the worker thread
    joins. The run is left DRAINING on disk (not DONE) for a later reattach to
    recover the in-flight lanes -- shutdown never reaps live work.
    """
    state_path = _write_state(tmp_path)
    release = threading.Event()
    watched: list[str] = []

    def _gated_watch(c: MethodContext, lane: FleetLane) -> str:
        watched.append(lane.wave_id)
        release.wait(timeout=5.0)
        return "closed"

    async def _scenario() -> None:
        ctx = _ctx(state_path)
        await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})
        await asyncio.sleep(0.05)

    original = fleet_mod._default_watcher
    original_spawner = fleet_mod._default_spawner
    fleet_mod._default_watcher = _gated_watch  # type: ignore[assignment]
    fleet_mod._default_spawner = lambda c, wid: f"ses-{wid}"  # type: ignore[assignment]
    try:
        asyncio.run(_scenario())
        assert drive_in_flight() is True
        # Shutdown: signal the cancel + release the gated lane so the loop sees
        # the cancel on its next round and stops; the join then succeeds.
        active = fleet_mod._ACTIVE_DRIVE[0]
        assert active is not None
        active.cancel.set()
        release.set()
        shutdown_drive(timeout=5.0)
        # The thread joined cleanly -- no live drive remains.
        assert drive_in_flight() is False
    finally:
        release.set()
        fleet_mod._default_watcher = original  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawner  # type: ignore[assignment]
    # The cancel stopped further claims: not all three waves were watched (the
    # first lane finished, then the cancel halted the next round's claim).
    assert len(watched) < len(_WAVE_IDS)
    # The run is left DRAINING on disk for a reattach to recover (not DONE).
    run = _persisted_run(state_path)
    assert run is not None
    assert run.run_state is FleetRunState.DRAINING


def test_shutdown_drive_no_active_run_is_noop(tmp_path: Path) -> None:
    """C6: shutdown_drive with no active drive is a no-op (idle daemon shuts down)."""
    fleet_mod._ACTIVE_DRIVE[0] = None
    # No raise, returns immediately.
    shutdown_drive(timeout=1.0)
    assert drive_in_flight() is False


# ---- paused arm stays idle, no worker thread ---------------------------------


def test_paused_drive_stays_idle_no_thread(tmp_path: Path) -> None:
    """A drive armed while dispatch_paused stays IDLE + starts no worker thread."""
    state_path = _write_state(tmp_path, dispatch_paused=True)

    async def _scenario() -> dict[str, Any]:
        ctx = _ctx(state_path)
        return await drive(ctx, {"frontier": list(_WAVE_IDS), "concurrency": 1})

    handle = asyncio.run(_scenario())
    assert handle["run_state"] == "idle"
    assert handle["backgrounded"] is False
    # No worker thread was started.
    assert drive_in_flight() is False
    # The frontier is staged IDLE on disk.
    run = _persisted_run(state_path)
    assert run is not None
    assert run.run_state is FleetRunState.IDLE
    assert run.frontier == _WAVE_IDS
