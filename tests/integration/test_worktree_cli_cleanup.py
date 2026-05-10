"""CLI integration tests for ``eawf worktree cleanup``.

Each test creates a worktree, optionally pollutes / merges it, then drives
``eawf worktree cleanup`` and asserts the dirty/CONFLICTED guards, the
ABANDONED transition, and the registry-pruning success path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from tests.integration.test_worktree_cli_create import _seed_repo_with_state
from tests.integration.test_worktree_cli_merge_back import _create_worktree_and_commit

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for worktree CLI integration tests",
)


def test_cli_cleanup_after_merge(tmp_path: Path) -> None:
    """Full lifecycle: create -> commit -> merge-back -> cleanup."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    wt_path, _ = _create_worktree_and_commit(
        repo,
        state_path,
        file_name="hello.txt",
        content="hi\n",
        msg="add hello",
    )
    # Merge back.
    res = runner.invoke(
        app,
        [
            "-w",
            str(repo),
            "worktree",
            "merge-back",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    # Cleanup.
    res = runner.invoke(
        app,
        [
            "-w",
            str(repo),
            "worktree",
            "cleanup",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    # git worktree list should no longer show the path.
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert wt_path not in out


def test_cli_cleanup_refuses_dirty_no_force(tmp_path: Path) -> None:
    """A dirty worktree refuses cleanup without ``--force`` (exit 8).

    Post-condition: the polluting file is still on disk after the
    refusal, so a silent success that swallowed the dirty marker can't
    pass this gate.
    """
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    wt_path, _ = _create_worktree_and_commit(
        repo,
        state_path,
        file_name="hello.txt",
        content="hi\n",
        msg="add hello",
    )
    # Pollute the worktree (but DO NOT merge, so status stays ACTIVE).
    scratch = Path(wt_path) / "scratch.txt"
    scratch.write_text("dirty\n", encoding="utf-8")
    res = runner.invoke(
        app,
        [
            "-w",
            str(repo),
            "worktree",
            "cleanup",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 8, res.stdout
    assert "dirty" in res.stdout
    # Refusal must have left the worktree untouched — the polluting
    # file is still present and the worktree directory exists.
    assert scratch.exists(), "cleanup refused but the dirty file was removed anyway"
    assert Path(wt_path).exists(), "cleanup refused but the worktree dir was removed"
