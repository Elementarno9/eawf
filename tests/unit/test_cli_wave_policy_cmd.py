"""Unit tests for ``eawf wave policy set/show``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _seed_state(state_dir: Path) -> Path:
    """Write a minimal state.json with one closed phase and one open wave."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
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
                    "phase_id": "P00",
                    "iter_id": "P00-I01",
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {
                    "P00": {
                        "id": "P00",
                        "scope_id": "ZZ",
                        "subproject_id": None,
                        "title": "bootstrap",
                        "status": "active",
                        "iter_ids": ["P00-I01"],
                        "outcome_ids": [],
                        "opened_at": "2026-05-08T00:00:00Z",
                        "closed_at": None,
                        "audit_id": None,
                    }
                },
                "iters": {
                    "P00-I01": {
                        "id": "P00-I01",
                        "phase_id": "P00",
                        "title": "first",
                        "status": "active",
                        "wave_ids": ["P00-I01-W01"],
                        "estimate_id": None,
                        "audit_id": None,
                        "opened_at": "2026-05-08T00:00:00Z",
                        "closed_at": None,
                    }
                },
                "waves": {
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
                },
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    return state_path


@pytest.fixture
def state_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    _seed_state(state_dir)
    monkeypatch.setenv("EA_STATE", str(state_dir / "state.json"))
    return tmp_path


def test_wave_policy_set_then_show(state_workspace: Path) -> None:
    from eawf.cli.app import app

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "wave",
            "policy",
            "set",
            "P00-I01-W01",
            "--allow=Read,Edit,Bash",
            "--deny=Write",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "policy set: POL-1" in res.output

    res_show = runner.invoke(app, ["wave", "policy", "show", "P00-I01-W01"])
    assert res_show.exit_code == 0, res_show.output
    assert "POL-1" in res_show.output
    assert "Read,Edit,Bash" in res_show.output
    assert "Write" in res_show.output


def test_wave_policy_set_rejects_bad_scope_kind(state_workspace: Path) -> None:
    from eawf.cli.app import app

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["wave", "policy", "set", "P00-I01-W01", "--scope-kind=cluster"],
    )
    assert res.exit_code != 0
    assert "unknown scope_kind" in res.output


def test_wave_policy_show_missing_scope_returns_no_policies(state_workspace: Path) -> None:
    from eawf.cli.app import app

    runner = CliRunner()
    res = runner.invoke(app, ["wave", "policy", "show", "P99-I99-W99"])
    assert res.exit_code == 0, res.output
    assert "(no policies)" in res.output
