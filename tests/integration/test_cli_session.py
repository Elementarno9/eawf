"""End-to-end CLI tests for ``eawf session ...`` against a temp state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests.integration._upkeep_daemon import upkeep_daemon  # noqa: F401

pytestmark = pytest.mark.usefixtures("upkeep_daemon")

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
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return state_path


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def test_session_start_rejects_dual_session(tmp_state: Path) -> None:
    runner.invoke(
        app,
        [
            "session",
            "start",
            "--role",
            "executor",
            "--scope",
            "QR",
            "--runtime",
            "claude",
        ],
    )
    result = runner.invoke(
        app,
        [
            "session",
            "start",
            "--role",
            "executor",
            "--scope",
            "QR",
            "--runtime",
            "claude",
        ],
    )
    assert result.exit_code == 2  # VALIDATION_FAILED


def test_session_close_unknown_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["session", "close", "SES-MISSING"])
    assert result.exit_code == 1


def test_session_recover_marks_stale_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed a state with an aged session; ``session recover`` must mark it stale."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    aged_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
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
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": ["SES-OLD"],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {
            "SES-OLD": {
                "id": "SES-OLD",
                "role": "executor",
                "runtime": "claude",
                "scope_id": "QR",
                "status": "active",
                "claimed_wave_ids": [],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": aged_at,
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "session", "recover", "--age", "30"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "SES-OLD" in payload["marked_session_ids"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["agent_sessions"]["SES-OLD"]["status"] == "stale"


def test_session_recover_default_age(tmp_state: Path) -> None:
    """`session recover` with no flag uses the default 30-minute threshold."""
    result = runner.invoke(app, ["--json", "session", "recover"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["age_minutes"] == 30
