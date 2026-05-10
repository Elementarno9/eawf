"""CLI integration tests for ``eawf worktree merge-back``.

Each test creates a worktree, makes a commit there, then drives the
merge-back via :class:`typer.testing.CliRunner` and asserts the parent
branch reflects the change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from tests.integration.test_worktree_cli_create import _seed_repo_with_state

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for worktree CLI integration tests",
)


def _create_worktree_and_commit(
    repo: Path,
    state_path: Path,
    *,
    file_name: str,
    content: str,
    msg: str,
) -> tuple[str, str]:
    """Create a worktree and commit *content* under *file_name*.

    Returns ``(worktree_path, sha)``.
    """
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
    wt_path = repo / envelope["path"]
    (wt_path / file_name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(wt_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-q", "-m", msg], check=True)
    sha = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return str(wt_path), sha


def test_cli_merge_back_cherry_pick_default(tmp_path: Path) -> None:
    """Default cherry-pick lands the worktree commit on the parent branch."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    _create_worktree_and_commit(
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
            "worktree",
            "merge-back",
            "--wave",
            "P05-I01-W01",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["status"] == "merged"
    assert envelope["strategy"] == "cherry_pick"
    # Parent branch should now have the file.
    assert (repo / "hello.txt").exists()


def test_cli_merge_back_rebase_strategy(tmp_path: Path) -> None:
    """``--strategy rebase_then_ff`` lands via fast-forward."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    _create_worktree_and_commit(
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
            "worktree",
            "merge-back",
            "--wave",
            "P05-I01-W01",
            "--strategy",
            "rebase_then_ff",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    assert envelope["status"] == "merged"
    assert envelope["strategy"] == "rebase_then_ff"
    assert (repo / "hello.txt").exists()


def test_cli_merge_back_conflict_envelope(tmp_path: Path) -> None:
    """A synthetic same-file conflict surfaces a structured envelope."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    # Edit conflict.txt on parent before the worktree exists.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)
    _create_worktree_and_commit(
        repo,
        state_path,
        file_name="conflict.txt",
        content="worktree\n",
        msg="worktree edit",
    )
    # Move the parent forward again so the worktree's commit will conflict.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    res = runner.invoke(
        app,
        [
            "--json",
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
    envelope = json.loads(res.stdout)
    assert envelope["status"] == "conflicted"
    assert "conflict" in envelope
    assert any("conflict.txt" in f for f in envelope["conflict"]["files"])
    # State.json should reflect the CONFLICTED record.
    state = orjson.loads(state_path.read_bytes())
    record_id = next(iter(state["worktrees"]))
    assert state["worktrees"][record_id]["status"] == "conflicted"


def _trigger_conflict(repo: Path, state_path: Path) -> None:
    """Drive the merge-back path until a conflict envelope lands.

    Shared scaffolding for the ``--continue`` / ``--abort`` resume tests:
    edits ``conflict.txt`` on parent, creates a worktree that mutates
    the same file, then advances the parent again so the cherry-pick
    refuses with a conflict.
    """
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)
    _create_worktree_and_commit(
        repo,
        state_path,
        file_name="conflict.txt",
        content="worktree\n",
        msg="worktree edit",
    )
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)
    res = runner.invoke(
        app,
        [
            "--json",
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
    assert json.loads(res.stdout)["status"] == "conflicted"


def test_cli_merge_back_continue_envelope(tmp_path: Path) -> None:
    """``--continue`` after manual resolution lands the merge and reports MERGED."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    _trigger_conflict(repo, state_path)
    # Resolve the conflict in the parent worktree by accepting the
    # worktree's content. The diff vs. parent's HEAD is non-empty
    # (``parent v2`` → ``worktree``) so cherry-pick --continue produces
    # a real commit and the unit-level
    # ``test_continue_after_resolution_completes`` confirms the same
    # convergence pattern lands MERGED.
    (repo / "conflict.txt").write_text("worktree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "conflict.txt"], check=True)

    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "worktree",
            "merge-back",
            "--wave",
            "P05-I01-W01",
            "--continue",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    # The continue envelope mirrors the success-shape: status string is
    # the WorktreeStatus value (``merged``), strategy is preserved, and
    # the conflict block is gone.
    assert envelope["status"] == "merged"
    assert envelope["strategy"] == "cherry_pick"
    assert "conflict" not in envelope
    state = orjson.loads(state_path.read_bytes())
    record_id = next(iter(state["worktrees"]))
    assert state["worktrees"][record_id]["status"] == "merged"


def test_cli_merge_back_abort_envelope(tmp_path: Path) -> None:
    """``--abort`` after a conflict marks the record ABANDONED and clears merged_commit."""
    repo, state_path = _seed_repo_with_state(tmp_path / "repo")
    _trigger_conflict(repo, state_path)

    res = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(repo),
            "worktree",
            "merge-back",
            "--wave",
            "P05-I01-W01",
            "--abort",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    envelope = json.loads(res.stdout)
    # Abort envelope shape mirrors the success branch (no ``conflict``
    # block) but the recorded WorktreeStatus is ``abandoned`` and the
    # merged_commit is cleared.
    assert envelope["status"] == "abandoned"
    assert "conflict" not in envelope
    assert envelope.get("merged_commit") is None
    state = orjson.loads(state_path.read_bytes())
    record_id = next(iter(state["worktrees"]))
    assert state["worktrees"][record_id]["status"] == "abandoned"
