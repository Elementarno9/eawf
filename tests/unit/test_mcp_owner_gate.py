"""Owner-gate unit tests for ``eawf mcp add/update/remove``.

The CLI handler must refuse to ``update`` or ``remove`` an entry whose
``state.mcp_servers[id].owner`` is anything other than ``"eawf"``.
The strict invariant ``INV.OWNER.MCP_NON_EAWF`` already catches
non-eawf owners at validation time, but this duplication is
defence-in-depth: a future relaxation of the invariant must not
silently let the CLI mutate user-owned rows.

We exercise the CLI through Typer's :class:`CliRunner` against a
seeded ``state.json``. Direct invariant bypass is achieved by
writing the user-owned state row via raw JSON (the model itself
permits any owner string; only the cross-cutting validator rejects
non-eawf).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def _seed_state(tmp_path: Path, *, mcp_servers: dict[str, dict[str, object]]) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    body: dict[str, object] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
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
        "mcp_servers": mcp_servers,
    }
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return state_path


@pytest.fixture
def tmp_state_with_user_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a state.json holding a single user-owned MCP entry.

    The entry violates ``INV.OWNER.MCP_NON_EAWF``; we patch the
    invariants module to skip that check so the CLI can load the
    file and exercise the owner-gate path.
    """
    state_path = _seed_state(
        tmp_path,
        mcp_servers={
            "manual-mcp": {
                "id": "manual-mcp",
                "owner": "user",
                "command": "/usr/bin/manual",
                "args": [],
                "env_refs": [],
                "risk": "read",
                "write_capable": False,
                "status": "configured",
                "installed_targets": [],
            }
        },
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    # Disable INV.OWNER.MCP_NON_EAWF so the load succeeds. We still
    # exercise the owner-gate guard *inside* the CLI handler.
    from eawf.validate import invariants

    monkeypatch.setattr(
        invariants,
        "check_mcp_plugin_owners",
        lambda state: iter(()),  # type: ignore[arg-type]
    )
    return state_path


@pytest.fixture
def tmp_state_with_eawf_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = _seed_state(
        tmp_path,
        mcp_servers={
            "ours": {
                "id": "ours",
                "owner": "eawf",
                "command": "/usr/bin/ours",
                "args": [],
                "env_refs": [],
                "risk": "read",
                "write_capable": False,
                "status": "configured",
                "installed_targets": [],
            }
        },
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def test_update_refuses_user_owned_entry(tmp_state_with_user_mcp: Path) -> None:
    result = runner.invoke(
        app,
        ["mcp", "update", "manual-mcp", "--command", "/should-not-apply"],
    )
    assert result.exit_code == 1, result.output  # INVALID_INPUT


def test_remove_refuses_user_owned_entry(tmp_state_with_user_mcp: Path) -> None:
    result = runner.invoke(app, ["mcp", "remove", "manual-mcp"])
    assert result.exit_code == 1, result.output


def test_add_rejects_collision_without_force(tmp_state_with_eawf_mcp: Path) -> None:
    result = runner.invoke(
        app,
        ["mcp", "add", "ours", "--command", "/dup"],
    )
    assert result.exit_code == 1, result.output


def test_add_force_redefines_existing_eawf_entry(tmp_state_with_eawf_mcp: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--json",
            "mcp",
            "add",
            "ours",
            "--command",
            "/replacement",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "/replacement"
