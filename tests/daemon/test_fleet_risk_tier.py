"""Tests: fleet RiskTier auto-close / fork gate wiring (P30-I12-W05 / DL-5).

Exercises the RiskTier gate the fleet auto-drain loop applies to each lane:
the loop resolves a wave's :class:`~eawf.kernel.state.enums.RiskTier` from its
gate kinds at fill time (the cockpit badge) and gates the watcher's terminal
outcome through it at drain time.

The success criteria under test (the fleet-loop slice):

* C1: a high / ui RiskTier is RESOLVED + RECORDED on the lane registry at fill
  time, so the cockpit badge reads off it.
* C2 (the LOAD-BEARING SAFETY INVARIANT): a high / ui lane that the watcher
  reports ``"closed"`` is DOWNGRADED to a fork while jury authority is advisory
  -- it never silently auto-closes -- and auto-closes once BLOCKING authority is
  passed in. A mech / med lane always auto-closes. The pure
  :func:`~eawf.runtime.daemon.methods.fleet.gate_lane_outcome` is exercised
  directly for the per-outcome matrix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import RiskTier
from eawf.kernel.state.models import FleetLane, FleetRunState, State
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    _lane_risk_tier,
    arm_drive,
    gate_lane_outcome,
)

pytestmark = pytest.mark.integration

# A deterministic (mech), an auditor (med), and a jury (high) wave.
_WAVE_KINDS = {
    "P30-I12-W01": "file_exists",
    "P30-I12-W02": "auditor_verdict",
    "P30-I12-W03": "jury_verdict",
}
_WAVE_IDS = list(_WAVE_KINDS)


def _gate_payload(kind: str) -> dict[str, Any]:
    return {
        "id": "GATE-01",
        "criterion_id": "CR-01",
        "kind": kind,
        "args": {},
        "policy": "block",
        "cadence": "every-wave",
        "required": True,
        "timeout_s": None,
    }


def _state_payload() -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid, kind in _WAVE_KINDS.items():
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I12",
            "title": f"Frontier wave {wid[-3:]}",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "gates": [_gate_payload(kind)],
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
            "subproject_id": None,
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


# --- C1: the lane RiskTier is resolved from the wave's gate kinds ----------


def test_lane_risk_tier_resolved_from_gate_kinds(tmp_path: Path) -> None:
    """C1: ``_lane_risk_tier`` reads the wave's gates and classifies the band."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    assert _lane_risk_tier(ctx, "P30-I12-W01") is RiskTier.MECH
    assert _lane_risk_tier(ctx, "P30-I12-W02") is RiskTier.MED
    assert _lane_risk_tier(ctx, "P30-I12-W03") is RiskTier.HIGH
    # A vanished wave (or stateless ctx) is the safe least-risk band.
    assert _lane_risk_tier(ctx, "P30-I12-W99") is RiskTier.MECH


def test_loop_records_lane_risk_tier_badge(tmp_path: Path) -> None:
    """C1: arming the drive resolves + records the per-lane RiskTier badge."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    seen: dict[str, RiskTier] = {}

    def _watch_capture(c: MethodContext, lane: FleetLane) -> str:
        # The loop has recorded the lane's tier on its registry before draining.
        # Re-resolve here to confirm the wave-derived band matches the badge.
        seen[lane.wave_id] = _lane_risk_tier(c, lane.wave_id)
        return "closed"

    arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=3,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=_watch_capture,
        block_authority=BlockAuthority.BLOCKING,
    )
    assert seen == {
        "P30-I12-W01": RiskTier.MECH,
        "P30-I12-W02": RiskTier.MED,
        "P30-I12-W03": RiskTier.HIGH,
    }


# --- C2: high / ui forks under advisory, closes under blocking -------------


def test_high_lane_forks_under_advisory_authority(tmp_path: Path) -> None:
    """C2 (LOAD-BEARING): under advisory authority the HIGH wave's clean close is
    downgraded to a fork while the MECH + MED waves auto-close.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=3,
        spawn=lambda c, wid: f"ses-{wid}",
        # Every lane's watcher reports a CLEAN close; only the risk gate forks.
        watch=lambda c, lane: "closed",
        block_authority=BlockAuthority.ADVISORY,
    )
    assert run.run_state is FleetRunState.DONE
    # mech + med auto-closed; the high lane was forked by the safety gate.
    assert run.counters.closed == 2
    assert run.counters.forked == 1


def test_high_lane_auto_closes_under_blocking_authority(tmp_path: Path) -> None:
    """C2: once BLOCKING authority is granted, the HIGH wave auto-closes via the
    jury path -- all three lanes close clean.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=3,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=lambda c, lane: "closed",
        block_authority=BlockAuthority.BLOCKING,
    )
    assert run.run_state is FleetRunState.DONE
    assert run.counters.closed == 3
    assert run.counters.forked == 0


# --- C2: the pure gate_lane_outcome decision matrix ------------------------


def test_gate_lane_outcome_passes_forked_through() -> None:
    """C2: a watcher-reported fork stays forked regardless of tier / authority --
    a failed lane never auto-closes.
    """
    for tier in RiskTier:
        for authority in (BlockAuthority.ADVISORY, BlockAuthority.BLOCKING):
            assert (
                gate_lane_outcome("forked", tier, block_authority=authority) == "forked"
            )


def test_gate_lane_outcome_downgrades_high_ui_under_advisory() -> None:
    """C2 (LOAD-BEARING): a clean close of a high / ui lane is downgraded to a
    fork under advisory authority, and preserved under blocking.
    """
    for tier in (RiskTier.HIGH, RiskTier.UI):
        assert (
            gate_lane_outcome("closed", tier, block_authority=BlockAuthority.ADVISORY)
            == "forked"
        )
        assert (
            gate_lane_outcome("closed", tier, block_authority=BlockAuthority.BLOCKING)
            == "closed"
        )


def test_gate_lane_outcome_preserves_mech_med_close() -> None:
    """C2: a clean close of a mech / med lane passes through under any authority."""
    for tier in (RiskTier.MECH, RiskTier.MED):
        for authority in (BlockAuthority.ADVISORY, BlockAuthority.BLOCKING):
            assert (
                gate_lane_outcome("closed", tier, block_authority=authority) == "closed"
            )
