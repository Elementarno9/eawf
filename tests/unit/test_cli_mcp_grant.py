"""Unit tests for ``eawf mcp grant`` / ``eawf mcp revoke``.

The handlers route through :func:`state_transaction`, which validates both
schema and invariants on commit; this lets us verify the referential
integrity refusal end-to-end (dangling ``server_id`` → exit 4 from
``ValidationFailed``).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def _seed_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    body: dict[str, Any] = {
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


def _add_server(server_id: str = "filesystem") -> None:
    res = runner.invoke(
        app,
        ["mcp", "add", server_id, "--command", "/usr/local/bin/mcp"],
    )
    assert res.exit_code == 0, res.output


def _read_state(state_path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    return parsed


# ---- grant_cmd happy paths --------------------------------------------------


def test_grant_cmd_writes_grant_and_emits_json_envelope(tmp_state: Path) -> None:
    _add_server("filesystem")
    pre_state = _read_state(tmp_state)
    pre_updated_at = pre_state["updated_at"]

    res = runner.invoke(
        app,
        ["--json", "mcp", "grant", "wave", "P10-I01-W04", "filesystem"],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["id"] == "GRANT-1"
    assert payload["scope_kind"] == "wave"
    assert payload["scope_id"] == "P10-I01-W04"
    assert payload["server_id"] == "filesystem"
    assert payload["granted_at"].endswith("+00:00") or payload["granted_at"].endswith("Z")

    state = _read_state(tmp_state)
    grants = state["mcp_grants"]
    assert isinstance(grants, dict)
    grant_row = grants["GRANT-1"]
    assert grant_row["id"] == "GRANT-1"
    assert grant_row["scope_kind"] == "wave"
    assert grant_row["scope_id"] == "P10-I01-W04"
    assert grant_row["server_id"] == "filesystem"
    # The on-disk timestamp is the canonical ISO-8601 UTC form; the CLI
    # envelope normalises the same instant via ``datetime.isoformat`` so
    # the two surfaces may differ in Z vs +00:00 suffix. Comparing parsed
    # datetimes keeps the assertion semantic, not lexical.
    disk_dt = datetime.fromisoformat(grant_row["granted_at"].replace("Z", "+00:00"))
    envelope_dt = datetime.fromisoformat(payload["granted_at"].replace("Z", "+00:00"))
    assert disk_dt == envelope_dt
    # state.updated_at advanced under state_transaction.
    assert state["updated_at"] != pre_updated_at


def test_grant_cmd_text_mode_emits_single_line_summary(tmp_state: Path) -> None:
    _add_server("filesystem")
    res = runner.invoke(
        app,
        ["mcp", "grant", "wave", "P10-I01-W04", "filesystem"],
    )
    assert res.exit_code == 0, res.output
    body = res.output.strip()
    assert "\n" not in body
    assert body.startswith("mcp granted: GRANT-1")
    assert "wave=P10-I01-W04" in body
    assert "filesystem" in body


def test_grant_cmd_auto_increments_grant_ids(tmp_state: Path) -> None:
    _add_server("filesystem")
    _add_server("fs-write")
    a = runner.invoke(
        app,
        ["--json", "mcp", "grant", "wave", "P10-I01-W04", "filesystem"],
    )
    b = runner.invoke(
        app,
        ["--json", "mcp", "grant", "profile", "research", "fs-write"],
    )
    assert a.exit_code == 0, a.output
    assert b.exit_code == 0, b.output
    assert json.loads(a.output)["id"] == "GRANT-1"
    assert json.loads(b.output)["id"] == "GRANT-2"


def test_grant_cmd_honours_explicit_grant_id_override(tmp_state: Path) -> None:
    _add_server("filesystem")
    res = runner.invoke(
        app,
        [
            "--json",
            "mcp",
            "grant",
            "global",
            "global",
            "filesystem",
            "--grant-id",
            "GRANT-42",
        ],
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["id"] == "GRANT-42"


# ---- grant_cmd error paths --------------------------------------------------


def test_grant_cmd_rejects_unknown_scope_kind(tmp_state: Path) -> None:
    _add_server("filesystem")
    res = runner.invoke(
        app,
        ["mcp", "grant", "team", "research", "filesystem"],
    )
    # InvalidInput → exit code 3.
    assert res.exit_code == 1, res.output
    assert "scope_kind" in res.output


def test_grant_cmd_refuses_dangling_server_id_via_invariant(tmp_state: Path) -> None:
    """Granting against an unregistered server fires
    ``INV.REF.MCP_GRANT_SERVER_MISSING`` and the transaction rolls back."""
    res = runner.invoke(
        app,
        ["mcp", "grant", "wave", "P10-I01-W04", "ghost-server"],
    )
    # ValidationFailed → exit code 4.
    assert res.exit_code == 2, res.output
    assert "INV.REF.MCP_GRANT_SERVER_MISSING" in res.output
    state = _read_state(tmp_state)
    assert state.get("mcp_grants") in (None, {})


def test_grant_cmd_rejects_duplicate_grant_id(tmp_state: Path) -> None:
    _add_server("filesystem")
    first = runner.invoke(
        app,
        ["mcp", "grant", "wave", "P10-I01-W04", "filesystem", "--grant-id", "GRANT-7"],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        app,
        ["mcp", "grant", "wave", "P10-I01-W04", "filesystem", "--grant-id", "GRANT-7"],
    )
    assert second.exit_code == 1, second.output
    assert "already exists" in second.output


# ---- revoke_cmd paths -------------------------------------------------------


def test_revoke_cmd_removes_grant_and_bumps_updated_at(tmp_state: Path) -> None:
    _add_server("filesystem")
    grant = runner.invoke(
        app,
        ["--json", "mcp", "grant", "wave", "P10-I01-W04", "filesystem"],
    )
    assert grant.exit_code == 0, grant.output
    grant_id = json.loads(grant.output)["id"]
    mid_state = _read_state(tmp_state)
    mid_updated_at = mid_state["updated_at"]

    res = runner.invoke(app, ["--json", "mcp", "revoke", grant_id])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["id"] == grant_id
    assert payload["removed_from_state"] is True

    final = _read_state(tmp_state)
    # mcp_grants reverted to None once empty (mirrors mcp_servers handling).
    assert final.get("mcp_grants") is None
    assert final["updated_at"] != mid_updated_at


def test_revoke_cmd_keeps_other_grants_intact(tmp_state: Path) -> None:
    _add_server("filesystem")
    _add_server("fs-write")
    runner.invoke(app, ["mcp", "grant", "wave", "P10-I01-W04", "filesystem"])
    runner.invoke(app, ["mcp", "grant", "profile", "research", "fs-write"])
    res = runner.invoke(app, ["mcp", "revoke", "GRANT-1"])
    assert res.exit_code == 0, res.output
    final = _read_state(tmp_state)
    grants = final["mcp_grants"]
    assert isinstance(grants, dict)
    assert "GRANT-1" not in grants
    assert "GRANT-2" in grants


def test_revoke_cmd_returns_not_found_on_missing_id(tmp_state: Path) -> None:
    res = runner.invoke(app, ["mcp", "revoke", "GRANT-404"])
    # NotFound → exit code 2.
    assert res.exit_code == 1, res.output
    assert "GRANT-404" in res.output


def test_revoke_cmd_text_mode_one_line_summary(tmp_state: Path) -> None:
    _add_server("filesystem")
    runner.invoke(app, ["mcp", "grant", "wave", "P10-I01-W04", "filesystem"])
    res = runner.invoke(app, ["mcp", "revoke", "GRANT-1"])
    assert res.exit_code == 0, res.output
    body = res.output.strip()
    assert body.startswith("mcp revoked: GRANT-1")
    assert "wave=P10-I01-W04" in body
