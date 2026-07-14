"""Unit tests for cross-entity invariants in :mod:`eawf.kernel.validate.invariants`.

Each invariant function gets at minimum one happy-path test (no violations)
and one negative test (a State constructed to trigger that exact code).
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.kernel.validate.invariants import (
    ALL_INVARIANTS,
    Violation,
    check_audit_evidence,
    check_closure_rules,
    check_closure_timestamps,
    check_current_pointers,
    check_mcp_plugin_owners,
    check_parent_ids,
    check_plugin_owners,
    check_plugin_runtimes,
    check_scope_consistency,
    check_wave_blocks_invariant,
)
from eawf.kernel.validate.strict import validate_state

# ---- Fixture builders -------------------------------------------------------


def _base_state_payload() -> dict[str, Any]:
    """Return a minimal repo-scoped payload that passes every invariant."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _phase(phase_id: str, *, status: str = "active") -> dict[str, Any]:
    return {
        "id": phase_id,
        "scope_id": "QR",
        "title": f"Phase {phase_id}",
        "status": status,
        "iter_ids": [],
        "outcome_ids": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T01:00:00Z" if status == "closed" else None,
        "audit_id": None,
    }


def _iter(iter_id: str, *, phase_id: str, status: str = "active") -> dict[str, Any]:
    return {
        "id": iter_id,
        "phase_id": phase_id,
        "title": f"Iter {iter_id}",
        "status": status,
        "wave_ids": [],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
    }


def _wave(wave_id: str, *, iter_id: str, status: str = "in_progress") -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": f"Wave {wave_id}",
        "status": status,
        "deps": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "outcome": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
    }


def _codes(violations: Iterable[Violation]) -> set[str]:
    return {v.code for v in violations}


# ---- check_parent_ids -------------------------------------------------------


def test_check_parent_ids_no_violations_on_consistent_state() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01")
    state = State.model_validate(payload)
    assert list(check_parent_ids(state)) == []


def test_check_parent_ids_flags_iter_with_missing_phase() -> None:
    payload = _base_state_payload()
    payload["iters"]["P99-I01"] = _iter("P99-I01", phase_id="P99")
    state = State.model_validate(payload)
    codes = _codes(check_parent_ids(state))
    assert "INV.PARENT.ITER_PHASE_MISSING" in codes


def test_check_parent_ids_flags_iter_id_phase_mismatch() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["phases"]["P02"] = _phase("P02")
    # iter id encodes P01, but phase_id field says P02
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P02")
    state = State.model_validate(payload)
    codes = _codes(check_parent_ids(state))
    assert "INV.PARENT.ITER_ID_MISMATCH" in codes


def test_check_parent_ids_flags_wave_with_missing_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01")
    state = State.model_validate(payload)
    codes = _codes(check_parent_ids(state))
    assert "INV.PARENT.WAVE_ITER_MISSING" in codes


def test_check_parent_ids_flags_wave_id_iter_mismatch() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["phases"]["P02"] = _phase("P02")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["iters"]["P02-I05"] = _iter("P02-I05", phase_id="P02")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P02-I05")
    state = State.model_validate(payload)
    codes = _codes(check_parent_ids(state))
    assert "INV.PARENT.WAVE_ID_MISMATCH" in codes


# ---- check_current_pointers -------------------------------------------------


def test_check_current_pointers_no_violations_when_pointers_null() -> None:
    payload = _base_state_payload()
    state = State.model_validate(payload)
    assert list(check_current_pointers(state)) == []


def test_check_current_pointers_flags_pointer_to_closed_phase() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["current"]["phase_id"] = "P01"
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.PHASE_NOT_OPEN" in codes


def test_check_current_pointers_flags_pointer_to_missing_phase() -> None:
    payload = _base_state_payload()
    payload["current"]["phase_id"] = "P03"
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.PHASE_MISSING" in codes


def test_check_current_pointers_flags_active_wave_with_pending_status() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01", status="pending")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    payload["current"]["active_wave_ids"] = ["P01-I01-W01"]
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.WAVE_NOT_ACTIVE" in codes


def test_check_current_pointers_flags_pointer_to_closed_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="closed")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.ITER_NOT_OPEN" in codes


def test_check_current_pointers_no_violation_when_iter_phase_matches() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.ITER_PHASE_MISMATCH" not in codes


def test_check_current_pointers_flags_iter_phase_mismatch() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["phases"]["P02"] = _phase("P02")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["iters"]["P02-I01"] = _iter("P02-I01", phase_id="P02")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P02-I01"
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.ITER_PHASE_MISMATCH" in codes


# ---- check_current_pointers: orphan-active iter (LC-6) ----------------------


def test_check_current_pointers_flags_orphan_active_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = None
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.CURRENT.ITER_ORPHAN_ACTIVE" in codes


def test_check_current_pointers_clean_when_current_iter_set() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    state = State.model_validate(payload)
    assert "INV.CURRENT.ITER_ORPHAN_ACTIVE" not in _codes(check_current_pointers(state))


def test_validate_state_flags_orphan_active_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = None
    report = validate_state(payload)
    codes = {v.code for v in report.violations}
    assert "INV.CURRENT.ITER_ORPHAN_ACTIVE" in codes


# ---- check_current_pointers: multi-active iter (LC-6) -----------------------


def test_check_current_pointers_flags_two_active_iters_under_one_phase() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["iters"]["P01-I02"] = _iter("P01-I02", phase_id="P01")
    state = State.model_validate(payload)
    codes = _codes(check_current_pointers(state))
    assert "INV.LIFECYCLE.MULTI_ACTIVE_ITER" in codes


def test_check_current_pointers_clean_single_active_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["iters"]["P01-I02"] = _iter("P01-I02", phase_id="P01", status="planned")
    state = State.model_validate(payload)
    assert "INV.LIFECYCLE.MULTI_ACTIVE_ITER" not in _codes(check_current_pointers(state))


def test_validate_state_flags_multi_active_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["iters"]["P01-I02"] = _iter("P01-I02", phase_id="P01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    report = validate_state(payload)
    codes = {v.code for v in report.violations}
    assert "INV.LIFECYCLE.MULTI_ACTIVE_ITER" in codes


# ---- check_closure_rules ----------------------------------------------------


def test_check_closure_rules_no_violations_when_closed_parent_has_closed_children() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="closed")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01", status="closed")
    state = State.model_validate(payload)
    assert list(check_closure_rules(state)) == []


def test_check_closure_rules_flags_closed_phase_with_open_iter() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="active")
    state = State.model_validate(payload)
    codes = _codes(check_closure_rules(state))
    assert "INV.CLOSURE.PHASE_HAS_OPEN_ITER" in codes


def test_check_closure_rules_flags_closed_iter_with_open_wave() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01", status="closed")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01", status="in_progress")
    state = State.model_validate(payload)
    codes = _codes(check_closure_rules(state))
    assert "INV.CLOSURE.ITER_HAS_OPEN_WAVE" in codes


# ---- check_audit_evidence ---------------------------------------------------


def test_check_audit_evidence_no_violations_when_outcomes_pending() -> None:
    payload = _base_state_payload()
    payload["outcomes"] = {
        "OUT-001": {
            "id": "OUT-001",
            "scope_id": "QR",
            "metric": "latency_ms",
            "threshold": 100.0,
            "direction": "max",
            "value": None,
            "status": "pending",
            "audit_id": None,
            "updated_at": "2026-05-08T00:00:00Z",
        }
    }
    state = State.model_validate(payload)
    assert list(check_audit_evidence(state)) == []


def test_check_audit_evidence_flags_met_outcome_without_audit() -> None:
    payload = _base_state_payload()
    payload["outcomes"] = {
        "OUT-001": {
            "id": "OUT-001",
            "scope_id": "QR",
            "metric": "latency_ms",
            "threshold": 100.0,
            "direction": "max",
            "value": 80.0,
            "status": "met",
            "audit_id": None,
            "updated_at": "2026-05-08T00:00:00Z",
        }
    }
    state = State.model_validate(payload)
    codes = _codes(check_audit_evidence(state))
    assert "INV.AUDIT.OUTCOME_MISSING_AUDIT" in codes


def test_check_audit_evidence_flags_resolved_hypothesis_without_audit() -> None:
    payload = _base_state_payload()
    payload["hypotheses"] = {
        "H03-12": {
            "id": "H03-12",
            "scope_id": "QR",
            "title": "Latency below 100ms improves UX.",
            "metric": "p99_latency_ms",
            "confirm": "< 100",
            "reject": ">= 200",
            "status": "confirmed",
            "verdict": "confirmed",
            "audit_id": None,
            "source_artifact_id": None,
        }
    }
    state = State.model_validate(payload)
    codes = _codes(check_audit_evidence(state))
    assert "INV.AUDIT.HYPOTHESIS_MISSING_AUDIT" in codes


def test_check_audit_evidence_passes_when_audit_id_present() -> None:
    payload = _base_state_payload()
    payload["outcomes"] = {
        "OUT-001": {
            "id": "OUT-001",
            "scope_id": "QR",
            "metric": "latency_ms",
            "threshold": 100.0,
            "direction": "max",
            "value": 80.0,
            "status": "met",
            "audit_id": "AUD-001",
            "updated_at": "2026-05-08T00:00:00Z",
        }
    }
    state = State.model_validate(payload)
    assert list(check_audit_evidence(state)) == []


# ---- check_mcp_plugin_owners ------------------------------------------------


def test_check_mcp_plugin_owners_no_violations_when_eawf_owned() -> None:
    payload = _base_state_payload()
    payload["mcp_servers"] = {
        "filesystem": {
            "id": "filesystem",
            "owner": "eawf",
            "command": "/usr/local/bin/mcp-fs",
            "args": [],
            "env_refs": [],
            "risk": "read",
            "write_capable": False,
            "status": "configured",
            "installed_targets": [],
        }
    }
    state = State.model_validate(payload)
    assert list(check_mcp_plugin_owners(state)) == []


def test_check_mcp_plugin_owners_flags_user_owned_server() -> None:
    payload = _base_state_payload()
    payload["mcp_servers"] = {
        "filesystem": {
            "id": "filesystem",
            "owner": "user",
            "command": "/usr/local/bin/mcp-fs",
            "args": [],
            "env_refs": [],
            "risk": "read",
            "write_capable": False,
            "status": "configured",
            "installed_targets": [],
        }
    }
    state = State.model_validate(payload)
    codes = _codes(check_mcp_plugin_owners(state))
    assert "INV.OWNER.MCP_NON_EAWF" in codes


def test_check_mcp_plugin_owners_skips_when_servers_dict_absent() -> None:
    """When the optional ``mcp_servers`` field is None, the check is a no-op."""
    payload = _base_state_payload()
    state = State.model_validate(payload)
    assert state.mcp_servers is None
    assert list(check_mcp_plugin_owners(state)) == []


# ---- check_plugin_runtimes --------------------------------------------------


def _plugin(pid: str, *, runtime: str = "claude") -> dict[str, Any]:
    return {
        "id": pid,
        "owner": "eawf",
        "runtime": runtime,
        "scope_id": "user",
        "target_path": f"~/.config/{pid}",
        "status": "installed",
        "managed_files": [],
        "installed_at": "2026-05-08T00:00:00Z",
        "updated_at": "2026-05-08T00:00:00Z",
    }


def test_check_plugin_runtimes_passes_when_runtime_is_claude() -> None:
    payload = _base_state_payload()
    payload["plugins"]["claude"] = _plugin("claude", runtime="claude")
    state = State.model_validate(payload)
    assert list(check_plugin_runtimes(state)) == []


def test_check_plugin_runtimes_flags_non_claude_runtime() -> None:
    payload = _base_state_payload()
    payload["plugins"]["opencode"] = _plugin("opencode", runtime="opencode")
    state = State.model_validate(payload)
    codes = _codes(check_plugin_runtimes(state))
    assert "INV.OWNER.PLUGIN_NON_CLAUDE" in codes


# ---- check_scope_consistency -----------------------------------------------


def test_check_scope_consistency_passes_for_repo_with_project() -> None:
    payload = _base_state_payload()
    state = State.model_validate(payload)
    assert list(check_scope_consistency(state)) == []


def test_check_scope_consistency_flags_repo_without_project() -> None:
    payload = _base_state_payload()
    payload["project"] = None
    payload["current"]["project_code"] = None
    state = State.model_validate(payload)
    codes = _codes(check_scope_consistency(state))
    assert "INV.SCOPE.REPO_REQUIRES_PROJECT" in codes


def test_check_scope_consistency_passes_for_workspace_with_index() -> None:
    payload = _base_state_payload()
    payload["scope_kind"] = "workspace"
    payload["urn"] = "urn:eawf:v1:workspace:MAIN"
    payload["project"] = None
    payload["current"]["project_code"] = None
    payload["workspace"] = {
        "code": "MAIN",
        "title": "Main",
        "repos": {},
        "current_repo_code": None,
    }
    state = State.model_validate(payload)
    assert list(check_scope_consistency(state)) == []


def test_check_scope_consistency_flags_workspace_without_index() -> None:
    payload = _base_state_payload()
    payload["scope_kind"] = "workspace"
    payload["urn"] = "urn:eawf:v1:workspace:MAIN"
    payload["project"] = None
    payload["current"]["project_code"] = None
    payload["workspace"] = None
    state = State.model_validate(payload)
    codes = _codes(check_scope_consistency(state))
    assert "INV.SCOPE.WORKSPACE_REQUIRES_INDEX" in codes


def test_check_scope_consistency_flags_workspace_with_embedded_project() -> None:
    payload = _base_state_payload()
    payload["scope_kind"] = "workspace"
    payload["urn"] = "urn:eawf:v1:workspace:MAIN"
    payload["workspace"] = {
        "code": "MAIN",
        "title": "Main",
        "repos": {},
        "current_repo_code": None,
    }
    # project still embedded -- should trigger WORKSPACE_NO_PROJECT
    state = State.model_validate(payload)
    codes = _codes(check_scope_consistency(state))
    assert "INV.SCOPE.WORKSPACE_NO_PROJECT" in codes


# ---- check_plugin_owners ----------------------------------------------------


def test_check_plugin_owners_passes_when_eawf() -> None:
    payload = _base_state_payload()
    payload["plugins"]["claude"] = _plugin("claude")
    state = State.model_validate(payload)
    assert list(check_plugin_owners(state)) == []


def test_check_plugin_owners_flags_user_owned_plugin() -> None:
    payload = _base_state_payload()
    plugin = _plugin("claude")
    plugin["owner"] = "user"
    payload["plugins"]["claude"] = plugin
    state = State.model_validate(payload)
    codes = _codes(check_plugin_owners(state))
    assert "INV.OWNER.PLUGIN_NON_EAWF" in codes


# ---- check_wave_blocks_invariant -------------------------------------------


def _wave_with_graph(
    wave_id: str,
    *,
    iter_id: str,
    deps: list[str] | None = None,
    blocks: list[str] | None = None,
) -> dict[str, Any]:
    payload = _wave(wave_id, iter_id=iter_id)
    payload["deps"] = list(deps or [])
    payload["blocks"] = list(blocks or [])
    return payload


def test_check_wave_blocks_invariant_no_violations_when_indices_consistent() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["waves"]["P01-I01-W01"] = _wave_with_graph(
        "P01-I01-W01", iter_id="P01-I01", blocks=["P01-I01-W02"]
    )
    payload["waves"]["P01-I01-W02"] = _wave_with_graph(
        "P01-I01-W02", iter_id="P01-I01", deps=["P01-I01-W01"]
    )
    state = State.model_validate(payload)
    assert list(check_wave_blocks_invariant(state)) == []


def test_check_wave_blocks_invariant_flags_missing_reverse_block() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    # W02 deps on W01 but W01.blocks does NOT include W02 → reverse missing.
    payload["waves"]["P01-I01-W01"] = _wave_with_graph("P01-I01-W01", iter_id="P01-I01")
    payload["waves"]["P01-I01-W02"] = _wave_with_graph(
        "P01-I01-W02", iter_id="P01-I01", deps=["P01-I01-W01"]
    )
    state = State.model_validate(payload)
    codes = _codes(check_wave_blocks_invariant(state))
    assert "INV.GRAPH.BLOCKS_MISSING_REVERSE" in codes


def test_check_wave_blocks_invariant_flags_missing_reverse_dep() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    # W01.blocks lists W02 but W02.deps does NOT include W01.
    payload["waves"]["P01-I01-W01"] = _wave_with_graph(
        "P01-I01-W01", iter_id="P01-I01", blocks=["P01-I01-W02"]
    )
    payload["waves"]["P01-I01-W02"] = _wave_with_graph("P01-I01-W02", iter_id="P01-I01")
    state = State.model_validate(payload)
    codes = _codes(check_wave_blocks_invariant(state))
    assert "INV.GRAPH.DEPS_MISSING_REVERSE" in codes


def test_check_wave_blocks_invariant_skips_dangling_peer_silently() -> None:
    """Dangling refs are flagged by check_parent_ids; this invariant ignores them."""
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    # W01 deps on a wave that does not exist in state — no graph violation.
    payload["waves"]["P01-I01-W01"] = _wave_with_graph(
        "P01-I01-W01", iter_id="P01-I01", deps=["P01-I01-W99"]
    )
    state = State.model_validate(payload)
    assert list(check_wave_blocks_invariant(state)) == []


# ---- ALL_INVARIANTS aggregate ----------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "check_parent_ids",
        "check_current_pointers",
        "check_closure_rules",
        "check_closure_timestamps",
        "check_audit_evidence",
        "check_mcp_plugin_owners",
        "check_plugin_runtimes",
        "check_scope_consistency",
        "check_plugin_owners",
        "check_wave_blocks_invariant",
    ],
)
def test_all_invariants_includes_each_named_check(name: str) -> None:
    fn_names = {fn.__name__ for fn in ALL_INVARIANTS}
    assert name in fn_names


def test_all_invariants_returns_no_violations_on_clean_state() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01")
    payload["iters"]["P01-I01"] = _iter("P01-I01", phase_id="P01")
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01")
    payload["current"]["phase_id"] = "P01"
    payload["current"]["iter_id"] = "P01-I01"
    payload["current"]["active_wave_ids"] = ["P01-I01-W01"]
    state = State.model_validate(payload)
    aggregated = [v for inv in ALL_INVARIANTS for v in inv(state)]
    assert aggregated == []


def test_violation_dataclass_is_frozen() -> None:
    v = Violation(code="X.Y.Z", path="/foo", message="bar")
    with pytest.raises((AttributeError, TypeError)):
        v.code = "OTHER"  # type: ignore[misc]


def test_base_state_payload_is_independent_per_call() -> None:
    """Sanity check: helper returns fresh dicts so mutations don't leak."""
    a = _base_state_payload()
    b = _base_state_payload()
    a["phases"]["P01"] = _phase("P01")
    assert "P01" not in b["phases"]
    # Also confirm deepcopy contract for nested dicts callers rely on.
    c = deepcopy(a)
    a["phases"].clear()
    assert "P01" in c["phases"]


# ---- check_closure_timestamps -----------------------------------------------


def test_check_closure_timestamps_flags_closed_phase_without_closed_at() -> None:
    payload = _base_state_payload()
    phase = _phase("P01", status="closed")
    phase["closed_at"] = None  # override builder default
    payload["phases"]["P01"] = phase
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.PHASE_NO_CLOSED_AT" in codes


def test_check_closure_timestamps_flags_closed_wave_without_closed_at() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    closed_iter = _iter("P01-I01", phase_id="P01", status="closed")
    closed_iter["closed_at"] = "2026-05-08T01:00:00Z"
    payload["iters"]["P01-I01"] = closed_iter
    payload["waves"]["P01-I01-W01"] = _wave("P01-I01-W01", iter_id="P01-I01", status="closed")
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.WAVE_NO_CLOSED_AT" in codes


def test_check_closure_timestamps_flags_stale_session_without_ended_at() -> None:
    payload = _base_state_payload()
    payload["agent_sessions"]["S01"] = {
        "id": "S01",
        "role": "executor",
        "runtime": "claude",
        "scope_id": "QR",
        "status": "stale",
        "claimed_wave_ids": [],
        "worktree_ids": [],
        "artifact_ids": [],
        "started_at": "2026-05-08T00:00:00Z",
        "ended_at": None,
        "summary": None,
    }
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.SESSION_NO_ENDED_AT" in codes


def test_check_closure_timestamps_clean_state_yields_no_violations() -> None:
    payload = _base_state_payload()
    closed_phase = _phase("P01", status="closed")
    closed_phase["closed_at"] = "2026-05-08T01:00:00Z"
    payload["phases"]["P01"] = closed_phase
    state = State.model_validate(payload)
    assert list(check_closure_timestamps(state)) == []


def test_check_closure_timestamps_flags_closed_iter_without_closed_at() -> None:
    payload = _base_state_payload()
    payload["phases"]["P01"] = _phase("P01", status="closed")
    closed_iter = _iter("P01-I01", phase_id="P01", status="closed")
    closed_iter["closed_at"] = None  # explicit override; builder leaves it None already
    payload["iters"]["P01-I01"] = closed_iter
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.ITER_NO_CLOSED_AT" in codes


def test_check_closure_timestamps_flags_closed_goal_without_closed_at() -> None:
    payload = _base_state_payload()
    payload["goals"] = {
        "G01": {
            "id": "G01",
            "scope_id": "QR",
            "title": "Reach P99 latency target",
            "summary": "Drive p99 latency below 100ms.",
            "status": "achieved",
            "outcome_ids": [],
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    }
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.GOAL_NO_CLOSED_AT" in codes


def test_check_closure_timestamps_flags_closed_backlog_without_closed_at() -> None:
    payload = _base_state_payload()
    payload["backlog"] = {
        "B01": {
            "id": "B01",
            "scope_id": "QR",
            "title": "Migrate to async storage",
            "priority": "P1",
            "status": "closed",
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "resolution": None,
        }
    }
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.BACKLOG_NO_CLOSED_AT" in codes


def test_check_closure_timestamps_flags_closed_incident_without_closed_at() -> None:
    payload = _base_state_payload()
    payload["incidents"] = {
        "INC-001": {
            "id": "INC-001",
            "scope_id": "QR",
            "severity": "high",
            "title": "Production cache stampede",
            "status": "resolved",
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "root_cause": None,
            "corrective_action_ids": [],
            "report_artifact_id": None,
        }
    }
    state = State.model_validate(payload)
    codes = _codes(check_closure_timestamps(state))
    assert "INV.CLOSURE.INCIDENT_NO_CLOSED_AT" in codes
