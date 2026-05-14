"""Integration tests for the typed agent-report CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:QR",
                "updated_at": "2026-05-14T00:00:00Z",
                "project": {
                    "code": "QR",
                    "slug": "qr",
                    "title": "QR",
                    "domains": [],
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
                    "active_session_ids": ["SES-001"],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {
                    "SES-001": {
                        "id": "SES-001",
                        "role": "executor",
                        "runtime": "codex",
                        "scope_id": "P18-I01-W06",
                        "status": "active",
                        "claimed_wave_ids": [],
                        "worktree_ids": [],
                        "artifact_ids": [],
                        "started_at": "2026-05-14T00:00:00Z",
                        "ended_at": None,
                        "summary": None,
                    }
                },
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _body(summary: str = "implemented CLI") -> str:
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": summary,
            "wave_id": "P18-I01-W06",
            "outcome": "done",
        }
    )


def test_agent_report_add_show_list_and_rollup(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    add = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "agent-report",
            "add",
            "--session",
            "SES-001",
            "--base-id",
            "P18-I01-W06",
            "--body-json",
            _body(),
        ],
    )
    assert add.exit_code == 0, add.stdout
    added = json.loads(add.stdout)
    assert added["id"] == "AR-executor-P18-I01-W06-01"
    assert added["store_kind"] == "executor_report"

    listed = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "agent-report", "list", "--role", "executor"],
    )
    assert listed.exit_code == 0, listed.stdout
    rows = json.loads(listed.stdout)["reports"]
    assert [row["id"] for row in rows] == ["AR-executor-P18-I01-W06-01"]

    shown = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "agent-report",
            "show",
            "AR-executor-P18-I01-W06-01",
            "--role",
            "executor",
        ],
    )
    assert shown.exit_code == 0, shown.stdout
    report = json.loads(shown.stdout)["report"]
    assert report["header"]["attempt"] == 1

    rollup = runner.invoke(app, ["--json", "-w", str(workspace), "operator", "rollup", "P18"])
    assert rollup.exit_code == 0, rollup.stdout
    payload = json.loads(rollup.stdout)
    assert payload["report_count"] == 1
    assert payload["by_role"] == {"executor": 1}


def test_agent_report_add_increments_attempt(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    for summary in ("first", "second"):
        result = runner.invoke(
            app,
            [
                "--json",
                "-w",
                str(workspace),
                "agent-report",
                "add",
                "--session",
                "SES-001",
                "--base-id",
                "P18-I01-W06",
                "--body-json",
                _body(summary),
            ],
        )
        assert result.exit_code == 0, result.stdout
    listed = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "agent-report", "list", "--base-id", "P18-I01-W06"],
    )
    attempts = [row["attempt"] for row in json.loads(listed.stdout)["reports"]]
    assert attempts == [1, 2]
