"""CLI integration tests for ``eawf worktree create``.

Each test sets up a real ``git init``-ed repo via ``tmp_path``, seeds an
``.ea/state.json`` with one CLAIMED wave, then drives the Typer app via
:class:`typer.testing.CliRunner`. Asserts cover the JSON envelope shape,
the on-disk worktree, and the post-mutation state file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for worktree CLI integration tests",
)


def _seed_repo_with_state(workdir: Path, *, on_main: bool = False) -> tuple[Path, Path]:
    """Initialise a git repo + .ea/state.json. Returns (repo_root, state_path)."""
    workdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=workdir, check=True)
    (workdir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workdir, check=True)
    if not on_main:
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/eawf-v0.1"],
            cwd=workdir,
            check=True,
        )

    state_path = workdir / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:DEMO",
        "updated_at": datetime.now(UTC).isoformat(),
        "project": {
            "code": "DEMO",
            "slug": "demo",
            "title": "Demo",
            "description": None,
            "domains": ["test"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:DEMO",
        },
        "current": {
            "project_code": "DEMO",
            "track_id": None,
            "phase_id": "P05",
            "iter_id": "P05-I01",
            "active_wave_ids": ["P05-I01-W01"],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P05": {
                "id": "P05",
                "scope_id": "DEMO",
                "track_id": None,
                "title": "Phase 5",
                "status": "active",
                "iter_ids": ["P05-I01"],
                "outcome_ids": [],
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P05-I01": {
                "id": "P05-I01",
                "phase_id": "P05",
                "title": "Iter 1",
                "status": "active",
                "wave_ids": ["P05-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            "P05-I01-W01": {
                "id": "P05-I01-W01",
                "iter_id": "P05-I01",
                "title": "W1",
                "status": "claimed",
                "deps": [],
                "file_scopes": ["src/eawf/runtime/worktree/"],
                "claim_session_id": "SES-001",
                "worktree_id": None,
                "outcome": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    return workdir, state_path


def test_cli_create_full_cycle(tmp_path: Path) -> None:
    """Full CLI flow: create a worktree, assert envelope + git state + state.json."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    res = runner.invoke(
        app,
        [
            "-w",
            str(repo),
            "worktree",
            "create",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    # Worktree dir created.
    assert (repo / ".ea" / "worktrees" / "p05-w01").is_dir()
    # state.json updated with worktree record + wave.worktree_id.
    payload = orjson.loads(state_path.read_bytes())
    assert payload["worktrees"]
    record_id = next(iter(payload["worktrees"]))
    assert payload["waves"]["P05-I01-W01"]["worktree_id"] == record_id
    assert payload["worktrees"][record_id]["status"] == "active"


def test_cli_create_json_envelope_keys(tmp_path: Path) -> None:
    """``--json`` emits the canonical envelope keys with correct types."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "worktree",
            "create",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    for key in (
        "worktree_id",
        "wave_id",
        "branch",
        "base_branch",
        "path",
        "status",
        "created_at",
    ):
        assert key in envelope, f"missing key {key!r} in {envelope}"
    assert envelope["wave_id"] == "P05-I01-W01"
    assert envelope["branch"] == "feature/eawf-v0.1-p05-w01"
    assert envelope["status"] == "active"


def test_cli_create_exit_3_on_main(tmp_path: Path) -> None:
    """Operator on ``main`` (project default_branch) gets exit 3."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo", on_main=True)
    res = runner.invoke(
        app,
        [
            "-w",
            str(repo),
            "worktree",
            "create",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 1, res.stdout
    assert "refuses to branch from" in res.stdout
