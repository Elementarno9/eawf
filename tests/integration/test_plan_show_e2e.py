"""End-to-end CLI integration tests for ``eawf plan show``.

Tests use ``typer.testing.CliRunner`` against the registered ``app`` so the
full Typer dispatch path (global flags, command discovery, exit-code mapping)
is exercised. ``EA_STATE`` is pointed at a fixture stamped under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAN_SCHEMA = _REPO_ROOT / "src" / "eawf" / "schemas" / "plan-view.schema.json"


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
        "phase_id": "P05",
        "iter_id": "P05-I01",
        "active_wave_ids": [],
        "active_session_ids": [],
    },
    "workspace": None,
    "phases": {
        "P05": {
            "id": "P05",
            "scope_id": "QR",
            "title": "Phase Five",
            "status": "active",
            "iter_ids": ["P05-I01"],
            "outcome_ids": [],
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "audit_id": None,
        }
    },
    "iters": {
        "P05-I01": {
            "id": "P05-I01",
            "phase_id": "P05",
            "title": "Iter One",
            "status": "active",
            "wave_ids": ["P05-I01-W00", "P05-I01-W01", "P05-I01-W02"],
            "estimate_id": None,
            "audit_id": "AU-1",
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    },
    "waves": {
        "P05-I01-W00": {
            "id": "P05-I01-W00",
            "iter_id": "P05-I01",
            "title": "First wave",
            "status": "closed",
            "deps": [],
            "file_scopes": ["src/eawf/foo.py"],
            "claim_session_id": None,
            "worktree_id": None,
            "outcome": "ok",
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": "2026-05-08T01:00:00Z",
        },
        "P05-I01-W01": {
            "id": "P05-I01-W01",
            "iter_id": "P05-I01",
            "title": "Second wave",
            "status": "claimed",
            "deps": ["P05-I01-W00"],
            "file_scopes": [],
            "claim_session_id": "S-1",
            "worktree_id": None,
            "outcome": None,
            "opened_at": "2026-05-08T01:00:00Z",
            "closed_at": None,
        },
        "P05-I01-W02": {
            "id": "P05-I01-W02",
            "iter_id": "P05-I01",
            "title": "Third wave",
            "status": "pending",
            "deps": ["P05-I01-W01"],
            "file_scopes": [],
            "claim_session_id": None,
            "worktree_id": None,
            "outcome": None,
            "opened_at": "2026-05-08T01:00:00Z",
            "closed_at": None,
        },
    },
    "audits": {
        "AU-1": {
            "id": "AU-1",
            "scope_id": "P05-I01",
            "kind": "evaluation",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "ruff_clean", "passed": True, "details": None},
                {"name": "mypy_strict", "passed": False, "details": "10 errors"},
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "minor",
        }
    },
    "artifacts": {},
    "agent_sessions": {
        "S-1": {
            "id": "S-1",
            "role": "executor",
            "runtime": "claude",
            "scope_id": "P05-I01-W01",
            "status": "active",
            "claimed_wave_ids": ["P05-I01-W01"],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-05-08T01:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    },
    "plugins": {},
    "indexes": {},
}


@pytest.fixture(autouse=True)
def _isolate_ea_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("EA_STATE", raising=False)
    yield


def _seed(tmp_path: Path, state: dict[str, Any] | None = None) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state if state is not None else _VALID_STATE))
    return state_path


def test_plan_show_markdown_default_iter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["plan", "show"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "# Plan: P05-I01" in out
    assert "## Summary" in out
    assert "## DAG" in out
    assert "## Waves" in out
    assert "## Checks" in out
    assert "## Risks" in out
    assert "P05-I01-W00" in out
    assert "P05-I01-W01" in out
    assert "P05-I01-W02" in out


def test_plan_show_json_validates_against_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    schema = json.loads(_PLAN_SCHEMA.read_text())
    jsonschema.validate(envelope, schema)
    # Surface contract spot-checks.
    assert envelope["iter"]["id"] == "P05-I01"
    assert len(envelope["waves"]) == 3
    assert envelope["dag"]["topo_order"] == ["P05-I01-W00", "P05-I01-W01", "P05-I01-W02"]
    assert envelope["summary"]["wave_count"] == 3
    # The checks list contains the iter audit + a wave_outcome synthetic check.
    sources = {c["source"] for c in envelope["checks"]}
    assert "iter_audit" in sources
    assert "wave_outcome" in sources


def test_plan_show_iter_override_picks_inactive_iter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = deepcopy(_VALID_STATE)
    state["iters"]["P05-I02"] = {
        "id": "P05-I02",
        "phase_id": "P05",
        "title": "Iter Two (closed)",
        "status": "closed",
        "wave_ids": [],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T05:00:00Z",
    }
    state["phases"]["P05"]["iter_ids"].append("P05-I02")
    state_path = _seed(tmp_path, state)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show", "--iter", "P05-I02"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["iter"]["id"] == "P05-I02"
    assert envelope["iter"]["status"] == "closed"


def test_plan_show_no_active_iter_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = deepcopy(_VALID_STATE)
    state["current"]["iter_id"] = None
    state["current"]["phase_id"] = None
    state_path = _seed(tmp_path, state)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show"])
    assert result.exit_code == 3, result.output
    body = json.loads(result.stdout)
    assert body["error"] == "InvalidInput"
    assert "no active iter" in body["message"]


def test_plan_show_unknown_iter_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show", "--iter", "P99-I99"])
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["error"] == "NotFound"


def test_plan_show_format_conflict_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show", "--format", "markdown"])
    assert result.exit_code == 3, result.output
    body = json.loads(result.stdout)
    assert body["error"] == "InvalidInput"
    assert "contradictory" in body["message"]


def test_plan_show_invalid_iter_id_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show", "--iter", "not-an-iter"])
    assert result.exit_code == 3, result.output
    body = json.loads(result.stdout)
    assert body["error"] == "InvalidInput"
    assert "invalid iter id" in body["message"]


def test_plan_show_state_missing_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    result = runner.invoke(app, ["--json", "plan", "show"])
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["error"] == "NotFound"


def test_plan_show_ascii_dag_replaces_mermaid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["plan", "show", "--ascii"])
    assert result.exit_code == 0, result.output
    assert "```mermaid" not in result.stdout
    assert "P05-I01-W00 (closed)" in result.stdout


def test_plan_show_section_filter_restricts_markdown_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["plan", "show", "--show", "risks"])
    assert result.exit_code == 0, result.output
    assert "## Risks" in result.stdout
    # Other sections suppressed.
    assert "## Summary" not in result.stdout
    assert "## DAG" not in result.stdout


def test_plan_show_help_lists_all_options() -> None:
    result = runner.invoke(app, ["plan", "show", "--help"])
    assert result.exit_code == 0, result.output
    # Typer's Rich formatter wraps option names in ANSI bold/dim sequences when
    # rendering through CliRunner (no real TTY); strip them before substring
    # checks so the assertion survives both decorated and plain output.
    out = _ANSI_RE.sub("", result.stdout)
    assert "--iter" in out, out
    assert "--format" in out, out
    assert "--show" in out, out
    assert "--ascii" in out, out


def test_plan_show_format_json_consistent_with_global_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json --format json`` is consistent — accepted, exit 0."""
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show", "--format", "json"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["iter"]["id"] == "P05-I01"


def test_plan_show_invalid_json_exits_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted state.json surfaces as ValidationFailed (exit 4), not a traceback."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(exist_ok=True)
    state_path = state_dir / "state.json"
    # Truncated JSON object — orjson.JSONDecodeError lands inside plan show's
    # except branch and maps to ValidationFailed.
    state_path.write_text('{"schema_version": "1.0",', encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "plan", "show"])
    assert result.exit_code == 4, result.output
    body = json.loads(result.stdout)
    assert body["error"] == "ValidationFailed"
    assert "not valid JSON" in body["message"]
