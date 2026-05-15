"""CLI integration tests for ``eawf wave land`` and ``eawf wave land-batch``.

The tests stand up a real ``git init``-ed tmp repo, seed an
``.ea/state.json`` with one or more CLAIMED waves, create worktrees,
make commits, and drive the wave-land verbs via :class:`CliRunner`.
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

from eawf.cli.app import app
from tests.integration.test_worktree_cli_create import _seed_repo_with_state
from tests.integration.test_worktree_cli_merge_back import _create_worktree_and_commit

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for wave land CLI tests",
)


def test_wave_land_happy_path_envelope_shape(tmp_path: Path) -> None:
    """The success envelope carries wave / commits / outcome / worktree_cleaned."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    wt_path, _ = _create_worktree_and_commit(
        repo,
        state_path,
        file_name="hello.txt",
        content="hi\n",
        msg="add hello",
    )

    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "wave",
            "land",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    for key in ("wave", "commits", "outcome", "worktree_cleaned"):
        assert key in envelope, f"missing key {key!r} in {envelope}"
    assert envelope["wave"] == "P05-I01-W01"
    assert isinstance(envelope["commits"], list)
    assert envelope["commits"], "expected at least one picked commit"
    assert envelope["worktree_cleaned"] is True
    assert "landed" in envelope["outcome"]
    # state.json reflects wave closed + worktree torn down.
    payload = orjson.loads(state_path.read_bytes())
    assert payload["waves"]["P05-I01-W01"]["status"] == "closed"
    # ``wave land`` does not pin a commit (B045 wires ``wave close --commit``
    # only); the field is present-but-null on the persisted wave.
    assert payload["waves"]["P05-I01-W01"]["commit"] is None
    # Parent branch should now have the file.
    assert (repo / "hello.txt").exists()
    # Worktree dir should be gone after cleanup.
    assert not Path(wt_path).exists()


def test_wave_land_unknown_wave_exit_2(tmp_path: Path) -> None:
    """An unknown wave id surfaces exit 2 (NOT_FOUND)."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "wave",
            "land",
            "P99-I99-W99",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 2, res.stdout


def test_wave_land_already_closed_wave_exit_4(tmp_path: Path) -> None:
    """Landing a wave that's already CLOSED surfaces exit 4 (VALIDATION_FAILED)."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    payload = orjson.loads(state_path.read_bytes())
    payload["waves"]["P05-I01-W01"]["status"] = "closed"
    payload["waves"]["P05-I01-W01"]["outcome"] = "previously closed"
    payload["waves"]["P05-I01-W01"]["closed_at"] = datetime.now(UTC).isoformat()
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))

    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "wave",
            "land",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 4, res.stdout


def test_wave_land_batch_stops_on_failure(tmp_path: Path) -> None:
    """When a wave in the batch conflicts, batch halts and exits non-zero."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    # Inject a second wave in the same iter so the batch has two candidates.
    payload = orjson.loads(state_path.read_bytes())
    payload["waves"]["P05-I01-W02"] = {
        "id": "P05-I01-W02",
        "iter_id": "P05-I01",
        "title": "W2",
        "status": "claimed",
        "deps": [],
        "file_scopes": ["src/eawf/dispatch/"],
        "claim_session_id": "SES-002",
        "worktree_id": None,
        "outcome": None,
        "opened_at": datetime.now(UTC).isoformat(),
        "closed_at": None,
    }
    payload["iters"]["P05-I01"]["wave_ids"] = ["P05-I01-W01", "P05-I01-W02"]
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))

    # Seed a conflict on parent before creating worktrees.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)
    # W01's worktree creates a divergent commit on conflict.txt.
    _create_worktree_and_commit(
        repo,
        state_path,
        file_name="conflict.txt",
        content="worktree-a\n",
        msg="wt-a edit",
    )
    # Move parent forward so W01's cherry-pick will conflict.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    # Drive land-batch with --iter scoping so only this iter is processed.
    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "wave",
            "land-batch",
            "--iter",
            "P05-I01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 4, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["failed_wave"] == "P05-I01-W01"
    assert envelope["error"]
    # Nothing landed — W01 conflicted on the first attempt; W02 was
    # never reached because the batch halts on the first failure.
    assert envelope["landed"] == []
