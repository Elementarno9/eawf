"""Unit tests for :func:`eawf.validate.invariants.check_mcp_grant_server_ref`.

Boundary (clean state) + error (dangling server_id) coverage; also verifies
the invariant is registered in :data:`ALL_INVARIANTS` so it runs under
``eawf validate`` and via :func:`state_transaction`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from eawf.state.models import State
from eawf.validate.invariants import (
    ALL_INVARIANTS,
    Violation,
    check_mcp_grant_server_ref,
)

pytestmark = pytest.mark.unit


def _base_state_payload() -> dict[str, Any]:
    """Minimal repo-scoped payload that passes every invariant."""
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


def _server(server_id: str) -> dict[str, Any]:
    return {
        "id": server_id,
        "owner": "eawf",
        "command": "/usr/local/bin/mcp",
        "args": [],
        "env_refs": [],
        "risk": "read",
        "write_capable": False,
        "status": "configured",
        "installed_targets": [],
    }


def _grant(
    grant_id: str = "GRANT-1",
    *,
    server_id: str = "filesystem",
    scope_kind: str = "wave",
    scope_id: str = "P10-I01-W04",
) -> dict[str, Any]:
    return {
        "id": grant_id,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "server_id": server_id,
        "granted_at": "2026-05-08T00:00:00Z",
    }


def _codes(violations: Iterable[Violation]) -> set[str]:
    return {v.code for v in violations}


def test_check_mcp_grant_server_ref_no_violations_when_mcp_grants_absent() -> None:
    """When ``mcp_grants`` is None, the check is a no-op."""
    payload = _base_state_payload()
    state = State.model_validate(payload)
    assert state.mcp_grants is None
    assert list(check_mcp_grant_server_ref(state)) == []


def test_check_mcp_grant_server_ref_no_violations_when_server_present() -> None:
    payload = _base_state_payload()
    payload["mcp_servers"] = {"filesystem": _server("filesystem")}
    payload["mcp_grants"] = {"GRANT-1": _grant(server_id="filesystem")}
    state = State.model_validate(payload)
    assert list(check_mcp_grant_server_ref(state)) == []


def test_check_mcp_grant_server_ref_flags_dangling_server_id() -> None:
    payload = _base_state_payload()
    payload["mcp_servers"] = {"filesystem": _server("filesystem")}
    payload["mcp_grants"] = {"GRANT-1": _grant(server_id="ghost")}
    state = State.model_validate(payload)
    violations = list(check_mcp_grant_server_ref(state))
    assert len(violations) == 1
    v = violations[0]
    assert v.code == "INV.REF.MCP_GRANT_SERVER_MISSING"
    assert v.path == "/mcp_grants/GRANT-1/server_id"
    assert "ghost" in v.message
    assert "GRANT-1" in v.message


def test_check_mcp_grant_server_ref_flags_when_mcp_servers_absent() -> None:
    """A grant with no server table at all also dangles."""
    payload = _base_state_payload()
    payload["mcp_grants"] = {"GRANT-1": _grant(server_id="filesystem")}
    state = State.model_validate(payload)
    codes = _codes(check_mcp_grant_server_ref(state))
    assert "INV.REF.MCP_GRANT_SERVER_MISSING" in codes


def test_check_mcp_grant_server_ref_flags_each_dangling_grant_independently() -> None:
    payload = _base_state_payload()
    payload["mcp_servers"] = {"fs": _server("fs")}
    payload["mcp_grants"] = {
        "GRANT-1": _grant("GRANT-1", server_id="fs"),
        "GRANT-2": _grant("GRANT-2", server_id="missing-a"),
        "GRANT-3": _grant("GRANT-3", server_id="missing-b"),
    }
    state = State.model_validate(payload)
    violations = list(check_mcp_grant_server_ref(state))
    assert len(violations) == 2
    paths = {v.path for v in violations}
    assert paths == {
        "/mcp_grants/GRANT-2/server_id",
        "/mcp_grants/GRANT-3/server_id",
    }


def test_check_mcp_grant_server_ref_is_registered_in_all_invariants() -> None:
    """The invariant must run via ``eawf validate`` / ``state_transaction``."""
    names = {fn.__name__ for fn in ALL_INVARIANTS}
    assert "check_mcp_grant_server_ref" in names
