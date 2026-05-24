"""Unit tests for :func:`eawf.runtime.worktree.wave_land.wave_land` (B027).

The tests build a tmp git repo with a parent feature branch and a
worktree branch carrying one or more commits, then exercise the
wave-centric automation: cherry-pick replay, close-wave state
transition, optional cleanup, and the batch driver's dep-order
behaviour.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.kernel.state.enums import WaveStatus, WorktreeStatus
from eawf.kernel.state.models import (
    Iter,
    Wave,
)
from eawf.runtime.worktree.create import create_worktree
from eawf.runtime.worktree.wave_land import (
    wave_land,
    wave_land_batch,
)
from tests.unit.test_worktree_create import _claimed_state, _make_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for wave land tests",
)


_DT = datetime(2026, 5, 9, tzinfo=UTC)


def _commit_in(worktree: Path, *, name: str, content: str, msg: str) -> str:
    """Make one commit in *worktree* and return its short sha."""
    target = worktree / name
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m", msg], check=True)
    return subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_wave_land_runs_cherry_pick_and_closes_wave(tmp_path: Path) -> None:
    """Happy path: cherry-pick lands and the wave transitions to closed."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    sha = _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")

    result = wave_land(state, repo_root=repo, wave_id="P05-I01-W01")

    assert state.waves["P05-I01-W01"].status == WaveStatus.CLOSED
    assert result.commits
    assert sha != ""  # sanity: original commit existed
    assert state.worktrees is not None
    # merge_back marked the record MERGED on the success branch;
    # cleanup_worktree leaves MERGED records as MERGED (it only
    # transitions ACTIVE → ABANDONED for force-teardown).
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED
    assert result.worktree_cleaned is True
    # Cleanup ran -> on-disk worktree directory is gone.
    assert not (repo / record.path).exists()


def test_wave_land_default_outcome_text(tmp_path: Path) -> None:
    """When no --outcome is given, a synthesised summary is stamped."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")

    result = wave_land(state, repo_root=repo, wave_id="P05-I01-W01")

    assert "landed" in result.outcome
    assert "commit" in result.outcome
    # The default text mentions the count of commits landed.
    assert str(len(result.commits)) in result.outcome
    assert state.waves["P05-I01-W01"].outcome == result.outcome


def test_wave_land_keep_worktree_skips_cleanup(tmp_path: Path) -> None:
    """``keep_worktree=True`` leaves the worktree directory in place."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")

    result = wave_land(
        state,
        repo_root=repo,
        wave_id="P05-I01-W01",
        keep_worktree=True,
    )

    assert result.worktree_cleaned is False
    assert result.cleanup is None
    assert (repo / record.path).exists()
    # The worktree record remains MERGED (cleanup did not run).
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED


def test_wave_land_no_worktree_raises(tmp_path: Path) -> None:
    """A wave with no worktree_id stamped surfaces :class:`NotFound`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    # Do NOT call create_worktree — leave worktree_id=None.
    with pytest.raises(cli_errors.UserError) as exc_info:
        wave_land(state, repo_root=repo, wave_id="P05-I01-W01")
    # The error path goes via merge_back which surfaces "no worktree id stamped".
    assert "worktree" in str(exc_info.value).lower()


def test_wave_land_conflict_does_not_close_wave(tmp_path: Path) -> None:
    """A cherry-pick conflict refuses to close the wave."""
    repo = _make_repo(tmp_path / "repo")
    # Add `conflict.txt` on the parent first so the worktree can diverge.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)

    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in(
        (repo / record.path),
        name="conflict.txt",
        content="worktree\n",
        msg="worktree edit",
    )
    # Move parent forward so cherry-pick will see the divergence.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    with pytest.raises(cli_errors.StateConflict) as exc_info:
        wave_land(state, repo_root=repo, wave_id="P05-I01-W01")
    # The hint references the repair procedure.
    msg = str(exc_info.value)
    assert "conflict" in msg.lower()
    assert "wave land P05-I01-W01" in msg or "merge-back" in msg
    # The wave must remain in its pre-call status (CLAIMED in this fixture).
    assert state.waves["P05-I01-W01"].status == WaveStatus.CLAIMED
    # The worktree record must be CONFLICTED (preserved evidence).
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.CONFLICTED


def test_wave_land_batch_runs_in_dep_order(tmp_path: Path) -> None:
    """``wave_land_batch`` lands deps before dependents in topo order."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    # Add a second iter-mate wave that depends on W01.
    state.waves["P05-I01-W02"] = Wave(
        id="P05-I01-W02",
        iter_id="P05-I01",
        title="W2",
        status=WaveStatus.CLAIMED,
        deps=["P05-I01-W01"],
        file_scopes=["src/eawf/dispatch/"],
        claim_session_id="SES-002",
        opened_at=_DT,
    )
    # Surface the new wave on the parent iter so the model stays consistent.
    iter_obj: Iter = state.iters["P05-I01"]
    iter_obj.wave_ids = ["P05-I01-W01", "P05-I01-W02"]

    record_a = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record_a.path), name="hello.txt", content="x\n", msg="add hello")

    record_b = create_worktree(state, repo_root=repo, wave_id="P05-I01-W02")
    _commit_in((repo / record_b.path), name="goodbye.txt", content="y\n", msg="add goodbye")

    batch_result = wave_land_batch(state, repo_root=repo)

    assert batch_result.failed_wave is None
    assert [r.wave_id for r in batch_result.landed] == ["P05-I01-W01", "P05-I01-W02"]
    # Both waves landed and closed.
    assert state.waves["P05-I01-W01"].status == WaveStatus.CLOSED
    assert state.waves["P05-I01-W02"].status == WaveStatus.CLOSED


def test_wave_land_batch_stops_on_first_failure(tmp_path: Path) -> None:
    """When a wave conflicts mid-batch, the batch halts and reports it."""
    repo = _make_repo(tmp_path / "repo")
    # Seed a conflict.txt on parent.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)

    state = _claimed_state()
    state.waves["P05-I01-W02"] = Wave(
        id="P05-I01-W02",
        iter_id="P05-I01",
        title="W2",
        status=WaveStatus.CLAIMED,
        deps=["P05-I01-W01"],
        file_scopes=["src/eawf/dispatch/"],
        claim_session_id="SES-002",
        opened_at=_DT,
    )
    iter_obj: Iter = state.iters["P05-I01"]
    iter_obj.wave_ids = ["P05-I01-W01", "P05-I01-W02"]

    record_a = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in(
        (repo / record_a.path),
        name="conflict.txt",
        content="wt-a\n",
        msg="wt-a edit",
    )
    record_b = create_worktree(state, repo_root=repo, wave_id="P05-I01-W02")
    _commit_in((repo / record_b.path), name="goodbye.txt", content="y\n", msg="add goodbye")

    # Move the parent forward so W01's worktree commit will conflict.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    batch_result = wave_land_batch(state, repo_root=repo)

    assert batch_result.failed_wave == "P05-I01-W01"
    assert batch_result.error is not None
    assert batch_result.landed == []
    # W02 must remain pre-batch (CLAIMED), since the batch halted at W01.
    assert state.waves["P05-I01-W02"].status == WaveStatus.CLAIMED


def test_wave_land_unknown_wave_raises_not_found(tmp_path: Path) -> None:
    """An unknown wave id surfaces :class:`NotFound` immediately."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    with pytest.raises(cli_errors.UserError):
        wave_land(state, repo_root=repo, wave_id="P99-I99-W99")


def test_wave_land_already_closed_wave_raises_validation_failed(tmp_path: Path) -> None:
    """Closing-a-closed wave surfaces :class:`ValidationFailed`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    # Synthesise a closed wave.
    state.waves["P05-I01-W01"].status = WaveStatus.CLOSED
    state.waves["P05-I01-W01"].outcome = "previously closed"
    with pytest.raises(cli_errors.ValidationError) as exc_info:
        wave_land(state, repo_root=repo, wave_id="P05-I01-W01")
    assert "claimed/in_progress" in str(exc_info.value)
