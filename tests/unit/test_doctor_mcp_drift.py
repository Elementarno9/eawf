"""Unit tests for the ``check_mcp_drift`` doctor check (P14-W08 / B062)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    McpRisk,
    McpStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    McpServer,
    Project,
    State,
)
from eawf.observability.doctor.checks import check_config_resolves, check_mcp_drift, run_all


def _seed_state(workspace: Path, *, servers: dict[str, McpServer] | None = None) -> Path:
    """Write a minimal state.json under <workspace>/.ea/ for the drift check."""
    ea = workspace / ".ea"
    ea.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:DEMO",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="DEMO",
                slug="demo",
                title="Demo",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:DEMO",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="DEMO").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    if servers:
        state.mcp_servers = servers
    payload = state.model_dump(mode="json")
    (ea / "state.json").write_text(json.dumps(payload, sort_keys=True) + "\n")
    return ea / "state.json"


def _server(server_id: str) -> McpServer:
    return McpServer(
        id=server_id,
        owner="eawf",
        command="mcp-server",
        args=[],
        env_refs=[],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


def test_check_returns_ok_when_no_workspace() -> None:
    result = check_mcp_drift(workspace=None)
    assert result.status == "ok"


def test_check_returns_ok_when_no_state(tmp_path: Path) -> None:
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "ok"


def test_check_returns_ok_when_no_eawf_servers(tmp_path: Path) -> None:
    _seed_state(tmp_path)
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "ok"
    assert "no eawf-owned" in (result.detail or "")


def test_check_warns_when_state_server_missing_from_runtime(tmp_path: Path) -> None:
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "warn"
    assert "missing-from-runtime" in (result.detail or "")


def test_check_returns_ok_when_runtime_has_eawf_entry(tmp_path: Path) -> None:
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text(
        json.dumps({"mcpServers": {"mcp-a": {"command": "mcp-server", "__eawf_owner": "eawf"}}})
    )
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "ok"


def test_check_warns_on_orphan_runtime_entry(tmp_path: Path) -> None:
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp-a": {"command": "mcp-server", "__eawf_owner": "eawf"},
                    "mcp-orphan": {"command": "x", "__eawf_owner": "eawf"},
                }
            }
        )
    )
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "warn"
    assert "orphans" in (result.detail or "")


def test_check_ignores_user_owned_entries(tmp_path: Path) -> None:
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp-a": {"command": "mcp-server", "__eawf_owner": "eawf"},
                    "user-private": {"command": "x"},
                }
            }
        )
    )
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "ok"


def test_run_all_includes_mcp_drift(tmp_path: Path) -> None:
    _seed_state(tmp_path)
    results = run_all(workspace=tmp_path)
    names = [r.name for r in results]
    assert "mcp_drift" in names
    assert len(results) == 19


def test_check_config_resolves_uses_workspace_as_repo_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path | None] = {}

    def _merge_config(
        *, repo: Path | None, workspace: Path | None
    ) -> tuple[dict[str, object], dict[str, str]]:
        captured.update(repo=repo, workspace=workspace)
        return {"profiles": {"enabled": ["core"]}}, {}

    monkeypatch.setattr("eawf.observability.doctor.checks.merge_config", _merge_config)

    result = check_config_resolves(workspace=tmp_path)

    assert result.status == "ok"
    assert captured == {"repo": tmp_path, "workspace": tmp_path}


def test_check_warns_on_unreadable_settings(tmp_path: Path) -> None:
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text("not json")
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "warn"


def test_check_warns_on_content_drift(tmp_path: Path) -> None:
    """An eawf entry whose command diverges from state is content-drift."""
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text(
        json.dumps({"mcpServers": {"mcp-a": {"command": "STALE-COMMAND", "__eawf_owner": "eawf"}}})
    )
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "warn"
    assert "content-drift" in (result.detail or "")


def test_check_ok_when_codex_toml_matches(tmp_path: Path) -> None:
    """A grant-matched eawf table in .codex/config.toml satisfies the check."""
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '[mcp_servers."mcp-a"]\n'
        'command = "mcp-server"\n'
        "args = []\n"
        "env = {}\n"
        '__eawf_owner = "eawf"\n'
    )
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "ok"


def test_check_warns_on_codex_orphan(tmp_path: Path) -> None:
    """An eawf table in codex config with no state row is an orphan."""
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    claude = tmp_path / ".mcp.json"
    claude.parent.mkdir(exist_ok=True)
    claude.write_text(
        json.dumps({"mcpServers": {"mcp-a": {"command": "mcp-server", "__eawf_owner": "eawf"}}})
    )
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers."ghost"]\ncommand = "x"\n__eawf_owner = "eawf"\n')
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "warn"
    assert "codex:ghost" in (result.detail or "")


def test_check_warns_on_unreadable_codex_toml(tmp_path: Path) -> None:
    _seed_state(tmp_path, servers={"mcp-a": _server("mcp-a")})
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("this is = = not toml")
    result = check_mcp_drift(workspace=tmp_path)
    assert result.status == "warn"
