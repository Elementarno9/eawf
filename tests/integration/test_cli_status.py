"""Integration tests for ``eawf status`` driving the CLI via :class:`CliRunner`.

A small valid state.json is stamped under ``tmp_path/.ea/`` and ``EA_STATE``
is pointed at it. The git invocations are stubbed out via monkeypatching of
:func:`subprocess.run` so the test never depends on a real worktree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


_VALID_STATE: dict[str, Any] = {
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
        "phase_id": "P01",
        "iter_id": "P01-I01",
        "active_wave_ids": ["P01-I01-W01"],
        "active_session_ids": [],
    },
    "workspace": None,
    "phases": {
        "P01": {
            "id": "P01",
            "scope_id": "QR",
            "subproject_id": None,
            "title": "Bootstrap",
            "status": "active",
            "iter_ids": ["P01-I01"],
            "outcome_ids": [],
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "audit_id": None,
        }
    },
    "iters": {
        "P01-I01": {
            "id": "P01-I01",
            "phase_id": "P01",
            "title": "Iter 1",
            "status": "active",
            "wave_ids": ["P01-I01-W01"],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    },
    "waves": {
        "P01-I01-W01": {
            "id": "P01-I01-W01",
            "iter_id": "P01-I01",
            "title": "W1",
            "status": "in_progress",
            "deps": [],
            "file_scopes": ["src/foo.py"],
            "claim_session_id": "S-1",
            "worktree_id": None,
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


def _stub_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every git subprocess invocation to fail with FileNotFoundError."""

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _seed(tmp_path: Path, state: dict[str, Any] | None = None) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state if state is not None else _VALID_STATE))
    return state_path


def test_status_json_envelope_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"]["code"] == "QR"
    assert payload["scope_kind"] == "repo"
    assert payload["current"]["phase_id"] == "P01"
    assert payload["current"]["iter_id"] == "P01-I01"
    assert payload["active_waves"][0]["id"] == "P01-I01-W01"
    assert payload["last_phase_audit"] is None
    assert payload["last_iter_audit"] is None
    assert payload["git"] == {"head": None, "branch": None, "dirty": None}
    assert payload["blockers"] == []
    assert payload["last_closed_waves"] == []
    assert payload["recent_decisions"] == []
    assert payload["open_backlog"] == []


def test_status_text_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "project: QR (Quant Research)" in result.stdout
    assert "phase=P01 iter=P01-I01" in result.stdout
    assert "blockers: none" in result.stdout


def test_status_command_local_json_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status --json`` (subcommand-level) also activates JSON emission."""
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"]["code"] == "QR"


def test_status_returns_not_found_when_state_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1  # NOT_FOUND
    body = json.loads(result.stdout)
    assert body["error"] == "UserError"
    assert "state.json" in body["message"]


def test_status_returns_invalid_input_when_state_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    bad = dict(_VALID_STATE)
    bad["scope_kind"] = "not-a-real-scope-kind"
    state_path.write_bytes(orjson.dumps(bad))
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1  # INVALID_INPUT
    body = json.loads(result.stdout)
    assert body["error"] == "UserError"


def test_status_workspace_flag_overrides_pwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)
    monkeypatch.delenv("EA_STATE", raising=False)
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status", "-w", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"]["code"] == "QR"


def test_status_lists_last_closed_waves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = json.loads(json.dumps(_VALID_STATE))  # deep copy
    state["waves"]["P01-I01-W00"] = {
        "id": "P01-I01-W00",
        "iter_id": "P01-I01",
        "title": "W0",
        "status": "closed",
        "deps": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "outcome": "ok",
        "opened_at": "2026-05-07T00:00:00Z",
        "closed_at": "2026-05-07T01:00:00Z",
    }
    state["iters"]["P01-I01"]["wave_ids"].append("P01-I01-W00")
    state_path = _seed(tmp_path, state)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "P01-I01-W00" in payload["last_closed_waves"]


def test_status_payload_keys_documented_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the JSON envelope key surface so future waves don't drift it."""
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    payload = json.loads(result.stdout)
    expected_keys = {
        "project",
        "scope_kind",
        "current",
        "last_phase_audit",
        "last_iter_audit",
        "active_waves",
        "active_sessions",
        "last_closed_waves",
        "recent_decisions",
        "open_backlog",
        "git",
        "drift",
        "blockers",
    }
    assert set(payload.keys()) == expected_keys


def _state_with_decisions_and_backlog() -> dict[str, Any]:
    """Deep-copy the base fixture and stamp two decisions + three backlog items."""
    state = json.loads(json.dumps(_VALID_STATE))
    state["decisions"] = {
        "D01": {
            "id": "D01",
            "scope_id": "QR",
            "title": "Pick portalocker for cross-platform file locks",
            "rationale": "portalocker is the only maintained cross-platform advisory lock.",
            "alternatives": ["fcntl-only"],
            "status": "active",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "D02": {
            "id": "D02",
            "scope_id": "QR",
            "title": "Adopt Pydantic v2 strict models at every boundary",
            "rationale": "strict validation at ingestion keeps downstream code typed.",
            "alternatives": [],
            "status": "active",
            "created_at": "2026-05-09T00:00:00Z",
        },
    }
    state["backlog"] = {
        "B01": {
            "id": "B01",
            "scope_id": "QR",
            "title": "Wire telemetry capture into wave close",
            "priority": "P1",
            "status": "open",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "B02": {
            "id": "B02",
            "scope_id": "QR",
            "title": "Backfill estimate reference classes",
            "priority": "P0",
            "status": "in_progress",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "B03": {
            "id": "B03",
            "scope_id": "QR",
            "title": "Old idea that already shipped",
            "priority": "P2",
            "status": "closed",
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": "2026-05-09T00:00:00Z",
        },
    }
    return state


def test_status_recent_decisions_newest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recent_decisions`` lists decisions newest-first with a compact projection."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    decisions = payload["recent_decisions"]
    assert [d["id"] for d in decisions] == ["D02", "D01"]
    assert decisions[0] == {
        "id": "D02",
        "title": "Adopt Pydantic v2 strict models at every boundary",
        "status": "active",
    }


def test_status_open_backlog_filters_closed_and_sorts_by_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``open_backlog`` drops closed items and sorts P0 before P1."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    backlog = payload["open_backlog"]
    assert [b["id"] for b in backlog] == ["B02", "B01"]  # P0 before P1; B03 closed → absent
    assert backlog[0]["priority"] == "P0"
    assert backlog[0]["status"] == "in_progress"


def test_status_is_byte_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status`` mutates nothing: state.json is byte-equal before and after.

    The whole status surface is a pure projection (AGENTS rule: the digest /
    status projections are PURE). This test hashes state.json before and after
    the command and asserts the digest is unchanged, so a future regression
    that writes through the status path is caught.
    """
    import hashlib

    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    before = hashlib.sha256(state_path.read_bytes()).hexdigest()
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    after = hashlib.sha256(state_path.read_bytes()).hexdigest()
    assert before == after


def test_status_text_branch_surfaces_decisions_and_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text branch shows compact recent-decisions + open-backlog lines."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "recent decisions: D02, D01" in result.stdout
    assert "open backlog: 2 (B02, B01)" in result.stdout
