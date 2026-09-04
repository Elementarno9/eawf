"""Tests: arm-form budget caps reach the DL-4 teeth + hard-halt.

The W02 fix threads the arm form's derived EU / USD / waves caps + the hard-halt
toggle into ``DriveParams`` rather than dropping them. These assertions bind the
arm-form-derived figures to the daemon's existing DL-4 budget HALT so a
budget-capped arm actually stops claiming at the cap, and a hard-halt arm reaps
the in-flight lanes instead of draining them.

The cap figures come from the arm form's own
:func:`~eawf.surfaces.tui.screens.overlays.arm.build_arm_spec` budget tier, so
the test proves the tier the operator picks reaches the daemon teeth end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import FleetRunState, FleetTerminalReason, State
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import fleet as fleet_mod
from eawf.runtime.daemon.methods.fleet import LaneDispatch, LaneSpend, arm_drive
from eawf.runtime.runtimes.cancel import CancelResult
from eawf.surfaces.tui.screens.overlays.arm import build_arm_spec
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_IDS = [
    "P30-I17-W01",
    "P30-I17-W02",
    "P30-I17-W03",
    "P30-I17-W04",
    "P30-I17-W05",
]


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


class _PgidSpawner:
    """Spawner fake assigning a real pgid per lane so a hard-halt kill resolves."""

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self._next_pgid = 9000

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch:
        self.spawned.append(wave_id)
        self._next_pgid += 1
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=self._next_pgid)


def test_strict_tier_waves_cap_halts_at_the_cap(tmp_path: Path) -> None:
    """A strict-tier arm's waves cap reaches the DL-4 teeth and stops claiming.

    The arm form's ``strict`` budget tier derives ``waves_cap=4``; driving the
    loop with that cap over a wider frontier HALTs once the claimed count hits
    the cap, ending DONE/budget -- the cap the operator picked is load-bearing,
    not dropped.
    """
    spec = build_arm_spec(
        scope="cross-repo",
        budget="strict",
        concurrency_option="1 lane",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.waves_cap == 4
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _PgidSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),  # 5 waves, wider than the strict cap of 4
        concurrency=1,
        waves_cap=spec.waves_cap,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=0.0, usd=0.0),
    )
    # The claimed count reached the cap with a wave still queued; the run ended
    # on the budget HALT rather than draining the full frontier.
    assert run.counters.claimed == 4
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    assert spawner.spawned == _WAVE_IDS[:4]


def test_standard_tier_eu_cap_halts(tmp_path: Path) -> None:
    """A standard-tier arm's EU cap reaches the DL-4 teeth.

    The ``standard`` tier derives ``eu_cap=16.0``; with each lane spending 8.0
    EU the cap fires after two lanes finish (16.0 >= 16.0), ending DONE/budget.
    """
    spec = build_arm_spec(
        scope="cross-repo",
        budget="standard",
        concurrency_option="2 lanes",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.eu_cap == 16.0
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _PgidSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        eu_cap=spec.eu_cap,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: LaneSpend(eu=8.0, usd=0.0),
    )
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    assert run.counters.spent_eu == pytest.approx(16.0)
    # Waves 3 + 4 never claimed -- the cap stopped claiming at the budget.
    assert spawner.spawned == _WAVE_IDS[:2]


def test_hard_halt_arm_reaps_in_flight_lanes_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard-halt arm reaps the in-flight lanes at the cap instead of draining.

    The ``hard-halt`` risk policy sets ``hard_halt=True``; with a budget cap that
    fires mid-round the loop KILLS the still-in-flight lanes (DL-3) rather than
    draining them. The group-signal seam is patched so no real signal lands.
    """
    spec = build_arm_spec(
        scope="cross-repo",
        budget="strict",
        concurrency_option="2 lanes",
        risk_policy="auto-close, hard-halt on fail",
        convergence_option="drain to empty",
    )
    assert spec.hard_halt is True
    assert spec.eu_cap == 4.0
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _PgidSpawner()
    killed: list[tuple[int, bool]] = []

    import signal as _signal

    def _fake_cancel(pgid: int, *, hard: bool) -> CancelResult:
        killed.append((pgid, hard))
        sig = _signal.SIGKILL if hard else _signal.SIGTERM
        return CancelResult(pgid=pgid, signal_sent=int(sig), delivered=True)

    monkeypatch.setattr(fleet_mod, "cancel_process_group", _fake_cancel)

    # First finished lane spends 5.0 EU (> 4.0 cap) so the cap fires mid-drain
    # with the sibling lane still in flight and frontier work queued.
    spends = iter([LaneSpend(eu=5.0, usd=0.0), LaneSpend(eu=0.0, usd=0.0)])

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        eu_cap=spec.eu_cap,
        hard_halt=spec.hard_halt,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: next(spends),
    )
    assert run.terminal_reason is FleetTerminalReason.BUDGET
    # The hard halt reaped the still-in-flight lane via the kill ladder rather
    # than draining it -- at least one group was signalled with SIGKILL.
    assert killed and all(hard for _pgid, hard in killed)
    persisted = load_state(state_path).fleet_run
    assert persisted is not None
    assert persisted.run_state is FleetRunState.DONE
