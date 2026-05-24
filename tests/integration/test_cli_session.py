"""End-to-end CLI tests for ``eawf session ...`` against a temp state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

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


def test_session_start_creates_active_record(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--json",
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
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"].startswith("SES-")
    assert payload["status"] == "active"
    assert payload["role"] == "executor"
    saved = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert payload["id"] in saved["agent_sessions"]


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


def test_session_start_invalid_runtime(tmp_state: Path) -> None:
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
            "nope",
        ],
    )
    assert result.exit_code == 1  # INVALID_INPUT


def test_session_start_invalid_role(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        [
            "session",
            "start",
            "--role",
            "wizard",
            "--scope",
            "QR",
            "--runtime",
            "claude",
        ],
    )
    assert result.exit_code == 1


def test_session_start_then_checkpoint_then_close(tmp_state: Path) -> None:
    start = runner.invoke(
        app,
        [
            "--json",
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
    sid = json.loads(start.output)["id"]

    checkpoint = runner.invoke(
        app,
        [
            "--json",
            "session",
            "checkpoint",
            sid,
            "--artifact",
            "ART-001",
            "--files",
            "src/**/*.py",
        ],
    )
    assert checkpoint.exit_code == 0
    body = json.loads(checkpoint.output)
    assert body["status"] == "checkpointed"
    assert "ART-001" in body["artifact_ids"]

    close = runner.invoke(
        app,
        [
            "--json",
            "session",
            "close",
            sid,
            "--status",
            "closed",
            "--summary",
            "done",
        ],
    )
    assert close.exit_code == 0
    body = json.loads(close.output)
    assert body["status"] == "closed"


def test_session_checkpoint_unknown_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["session", "checkpoint", "SES-MISSING"])
    assert result.exit_code == 1


def test_session_close_unknown_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["session", "close", "SES-MISSING"])
    assert result.exit_code == 1


def test_session_close_invalid_status(tmp_state: Path) -> None:
    start = runner.invoke(
        app,
        [
            "--json",
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
    sid = json.loads(start.output)["id"]
    result = runner.invoke(
        app,
        [
            "session",
            "close",
            sid,
            "--status",
            "active",  # not a terminal status
        ],
    )
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
            "subproject_id": None,
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


def test_session_full_pipeline(tmp_state: Path) -> None:
    """Start → checkpoint → close round-trip via JSON envelopes."""
    start = runner.invoke(
        app,
        [
            "--json",
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
    assert start.exit_code == 0
    sid = json.loads(start.output)["id"]
    runner.invoke(app, ["session", "checkpoint", sid])
    close = runner.invoke(
        app,
        ["--json", "session", "close", sid, "--status", "closed"],
    )
    assert close.exit_code == 0


def test_session_start_two_runtimes_same_scope(tmp_state: Path) -> None:
    """Different runtimes for the same scope should both succeed."""
    a = runner.invoke(
        app,
        [
            "--json",
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
    b = runner.invoke(
        app,
        [
            "--json",
            "session",
            "start",
            "--role",
            "executor",
            "--scope",
            "QR",
            "--runtime",
            "opencode",
        ],
    )
    assert a.exit_code == 0
    assert b.exit_code == 0
    assert json.loads(a.output)["id"] != json.loads(b.output)["id"]
