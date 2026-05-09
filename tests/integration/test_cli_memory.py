"""End-to-end CLI tests for ``eawf memory ...`` against a temp ``state.json``.

Drives the full path: add → list → render-context → view → stale → compact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


def _seed_state(tmp_path: Path) -> Path:
    """Write a minimal valid ``state.json`` and return its path."""
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


def test_memory_add_creates_entry(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--json",
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "Use uv run",
            "--body",
            "All Python invocations go through uv.",
            "--confidence",
            "h",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"].startswith("MEM-")
    assert payload["confidence"] == "high"
    # state.json was updated with the new memory_index entry.
    saved = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert payload["id"] in saved["memory_index"]


def test_memory_list_returns_added_entry(tmp_state: Path) -> None:
    runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "first",
            "--body",
            "body",
        ],
    )
    result = runner.invoke(app, ["--json", "memory", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 1


def test_memory_list_filter_by_scope(tmp_state: Path) -> None:
    runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "qr-entry",
            "--body",
            "body",
        ],
    )
    runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "P01",
            "--title",
            "phase-entry",
            "--body",
            "body",
        ],
    )
    result = runner.invoke(app, ["--json", "memory", "list", "--scope", "QR"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["entries"][0]["scope_id"] == "QR"


def test_memory_render_context_with_budget(tmp_state: Path) -> None:
    big_body = " ".join(["lorem"] * 100)
    for i in range(5):
        runner.invoke(
            app,
            [
                "memory",
                "add",
                "--scope",
                "QR",
                "--title",
                f"entry {i}",
                "--body",
                big_body,
            ],
        )
    result = runner.invoke(
        app,
        [
            "--json",
            "memory",
            "render-context",
            "--budget",
            "200",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["budget"] == 200
    assert payload["tokens_used"] <= 200
    assert payload["skipped_count"] >= 1


def test_memory_view_shows_full_body(tmp_state: Path) -> None:
    add = runner.invoke(
        app,
        [
            "--json",
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "title here",
            "--body",
            "complete body of memory entry",
        ],
    )
    payload = json.loads(add.output)
    mem_id = payload["id"]
    view = runner.invoke(app, ["--json", "memory", "view", mem_id])
    assert view.exit_code == 0
    body = json.loads(view.output)
    assert body["id"] == mem_id
    assert "complete body of memory entry" in body["body"]


def test_memory_view_unknown_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["memory", "view", "MEM-NOPE"])
    assert result.exit_code == 2  # NOT_FOUND


def test_memory_stale_lists_low_confidence_aged(tmp_state: Path) -> None:
    runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "stale candidate",
            "--body",
            "body",
            "--confidence",
            "l",
        ],
    )
    # Use age=0 to ensure all low-confidence entries surface.
    result = runner.invoke(app, ["--json", "memory", "stale", "--age", "0"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] >= 1


def test_memory_compact_idempotent(tmp_state: Path) -> None:
    runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "title",
            "--body",
            "body",
        ],
    )
    a = runner.invoke(app, ["--json", "memory", "compact"])
    b = runner.invoke(app, ["--json", "memory", "compact"])
    assert a.exit_code == 0
    assert b.exit_code == 0
    pa = json.loads(a.output)
    pb = json.loads(b.output)
    # Second compaction should report 0 dedup (idempotent).
    assert pb["dedup_count"] == 0
    assert pa["records_in"] == pb["records_in"] or pa["records_out"] == pb["records_in"]


def test_memory_add_invalid_confidence_returns_invalid_input(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "t",
            "--body",
            "b",
            "--confidence",
            "ultra-high",
        ],
    )
    assert result.exit_code == 3  # INVALID_INPUT


def test_memory_full_pipeline_add_list_render_view_stale_compact(tmp_state: Path) -> None:
    """End-to-end: add → list → render-context → view → stale → compact."""
    add = runner.invoke(
        app,
        [
            "--json",
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "entry",
            "--body",
            "body",
        ],
    )
    assert add.exit_code == 0
    mem_id = json.loads(add.output)["id"]

    listed = runner.invoke(app, ["--json", "memory", "list"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)["count"] == 1

    rendered = runner.invoke(app, ["--json", "memory", "render-context", "--budget", "100"])
    assert rendered.exit_code == 0
    assert json.loads(rendered.output)["budget"] == 100

    view = runner.invoke(app, ["--json", "memory", "view", mem_id])
    assert view.exit_code == 0
    assert json.loads(view.output)["id"] == mem_id

    stale = runner.invoke(app, ["--json", "memory", "stale", "--age", "0"])
    assert stale.exit_code == 0

    compact = runner.invoke(app, ["--json", "memory", "compact"])
    assert compact.exit_code == 0


def test_memory_add_no_state_returns_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "absent.json"))
    result = runner.invoke(
        app,
        [
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "t",
            "--body",
            "b",
        ],
    )
    assert result.exit_code == 2  # NOT_FOUND
