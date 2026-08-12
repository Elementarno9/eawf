"""Acceptance: a full armed autopilot run drains a two-wave frontier.

The autopilot-wiring capstone (CR-01). The W01-W09 waves each proved ONE leg of
the loop in isolation -- the backgrounded handle, the arm-spec -> DriveParams
mapping, the spawn / repair ladders, the frontier recompute, the
budget cap (W04 / DL-4), the watcher liveness, the pause / halt / resume control
RPCs, the cockpit reattach, and the jury / kill. None of them
proved the WHOLE chain end to end: an armed run claims -> dispatches -> watches
-> close-gates -> drains a real two-wave frontier under a budget cap, with the
``fleet_run`` evidence record + summary produced and assertable.

This module is that acceptance. It runs the REAL daemon-owned drive
(:func:`~eawf.runtime.daemon.methods.fleet.start_background_drive` and the
synchronous :func:`~eawf.runtime.daemon.methods.fleet.arm_drive` it backgrounds)
against a TEMP fixture state, never the repo's live ``.ea``. Because a live drive
would spawn REAL subagents (forbidden + costly), the spawn + watch + spend legs
are the SAME injectable fakes the W01-W09 daemon tests use: a stub spawner
returns a deterministic ``LaneDispatch`` (no subprocess), a stub watcher flips
each lane ``closed`` (no blocking disk poll), and a stub spend reader meters the
EU / USD the budget cap tests against. The drain, frontier advance, close-gate,
budget evaluation, summary computation, and canonical-writer persist are all the
PRODUCTION code paths -- only the subagent boundary is stubbed.

The "real run evidence" is therefore a TEMP-fixture ``fleet_run`` record + summary
(written through the daemon canonical writer to a ``tmp_path`` ``state.json``),
asserted off disk -- NOT a committed evidence fixture and NOT a live subagent
drive. That is the correct shape for an in-process acceptance: the loop produces
the same typed ``FleetRun`` summary it would in production, and the test asserts
the two-wave drain + budget cap honoured + counters / spend / throughput against
it.

The success criteria under test
--------------------------------
* C1 (arm returns fast): an armed drive over a two-wave ready frontier returns a
  DRAINING handle in well under a second while the drain continues on the worker
  thread.
* C2 (two-wave drain under cap): the stubbed spawner + watcher drain both lanes
  (claim -> dispatch -> watch-closed -> close-gate -> advance), the terminal
  ``fleet_run`` carries two drained lanes, and an armed budget cap is HONOURED
  (a third wave past the cap never claims, the run ends ``terminal_reason=budget``).
* C3 (evidence assertable): the ``fleet_run`` record + summary round-trip through
  the daemon canonical writer onto the temp ``state.json`` and are assertable off
  disk -- the closed count, the spend totals (under the cap), and the throughput
  the daemon computed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import eawf.runtime.daemon.methods.fleet as fleet_mod
from eawf.kernel.state.models import (
    FleetLane,
    FleetRunState,
    FleetTerminalReason,
    State,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    DriveParams,
    LaneDispatch,
    LaneSpend,
    arm_drive,
    drive_in_flight,
    shutdown_drive,
    start_background_drive,
)
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

# A three-wave frontier: a budget cap fires after the first two claim, so the
# third stays queued -- the run drains exactly two lanes under the cap.
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
    event_path = state_path.parent / "store" / "event.jsonl"
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=event_path,
        state_path=state_path,
    )


def _persisted(state_path: Path):  # type: ignore[no-untyped-def]
    return load_state(state_path).fleet_run


class _StubSpawner:
    """Deterministic stub spawner: records claim order, assigns a pgid, no subprocess.

    Mirrors the ``_RecordingSpawner`` fake the W04 budget tests use -- the
    subagent boundary is the ONLY thing stubbed, so the drive never launches a
    real (forbidden + costly) subagent.
    """

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self._next_pgid = 9000

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch:
        self.spawned.append(wave_id)
        self._next_pgid += 1
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=self._next_pgid)


@pytest.fixture(autouse=True)
def _clear_active_drive() -> Any:
    """Ensure no active drive leaks across tests (the registry is module-level)."""
    fleet_mod._ACTIVE_DRIVE[0] = None
    fleet_mod._LOOP_THREAD[0] = None
    yield
    shutdown_drive(timeout=2.0)
    fleet_mod._ACTIVE_DRIVE[0] = None
    fleet_mod._LOOP_THREAD[0] = None


# --------------------------------------------------------------------------
# C1: an armed drive returns a DRAINING handle fast while the drain continues
# --------------------------------------------------------------------------


def test_armed_run_returns_handle_fast_while_drain_continues(tmp_path: Path) -> None:
    """C1: start_background_drive returns a DRAINING handle in <1s; the drain finishes async.

    A gated watcher blocks each lane until the test releases it, so the drain is
    still in flight when the call returns. The arm answers in well under a second
    (it only arms IDLE -> DRAINING + starts the worker thread), and once the gate
    opens the two-wave frontier drains to completion. This is the end-to-end arm
    leg: the operator's ``a`` -> ArmModal -> DriveParams -> fleet.drive chain returns
    fast, never blocking the cockpit on the synchronous drain.
    """
    state_path = _write_state(tmp_path)
    release = threading.Event()
    spawner = _StubSpawner()

    def _gated_watch(c: MethodContext, lane: FleetLane) -> str:
        release.wait(timeout=5.0)
        return "closed"

    # Two-wave ready frontier, concurrency 2 so both lanes fill.
    params = DriveParams(frontier=_WAVE_IDS[:2], concurrency=2)

    original_watch = fleet_mod._default_watcher
    original_spawn = fleet_mod._default_spawner
    fleet_mod._default_watcher = _gated_watch  # type: ignore[assignment]
    fleet_mod._default_spawner = spawner  # type: ignore[assignment]
    try:

        async def _scenario() -> Any:
            return start_background_drive(_ctx(state_path), params)

        start = time.monotonic()
        handle = asyncio.run(_scenario())
        elapsed = time.monotonic() - start
        # The arm returned a DRAINING handle without blocking on the gated drain.
        assert elapsed < 1.0
        assert handle.run_state is FleetRunState.DRAINING
        assert handle.backgrounded is True
        assert handle.handle_id.startswith("fleet-run-")
        # The drain thread is still in flight (the watcher is gated).
        assert drive_in_flight() is True
        # Release the gate so both lanes close, then wait for the thread to drain.
        release.set()
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert drive_in_flight() is False
    finally:
        release.set()
        fleet_mod._default_watcher = original_watch  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawn  # type: ignore[assignment]
    # The two-wave frontier drained to a terminal DONE/drained run.
    run = _persisted(state_path)
    assert run is not None
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.closed == 2
    # Both frontier waves claimed -- the stub spawner recorded each claim.
    assert spawner.spawned == _WAVE_IDS[:2]


# --------------------------------------------------------------------------
# C2 + C3: the two-wave frontier drains under a budget cap; the run record +
# summary are produced + assertable off the canonical-writer persist
# --------------------------------------------------------------------------


def test_two_wave_frontier_drains_under_budget_cap_with_assertable_summary(
    tmp_path: Path,
) -> None:
    """C2/C3: a three-wave frontier drains exactly TWO lanes under an EU cap.

    The end-to-end drain: claim -> dispatch (stub spawner) -> watch-closed (stub
    watcher) -> close-gate -> advance, with each finished lane metering 1.0 EU
    against a 1.5 EU cap. The cap fires after the first lane finishes (spend 1.0
    < 1.5) and the second pushes it over (2.0 >= 1.5), so the THIRD wave never
    claims. Under the graceful-drain default the two in-flight lanes finish and
    the run ends DONE / ``terminal_reason=budget``.

    The terminal ``fleet_run`` record + summary are asserted off disk: two drained
    lanes (``closed == 2``), the cap honoured (only the first two waves claimed,
    spend totals match the metered EU / USD), and the daemon-computed summary
    fields present -- the in-process acceptance's "real run evidence" is this
    temp-fixture ``fleet_run`` written through the canonical writer.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _StubSpawner()

    # Run the drive synchronously (arm_drive is the loop start_background_drive
    # backgrounds onto its worker thread); a synchronous arm is the same loop,
    # observed inline for a deterministic terminal assertion.
    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        eu_cap=1.5,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=1.0, usd=0.5),
    )

    # --- the two-wave drain: exactly the first two waves claimed + closed -------
    assert spawner.spawned == _WAVE_IDS[:2]
    assert _WAVE_IDS[2] not in spawner.spawned  # the third wave never claimed
    assert run.run_state is FleetRunState.DONE
    assert run.counters.claimed == 2
    assert run.counters.closed == 2
    assert run.lanes == {}  # both lanes drained, none lingering

    # --- the budget cap was HONOURED: the run ended on the cap, not a full drain
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    assert run.counters.spent_eu == pytest.approx(2.0)  # 2 lanes x 1.0 EU
    assert run.counters.spent_usd == pytest.approx(1.0)  # 2 lanes x 0.5 USD
    assert run.eu_cap == pytest.approx(1.5)

    # --- C3: the run record + summary round-trip through the canonical writer ---
    persisted = _persisted(state_path)
    assert persisted is not None
    assert persisted.run_state is FleetRunState.DONE
    assert persisted.terminal_reason is FleetTerminalReason.BUDGET
    assert persisted.counters.closed == 2
    assert persisted.counters.spent_eu == pytest.approx(2.0)
    # The daemon stamped the run-summary fields on the terminal run.
    assert persisted.ended_at is not None
    assert persisted.armed_at is not None
    assert persisted.elapsed_hours is not None
    # Throughput is the daemon-computed closed-per-hour figure (>= 0 over a
    # non-zero window), present on the summary the cockpit reads.
    assert persisted.throughput is not None
    assert persisted.throughput >= 0.0

    # The terminal run re-validates off disk under strict State validation -- the
    # evidence record is a real typed FleetRun, not a hand-rolled fixture.
    reloaded = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert reloaded.fleet_run is not None
    assert reloaded.fleet_run.terminal_reason is FleetTerminalReason.BUDGET
    assert reloaded.fleet_run.counters.closed == 2


def test_full_drain_without_cap_drains_both_lanes_naturally(tmp_path: Path) -> None:
    """C2 (negative-cap path): a two-wave frontier under no cap drains both naturally.

    The companion to the budget-capped drain: armed over the same two-wave ready
    frontier with NO cap, the loop claims + drains both lanes and ends
    DONE / ``terminal_reason=drained`` -- the budget HALT is never taken, so the
    cap firing in the capped test is genuinely the cap, not an artefact of the
    frontier size.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _StubSpawner()

    run = arm_drive(
        ctx,
        frontier=_WAVE_IDS[:2],
        concurrency=2,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=5.0, usd=5.0),
    )

    assert spawner.spawned == _WAVE_IDS[:2]
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.claimed == 2
    assert run.counters.closed == 2


# --------------------------------------------------------------------------
# The arm chain rejects a second drive while one is in flight (single-active run)
# --------------------------------------------------------------------------


def test_second_arm_while_draining_is_rejected(tmp_path: Path) -> None:
    """A second start_background_drive while a run is DRAINING is rejected.

    The cockpit arm chain enforces a single active run: arming a second drive
    while the first is mid-drain raises rather than racing two loops over the same
    state. Mirrors the W01 in-flight guard, asserted through the acceptance arm
    surface.
    """
    from eawf.workflow.lifecycle._errors import LifecycleError

    state_path = _write_state(tmp_path)
    release = threading.Event()

    def _gated_watch(c: MethodContext, lane: FleetLane) -> str:
        release.wait(timeout=5.0)
        return "closed"

    params = DriveParams(frontier=_WAVE_IDS[:2], concurrency=1)

    original_watch = fleet_mod._default_watcher
    original_spawn = fleet_mod._default_spawner
    fleet_mod._default_watcher = _gated_watch  # type: ignore[assignment]
    fleet_mod._default_spawner = _StubSpawner()  # type: ignore[assignment]
    try:

        async def _scenario() -> None:
            ctx = _ctx(state_path)
            first = start_background_drive(ctx, params)
            assert first.run_state is FleetRunState.DRAINING
            await asyncio.sleep(0.05)
            with pytest.raises(LifecycleError, match="already in flight"):
                start_background_drive(ctx, params)

        asyncio.run(_scenario())
    finally:
        release.set()
        deadline = time.monotonic() + 5.0
        while drive_in_flight() and time.monotonic() < deadline:
            time.sleep(0.02)
        fleet_mod._default_watcher = original_watch  # type: ignore[assignment]
        fleet_mod._default_spawner = original_spawn  # type: ignore[assignment]
