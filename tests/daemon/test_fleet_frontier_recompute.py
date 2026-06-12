"""Tests: dynamic per-round frontier recompute (P30-I17-W04).

The fleet frontier was frozen at arm time, so a dep-unblocked wave could never
join a run armed before its dep closed -- a dep-chain iter drained one layer and
reported drained. These assertions confirm the W04 fix: the loop recomputes the
ready frontier off ``state.json`` each round and merges newly-unblocked waves, so
a multi-layer dep chain armed at its first layer drains to empty in one run.

- C1: a 3-layer dep chain (W01 -> W02 -> W03) armed with only the ready layer-1
  wave drains all three to CLOSED in one run, as each layer's close unblocks the
  next.
- C2: the recompute is purely additive -- it never re-adds an in-flight, queued,
  already-claimed, or forked wave.
- C3: recompute_frontier=False freezes the frontier at the armed list (the
  pre-W04 behaviour), so a dep chain armed at layer 1 drains only that layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import FleetRunState, FleetTerminalReason, State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import LaneDispatch, LaneOutcome, arm_drive
from eawf.runtime.lock import portalock
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle.wave import claim_wave, close_wave

pytestmark = pytest.mark.integration

# A 3-layer dep chain: W01 (ready) -> W02 -> W03.
_CHAIN = ["P30-I17-W01", "P30-I17-W02", "P30-I17-W03"]
_DEPS = {
    "P30-I17-W01": [],
    "P30-I17-W02": ["P30-I17-W01"],
    "P30-I17-W03": ["P30-I17-W02"],
}


def _state_payload() -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid in _CHAIN:
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I17",
            "title": f"Chain wave {wid[-3:]}",
            "status": "pending",
            "deps": _DEPS[wid],
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
                "wave_ids": list(_CHAIN),
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


def _claiming_spawner(state_path: Path) -> Any:
    """A spawner that claims the wave in state so close_wave's edge is legal."""
    pgid = [9000]

    def _spawn(ctx: MethodContext, wave_id: str) -> LaneDispatch:
        with portalock.acquire(state_path, timeout=5.0):
            state = load_state(state_path)
            claim_wave(state, wave_id=wave_id, session_id=f"ses-{wave_id}", out_of_order=True)
            state.updated_at = datetime.now(UTC)
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
        pgid[0] += 1
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=pgid[0])

    return _spawn


def _closing_watcher(state_path: Path) -> Any:
    """A watcher that closes the lane's wave in state so its dependents unblock."""

    def _watch(ctx: MethodContext, lane: Any) -> LaneOutcome:
        with portalock.acquire(state_path, timeout=5.0):
            state = load_state(state_path)
            close_wave(state, wave_id=lane.wave_id, outcome="drained by W04 test")
            state.updated_at = datetime.now(UTC)
            atomic_write_json_locked(state_path, state.model_dump(mode="json"))
        return "closed"

    return _watch


# ---- C1: a 3-layer dep chain armed at layer 1 drains to empty in one run -----


def test_dep_chain_drains_to_empty_in_one_run(tmp_path: Path) -> None:
    """C1: a 3-layer dep chain armed with only W01 drains all three in one run.

    As W01 closes its dependent W02 becomes ready and the per-round recompute
    pulls it onto the frontier; the same unblocks W03 once W02 closes. The run
    drains to DONE/drained with every chain wave CLOSED -- no wave is stranded
    off the armed frontier.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    # Armed with ONLY the ready layer-1 wave -- W02 + W03 are dep-blocked at arm.
    run = arm_drive(
        ctx,
        frontier=["P30-I17-W01"],
        concurrency=1,
        spawn=_claiming_spawner(state_path),
        watch=_closing_watcher(state_path),
    )
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # All three chain waves were claimed + closed in the single run.
    assert run.counters.claimed == 3
    assert run.counters.closed == 3
    # Every wave reached CLOSED in state -- the chain drained to empty.
    final = load_state(state_path)
    from eawf.kernel.state.enums import WaveStatus

    assert all(final.waves[wid].status is WaveStatus.CLOSED for wid in _CHAIN)


# ---- C3: recompute_frontier=False freezes the frontier (pre-W04) -------------


def test_frozen_frontier_drains_only_armed_layer(tmp_path: Path) -> None:
    """C3: recompute_frontier=False drains only the armed layer (pre-W04).

    With the recompute disabled the frontier is frozen at the armed list, so a
    dep chain armed at W01 drains only W01 and reports drained -- the exact
    pre-W04 stranding the recompute fixes.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    run = arm_drive(
        ctx,
        frontier=["P30-I17-W01"],
        concurrency=1,
        spawn=_claiming_spawner(state_path),
        watch=_closing_watcher(state_path),
        recompute_frontier=False,
    )
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # Only the armed layer-1 wave drained; W02 + W03 stayed off the frozen frontier.
    assert run.counters.claimed == 1
    assert run.counters.closed == 1


# ---- C2: the recompute is additive -- never re-adds a tracked wave -----------


def test_recompute_never_readds_in_flight_or_claimed_wave(tmp_path: Path) -> None:
    """C2: the recompute never re-adds an in-flight / claimed / queued wave.

    A spawner + watcher that DO NOT flip wave status (the waves stay PENDING in
    state) would, on a naive recompute, re-add every ready wave each round. The
    claimed-id + frontier-membership dedup prevents that: each chain-independent
    wave is claimed + closed exactly once.
    """
    # Use a no-dep 3-wave graph so every wave is ready every round; the dedup is
    # the only thing stopping a re-claim.
    state_path = _write_state(tmp_path)
    state = load_state(state_path)
    for wid in _CHAIN:
        state.waves[wid].deps = []
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    ctx = _ctx(state_path)
    spawned: list[str] = []

    def _spawn(c: MethodContext, wave_id: str) -> LaneDispatch:
        # Does NOT flip state -- the wave stays PENDING, so a naive recompute
        # would re-offer it every round.
        spawned.append(wave_id)
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=9500 + len(spawned))

    run = arm_drive(
        ctx,
        frontier=list(_CHAIN),
        concurrency=1,
        spawn=_spawn,
        watch=lambda c, lane: "closed",
    )
    assert run.run_state is FleetRunState.DONE
    # Each wave was claimed exactly once -- no re-add despite the PENDING status.
    assert sorted(spawned) == sorted(_CHAIN)
    assert len(spawned) == 3
    assert run.counters.claimed == 3
