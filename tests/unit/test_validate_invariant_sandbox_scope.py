"""Unit tests for :func:`eawf.kernel.validate.invariants.check_sandbox_policy_scope_ref`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from eawf.kernel.state.models import State
from eawf.kernel.validate.invariants import check_sandbox_policy_scope_ref
from eawf.sandbox.policy import SandboxPolicy


def _base_state() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": None,
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "subproject_id": None,
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


def _policy(pid: str, scope_kind: str, scope_id: str) -> SandboxPolicy:
    return SandboxPolicy(
        id=pid,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        granted_at=datetime(2026, 5, 8, tzinfo=UTC),
    )


def test_no_policies_no_violations() -> None:
    state = State.model_validate(_base_state())
    assert list(check_sandbox_policy_scope_ref(state)) == []


def test_wave_scope_resolved_no_violation() -> None:
    payload = _base_state()
    payload["waves"] = {
        "P00-I01-W01": {
            "id": "P00-I01-W01",
            "iter_id": "P00-I01",
            "title": "w1",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    }
    payload["iters"] = {
        "P00-I01": {
            "id": "P00-I01",
            "phase_id": "P00",
            "title": "i1",
            "status": "active",
            "wave_ids": ["P00-I01-W01"],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    }
    payload["phases"] = {
        "P00": {
            "id": "P00",
            "scope_id": "ZZ",
            "subproject_id": None,
            "title": "p",
            "status": "active",
            "iter_ids": ["P00-I01"],
            "outcome_ids": [],
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "audit_id": None,
        }
    }
    payload["sandbox_policies"] = {
        "POL-1": _policy("POL-1", "wave", "P00-I01-W01").model_dump(mode="json"),
    }
    state = State.model_validate(payload)
    assert list(check_sandbox_policy_scope_ref(state)) == []


def test_wave_scope_dangling_emits_violation() -> None:
    payload = _base_state()
    payload["sandbox_policies"] = {
        "POL-X": _policy("POL-X", "wave", "P00-I01-W99").model_dump(mode="json"),
    }
    state = State.model_validate(payload)
    violations = list(check_sandbox_policy_scope_ref(state))
    assert len(violations) == 1
    assert violations[0].code == "INV.REF.SANDBOX_POLICY_SCOPE_MISSING"
    assert "P00-I01-W99" in violations[0].message


def test_global_and_profile_scopes_pass_without_wave_lookup() -> None:
    payload = _base_state()
    payload["sandbox_policies"] = {
        "POL-G": _policy("POL-G", "global", "global").model_dump(mode="json"),
        "POL-P": _policy("POL-P", "profile", "research").model_dump(mode="json"),
    }
    state = State.model_validate(payload)
    assert list(check_sandbox_policy_scope_ref(state)) == []
