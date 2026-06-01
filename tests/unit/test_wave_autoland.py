"""Unit tests for :func:`eawf.runtime.worktree.autoland.wave_autoland`.

The land back-half cherry-picks *already-closed* waves' worktree commits
onto the parent feature branch in dependency order, stopping on the
first conflict. These tests build a tmp git repo with a parent feature
branch and one or more worktree branches carrying commits, flip the
waves to CLOSED (the worktree record stays ACTIVE), then exercise:

- empty land set -> no-op,
- single landable wave,
- multi-wave dep-ordered land (order asserted),
- a mid-sequence conflict that STOPS and reports the remaining tail,
- ``dry_run`` planning that mutates nothing.

The fixtures reuse the worktree-create harness (``_make_repo`` /
``_claimed_state``).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import WaveStatus, WorktreeStatus
from eawf.kernel.state.models import Iter, State, Wave
from eawf.runtime.worktree.autoland import wave_autoland
from eawf.runtime.worktree.create import create_worktree
from tests.unit.test_worktree_create import _claimed_state, _make_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for wave autoland tests",
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


def _add_wave(state: State, *, wave_id: str, deps: list[str]) -> None:
    """Append a CLAIMED iter-mate wave to the shared P05-I01 fixture iter.

    The wave starts CLAIMED so :func:`create_worktree` accepts it; the
    caller flips it to CLOSED via :func:`_close_wave` once its worktree
    carries commits (mirroring the real flow: close happens in-worktree
    before autoland brings the commits home).
    """
    state.waves[wave_id] = Wave(
        id=wave_id,
        iter_id="P05-I01",
        title=wave_id,
        status=WaveStatus.CLAIMED,
        deps=deps,
        file_scopes=["src/eawf/runtime/worktree/"],
        claim_session_id="SES-002",
        opened_at=_DT,
    )
    iter_obj: Iter = state.iters["P05-I01"]
    if wave_id not in iter_obj.wave_ids:
        iter_obj.wave_ids = [*iter_obj.wave_ids, wave_id]


def _close_wave(state: State, wave_id: str) -> None:
    """Flip a CLAIMED fixture wave to CLOSED (worktree record stays ACTIVE)."""
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLOSED
    wave.outcome = "closed in worktree"


def test_wave_autoland_empty_set_is_noop(tmp_path: Path) -> None:
    """No closed-with-commits waves -> exit cleanly with nothing landed."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    # The lone fixture wave is CLAIMED, not CLOSED, so it is not landable.
    result = wave_autoland(state, repo_root=repo, iter_id="P05-I01")

    assert result.order == []
    assert result.landed == []
    assert result.failed_wave is None
    assert result.remaining == []


def test_wave_autoland_single_landable_wave(tmp_path: Path) -> None:
    """One closed wave with commits lands and the worktree is torn down."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")
    _close_wave(state, "P05-I01-W01")

    result = wave_autoland(state, repo_root=repo, iter_id="P05-I01")

    assert result.order == ["P05-I01-W01"]
    assert [row.wave_id for row in result.landed] == ["P05-I01-W01"]
    assert result.landed[0].commits  # at least one sha replayed
    assert result.landed[0].worktree_cleaned is True
    assert result.failed_wave is None
    assert result.remaining == []
    # The commit is now on the parent branch.
    assert (repo / "hello.txt").exists()
    # The worktree record transitioned MERGED; the directory is gone.
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED
    assert not (repo / record.path).exists()


def test_wave_autoland_keep_worktree_skips_teardown(tmp_path: Path) -> None:
    """``keep_worktree=True`` lands commits but leaves the worktree in place."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")
    _close_wave(state, "P05-I01-W01")

    result = wave_autoland(state, repo_root=repo, iter_id="P05-I01", keep_worktree=True)

    assert result.landed[0].worktree_cleaned is False
    assert (repo / record.path).exists()
    # merge_back still marks the record MERGED even when cleanup is skipped.
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED


def test_wave_autoland_runs_in_dep_order(tmp_path: Path) -> None:
    """Deps land before dependents; ties break by wave id ascending."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    _add_wave(state, wave_id="P05-I01-W02", deps=["P05-I01-W01"])
    _add_wave(state, wave_id="P05-I01-W03", deps=["P05-I01-W02"])

    record_1 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record_1.path), name="a.txt", content="a\n", msg="add a")
    record_2 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W02")
    _commit_in((repo / record_2.path), name="b.txt", content="b\n", msg="add b")
    record_3 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W03")
    _commit_in((repo / record_3.path), name="c.txt", content="c\n", msg="add c")
    for wid in ("P05-I01-W01", "P05-I01-W02", "P05-I01-W03"):
        _close_wave(state, wid)

    result = wave_autoland(state, repo_root=repo, iter_id="P05-I01")

    assert result.order == ["P05-I01-W01", "P05-I01-W02", "P05-I01-W03"]
    assert [row.wave_id for row in result.landed] == [
        "P05-I01-W01",
        "P05-I01-W02",
        "P05-I01-W03",
    ]
    assert result.failed_wave is None
    assert result.remaining == []
    # All three commits landed on the parent branch.
    assert (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()
    assert (repo / "c.txt").exists()


def test_wave_autoland_stops_on_conflict_and_reports_remaining(tmp_path: Path) -> None:
    """A mid-sequence conflict halts the land and reports the un-landed tail."""
    repo = _make_repo(tmp_path / "repo")
    # Seed conflict.txt on the parent so W02's edit can diverge.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)

    state = _claimed_state()
    _add_wave(state, wave_id="P05-I01-W02", deps=["P05-I01-W01"])
    _add_wave(state, wave_id="P05-I01-W03", deps=["P05-I01-W02"])

    record_1 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record_1.path), name="a.txt", content="a\n", msg="add a")
    record_2 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W02")
    _commit_in((repo / record_2.path), name="conflict.txt", content="wt-2\n", msg="wt-2 edit")
    record_3 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W03")
    _commit_in((repo / record_3.path), name="c.txt", content="c\n", msg="add c")
    for wid in ("P05-I01-W01", "P05-I01-W02", "P05-I01-W03"):
        _close_wave(state, wid)

    # Move the parent forward so W02's conflict.txt commit conflicts.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    result = wave_autoland(state, repo_root=repo, iter_id="P05-I01")

    # W01 landed; W02 conflicted; W03 was never reached.
    assert [row.wave_id for row in result.landed] == ["P05-I01-W01"]
    assert result.failed_wave == "P05-I01-W02"
    assert result.error is not None
    assert "conflict" in result.error.lower()
    assert result.remaining == ["P05-I01-W02", "P05-I01-W03"]
    # The conflicted worktree record preserves evidence.
    assert state.worktrees is not None
    assert state.worktrees[record_2.id].status == WorktreeStatus.CONFLICTED
    # W03's worktree is untouched (still ACTIVE) because the land stopped.
    assert state.worktrees[record_3.id].status == WorktreeStatus.ACTIVE


def test_wave_autoland_dry_run_prints_order_without_mutating(tmp_path: Path) -> None:
    """``dry_run`` returns the planned order and touches nothing."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    _add_wave(state, wave_id="P05-I01-W02", deps=["P05-I01-W01"])

    record_1 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record_1.path), name="a.txt", content="a\n", msg="add a")
    record_2 = create_worktree(state, repo_root=repo, wave_id="P05-I01-W02")
    _commit_in((repo / record_2.path), name="b.txt", content="b\n", msg="add b")
    _close_wave(state, "P05-I01-W01")
    _close_wave(state, "P05-I01-W02")

    result = wave_autoland(state, repo_root=repo, iter_id="P05-I01", dry_run=True)

    assert result.dry_run is True
    assert result.order == ["P05-I01-W01", "P05-I01-W02"]
    assert result.landed == []
    assert result.failed_wave is None
    # Nothing landed: parent branch does NOT have the worktree files, and
    # both worktree records remain ACTIVE with their directories intact.
    assert not (repo / "a.txt").exists()
    assert not (repo / "b.txt").exists()
    assert state.worktrees is not None
    assert state.worktrees[record_1.id].status == WorktreeStatus.ACTIVE
    assert state.worktrees[record_2.id].status == WorktreeStatus.ACTIVE
    assert (repo / record_1.path).exists()
    assert (repo / record_2.path).exists()


def test_wave_autoland_skips_already_merged_worktree(tmp_path: Path) -> None:
    """A closed wave whose worktree is already MERGED is not re-landed."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")
    _close_wave(state, "P05-I01-W01")
    # First land brings the commit home and tears down the worktree.
    first = wave_autoland(state, repo_root=repo, iter_id="P05-I01")
    assert first.landed

    # A repeat autoland sees the MERGED record and treats it as a no-op.
    second = wave_autoland(state, repo_root=repo, iter_id="P05-I01")
    assert second.order == []
    assert second.landed == []


def test_wave_autoland_defaults_to_current_iter(tmp_path: Path) -> None:
    """With no explicit iter, the current-iter pointer scopes the land."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    state.current.phase_id = "P05"
    state.current.iter_id = "P05-I01"
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in((repo / record.path), name="hello.txt", content="x\n", msg="add hello")
    _close_wave(state, "P05-I01-W01")

    result = wave_autoland(state, repo_root=repo)

    assert result.order == ["P05-I01-W01"]
    assert [row.wave_id for row in result.landed] == ["P05-I01-W01"]
