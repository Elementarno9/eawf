"""Tests: fleet budget HALT teeth (P30-I12-W04 / DL-4).

Exercises the budget cap the fleet auto-drain loop applies at the claim gate:
once an armed spend cap (EU / USD / waves) is reached the loop claims no
further wave. Two modal resolutions of the in-flight lanes are covered:

* C1 (graceful drain, the DEFAULT): at the cap the loop stops claiming, the
  in-flight lanes are watched to completion, and the run ends DONE with
  ``terminal_reason=budget``. The waves past the cap never claim.
* C2 (hard halt, the armed toggle): reaching the cap KILLS the in-flight lanes
  via the DL-3 :func:`~eawf.runtime.daemon.methods.fleet.kill_lane` instead of
  draining them. The NEGATIVE PATH -- a run far under every armed cap (or armed
  with no cap) -- never triggers the budget HALT and drains naturally.

Every assertion drives the loop with injectable fakes for spawn / watch / spend
so the cap fires on injected EU figures without a real runtime sidecar, and the
kill path's group-signal seam is patched so no real signal is delivered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import FleetRunState, FleetTerminalReason, State
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import fleet as fleet_mod
from eawf.runtime.daemon.methods.fleet import (
    LaneDispatch,
    LaneSpend,
    arm_drive,
    budget_exhausted,
)
from eawf.runtime.runtimes.cancel import CancelResult
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

# Four PENDING frontier waves so a cap can fire with waves still queued.
_WAVE_IDS = ["P30-I12-W01", "P30-I12-W02", "P30-I12-W03", "P30-I12-W04"]


def _state_payload() -> dict[str, Any]:
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


class _RecordingSpawner:
    """Deterministic spawner fake that records claim order + assigns a pgid.

    Each spawned lane carries a real ``pgid`` so the hard-halt kill path
    resolves a killable lane (the registry never holds a fabricated pid; here
    the fakes supply a deterministic group id per claim).
    """

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self._next_pgid = 9000

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch:
        self.spawned.append(wave_id)
        self._next_pgid += 1
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=self._next_pgid)


def _persisted(state_path: Path) -> Any:
    return load_state(state_path).fleet_run


# ---- C1: graceful drain at the EU cap ---------------------------------------


def test_eu_cap_graceful_drain_stops_claiming_and_drains_in_flight(tmp_path: Path) -> None:
    """C1: at the EU cap the loop claims no further wave; in-flight lanes drain.

    A 4-wave frontier at concurrency 2 with each finished lane spending 1.0 EU
    and an ``eu_cap`` of 1.5 fires the cap after the first lane finishes (spend
    1.0 < 1.5) and the second pushes it over (2.0 >= 1.5). Under the
    graceful-drain default the in-flight lanes finish and the run ends
    DONE/budget WITHOUT claiming waves 3 + 4.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        eu_cap=1.5,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=1.0, usd=0.0),
    )

    # Exactly the first concurrency=2 waves claimed; waves 3 + 4 never claimed.
    assert spawner.spawned == _WAVE_IDS[:2]
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    assert run.counters.claimed == 2
    # Both in-flight lanes finished cleanly under the graceful drain.
    assert run.counters.closed == 2
    assert run.counters.spent_eu == pytest.approx(2.0)
    assert run.lanes == {}
    # The budget terminal round-trips through the daemon canonical writer.
    persisted = _persisted(state_path)
    assert persisted is not None
    assert persisted.terminal_reason is FleetTerminalReason.BUDGET


def test_usd_cap_graceful_drain_ends_budget(tmp_path: Path) -> None:
    """C1: the USD cap fires identically to the EU cap (graceful drain)."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        usd_cap=1.5,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=0.0, usd=1.0),
    )
    assert spawner.spawned == _WAVE_IDS[:2]
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    assert run.counters.spent_usd == pytest.approx(2.0)


def test_waves_cap_stops_claiming(tmp_path: Path) -> None:
    """C1: the waves cap fires on claimed count, ending DONE/budget."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        waves_cap=2,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(),
    )
    # The waves cap is met as soon as 2 waves are claimed; 3 + 4 never claim.
    assert spawner.spawned == _WAVE_IDS[:2]
    assert run.counters.claimed == 2
    assert run.terminal_reason is FleetTerminalReason.BUDGET


# ---- C2: hard halt kills in-flight lanes; negative path never HALTs ----------


def test_hard_halt_kills_in_flight_lanes_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: with hard_halt armed, reaching the cap KILLS the in-flight lanes.

    A watcher that holds the second lane RUNNING freezes it in flight while the
    first lane finishes and pushes spend over the cap. Under the armed hard halt
    the remaining in-flight lane is signalled via the DL-3 kill (the group
    signal seam is patched so no real signal is delivered) rather than drained,
    and the run ends DONE/budget.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    killed_pgids: list[int] = []

    def _fake_signal(pgid: int, *, hard: bool = False) -> CancelResult:
        killed_pgids.append(pgid)
        return CancelResult(pgid=pgid, signal_sent=9, delivered=True)

    monkeypatch.setattr(fleet_mod, "cancel_process_group", _fake_signal)

    # The first lane watched in the round finishes and its 1.0 EU spend pushes
    # the run over the 0.5 cap. The drain then breaks on the budget stop BEFORE
    # watching the second lane, so the second lane stays in flight for the
    # hard-halt kill (the watcher never runs on it).
    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        eu_cap=0.5,
        hard_halt=True,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=1.0, usd=0.0),
    )

    # The first finished lane (1.0 EU >= 0.5 cap) tripped the budget stop with
    # the frontier still holding waves, so the second in-flight lane was KILLED
    # rather than drained.
    assert spawner.spawned == _WAVE_IDS[:2]
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    assert killed_pgids, "hard halt must signal the in-flight lane's pgid"
    # The killed lane was deregistered + counted a fork; no further wave claimed.
    assert run.counters.claimed == 2
    assert run.lanes == {}


def test_far_under_budget_never_triggers_halt(tmp_path: Path) -> None:
    """C2 (negative path): a run far under every cap drains naturally, no HALT.

    A 4-wave frontier with each lane spending 0.1 EU under a generous 100.0 EU
    cap never reaches the cap, so the run drains all four waves and ends
    DONE/drained -- the budget HALT path is never taken.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        eu_cap=100.0,
        usd_cap=100.0,
        waves_cap=100,
        hard_halt=True,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=0.1, usd=0.1),
    )
    # Every wave claimed + closed; the run drained naturally, NOT on a budget.
    assert spawner.spawned == _WAVE_IDS
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.claimed == 4
    assert run.counters.closed == 4


def test_uncapped_run_never_triggers_halt(tmp_path: Path) -> None:
    """C2 (negative path): a run armed with no cap drains naturally, no HALT."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _RecordingSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=5.0, usd=5.0),
    )
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.claimed == 4


# ---- budget_exhausted pure-function matrix ----------------------------------


def _run_with(**kwargs: Any) -> Any:
    from eawf.kernel.state.models import FleetCounters, FleetRun

    base: dict[str, Any] = {
        "armed_at": "2026-06-11T00:00:00Z",
        "frontier": ["P30-I12-W01"],
        "counters": FleetCounters(),
    }
    base.update(kwargs)
    return FleetRun(**base)


def test_budget_exhausted_eu_boundary() -> None:
    """The EU cap fires at-or-above the cap (>=) and never below it."""
    from eawf.kernel.state.models import FleetCounters

    assert budget_exhausted(_run_with(eu_cap=2.0, counters=FleetCounters(spent_eu=2.0))) is True
    assert budget_exhausted(_run_with(eu_cap=2.0, counters=FleetCounters(spent_eu=1.99))) is False


def test_budget_exhausted_no_cap_is_never_exhausted() -> None:
    """A run with no armed cap is never exhausted regardless of spend."""
    from eawf.kernel.state.models import FleetCounters

    counters = FleetCounters(spent_eu=999.0, spent_usd=999.0, claimed=999)
    assert budget_exhausted(_run_with(counters=counters)) is False


def test_budget_exhausted_waves_cap_boundary() -> None:
    """The waves cap fires when claimed reaches it."""
    from eawf.kernel.state.models import FleetCounters

    assert budget_exhausted(_run_with(waves_cap=3, counters=FleetCounters(claimed=3))) is True
    assert budget_exhausted(_run_with(waves_cap=3, counters=FleetCounters(claimed=2))) is False


# ---- back-compat: a pre-W04 FleetRun re-validates ----------------------------


def test_pre_w04_fleet_run_revalidates() -> None:
    """A FleetRun payload without the W04 budget fields re-validates (back-compat)."""
    from eawf.kernel.state.models import FleetRun

    legacy = {
        "run_state": "draining",
        "concurrency": 2,
        "frontier": ["P30-I12-W01"],
        "lanes": {},
        "counters": {
            "claimed": 1,
            "dispatched": 1,
            "closed": 0,
            "forked": 0,
            "rounds": 0,
            "clean_rounds": 0,
        },
        "convergence": "drain",
        "kclean_k": 2,
        "terminal_reason": None,
        "armed_at": "2026-06-11T00:00:00Z",
    }
    run = FleetRun.model_validate(legacy)
    # The new fields default to uncapped / graceful-drain / zero spend.
    assert run.eu_cap is None
    assert run.usd_cap is None
    assert run.waves_cap is None
    assert run.hard_halt is False
    assert run.counters.spent_eu == 0.0
    assert run.counters.spent_usd == 0.0
