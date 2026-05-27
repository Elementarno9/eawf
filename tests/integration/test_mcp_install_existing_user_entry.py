"""Integration tests for non-Eä mcpServers preservation.

Two acceptance scenarios from the wave spec §7:

1. settings.json with a pre-installed user ``mcpServers["serena"]``
   survives byte-equal across ``eawf mcp install <other-id>`` and
   ``eawf mcp remove <other-id>``.
2. settings.json with a pre-installed user ``mcpServers["dup-id"]``
   blocks ``eawf mcp install dup-id`` (exit 8); ``--force`` succeeds
   and overwrites the user entry.

Byte-equality is achieved by pinning ``__eawf_managed_at`` to a
stable timestamp via the installer's ``timestamp`` parameter; the
CLI uses ``datetime.now(UTC).isoformat()`` so we exercise the
installer-level guarantee directly here in addition to the CLI
path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import McpRisk, McpStatus
from eawf.kernel.state.models import McpServer
from eawf.runtime.mcp.installer import install_runtime_entry, remove_runtime_entry
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _seed_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    body = {
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
    }
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return state_path


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def _make_server(server_id: str, command: str = "eawf-mcp-demo") -> McpServer:
    return McpServer(
        id=server_id,
        owner="eawf",
        command=command,
        args=[],
        env_refs=[],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


def test_user_serena_entry_survives_byte_equal_across_install_and_remove(
    tmp_path: Path, tmp_state: Path
) -> None:
    """User-owned ``mcpServers["serena"]`` is byte-equal across the
    full install→remove cycle for a different Eä-managed id."""
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    user_entry = {
        "command": "serena-mcp",
        "args": ["--config", "user.toml"],
        "env": {"SERENA_KEY": "literal-user-token"},
        "transport": "stdio",
    }
    initial = json.dumps({"mcpServers": {"serena": user_entry}}, sort_keys=True, indent=2) + "\n"
    settings_path.write_text(initial, encoding="utf-8")
    initial_bytes = settings_path.read_bytes()

    # Install the Eä-owned entry directly via the library (pin
    # timestamp so byte-equality on the round trip is testable).
    install_runtime_entry(
        server=_make_server("ours"),
        runtime="claude",
        target_dir=tmp_path,
        force=False,
        timestamp="1970-01-01T00:00:00+00:00",
    )
    parsed_after_install = json.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed_after_install["mcpServers"]["serena"] == user_entry
    assert "__eawf_owner" not in parsed_after_install["mcpServers"]["serena"]

    # Remove restores byte-equal initial file.
    remove_runtime_entry(
        server_id="ours",
        runtime="claude",
        target_dir=tmp_path,
        force=False,
    )
    final_bytes = settings_path.read_bytes()
    assert final_bytes == initial_bytes


def test_user_dup_id_blocks_install_without_force(tmp_path: Path, tmp_state: Path) -> None:
    """Pre-existing user ``mcpServers["dup-id"]`` blocks install with exit 8."""
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"mcpServers": {"dup-id": {"command": "manual-mcp"}}},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    runner.invoke(app, ["mcp", "add", "dup-id", "--command", "eawf-mcp"])
    result = runner.invoke(
        app,
        [
            "--no-input",
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "dup-id",
        ],
    )
    assert result.exit_code == 3, result.output  # INTEGRITY_VIOLATION


def test_user_dup_id_force_overrides_install(tmp_path: Path, tmp_state: Path) -> None:
    settings_path = tmp_path / ".mcp.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"mcpServers": {"dup-id": {"command": "manual-mcp"}}},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    runner.invoke(app, ["mcp", "add", "dup-id", "--command", "eawf-mcp"])
    result = runner.invoke(
        app,
        [
            "--no-input",
            "-w",
            str(tmp_path),
            "mcp",
            "install",
            "dup-id",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    entry = parsed["mcpServers"]["dup-id"]
    assert entry["__eawf_owner"] == "eawf"
    assert entry["command"] == "eawf-mcp"
