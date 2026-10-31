"""Unit tests for :func:`eawf.runtime.mcp.book.owned_registry` on an epoch-1 tree."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.mcp.book import owned_registry

pytestmark = pytest.mark.integration


def _server(server_id: str, owner: str = "eawf") -> dict[str, Any]:
    return {
        "id": server_id,
        "owner": owner,
        "command": "mcp-server",
        "args": [],
        "env_refs": [],
        "risk": "read",
        "write_capable": False,
        "status": "configured",
        "installed_targets": [],
    }


def _seed(workspace: Path, servers: dict[str, dict[str, Any]] | None) -> Path:
    ea = workspace / ".ea"
    ea.mkdir(parents=True)
    payload: dict[str, Any] = {
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
        "mcp_servers": servers,
    }
    state_path = ea / "state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path


def test_a_tree_without_state_holds_nothing(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    assert owned_registry(tmp_path / ".ea" / "state.json") == ([], [])


def test_a_frozen_document_without_servers_holds_nothing(tmp_path: Path) -> None:
    assert owned_registry(_seed(tmp_path, None)) == ([], [])
