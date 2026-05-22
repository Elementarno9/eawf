"""Coverage-lift tests for :mod:`eawf.worktree.git` (P27-I01-W01).

Two complementary strategies:

* **Monkeypatched ``subprocess.run``** drives the per-helper error-mapping
  branches (well-known stderr markers -> canonical CliError subclasses)
  without spinning up a real repo. This is the only practical way to hit
  branches like "timeout -> IntegrityViolation" deterministically.
* **The real :func:`dirty_repo` fixture** (a temp git repo left dirty)
  exercises the error-class paths against actual ``git`` so the mapping
  is validated end-to-end, not just against synthetic stderr.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.cli import errors as cli_errors
from eawf.worktree import git
from tests.fixtures.conftest import make_dirty_repo

_FAKE_GIT = "/usr/bin/git"


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """A real, dirty git repo (delegates to the shared fixtures builder).

    ``tests/fixtures/conftest.py`` is not on this module's conftest
    ancestry, so we re-expose the shared builder as a local fixture.
    """
    return make_dirty_repo(tmp_path)


def _patch_git_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.worktree.git.shutil.which", lambda _name: _FAKE_GIT)


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Patch ``subprocess.run`` to return a canned CompletedProcess."""
    _patch_git_present(monkeypatch)

    def _fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _fake_run)


# --- _ensure_git / _run --------------------------------------------------


def test_ensure_git_missing_raises_instrument_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.worktree.git.shutil.which", lambda _name: None)
    with pytest.raises(cli_errors.InstrumentMissing, match="git executable not found"):
        git._ensure_git()


def test_run_timeout_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_git_present(monkeypatch)

    def _raise_timeout(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=1.0)

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _raise_timeout)
    with pytest.raises(cli_errors.IntegrityViolation, match="timed out"):
        git._run(["git", "-C", str(tmp_path), "status"])


# --- repo_root -----------------------------------------------------------


def test_repo_root_outside_repo_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: not a git repository")
    with pytest.raises(cli_errors.NotFound, match="not a git repository"):
        git.repo_root(tmp_path)


def test_repo_root_clean_returns_toplevel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0, stdout=f"{tmp_path}\n")
    assert git.repo_root(tmp_path) == tmp_path


def test_repo_root_real_dirty_repo_returns_root(dirty_repo: Path) -> None:
    assert git.repo_root(dirty_repo) == dirty_repo.resolve()


# --- branch_exists -------------------------------------------------------


def test_branch_exists_nonzero_rc_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=129, stderr="usage: git branch")
    assert git.branch_exists(tmp_path, "feature/foo") is False


def test_branch_exists_real_repo_known_branch(dirty_repo: Path) -> None:
    assert git.branch_exists(dirty_repo, "main") is True
    assert git.branch_exists(dirty_repo, "no/such/branch") is False


# --- current_branch ------------------------------------------------------


def test_current_branch_real_repo_returns_feature(dirty_repo: Path) -> None:
    assert git.current_branch(dirty_repo) == "feature/eawf-v0.1"


# --- worktree_add error mapping -----------------------------------------


def test_worktree_add_already_in_use_maps_to_lock_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: 'x' is already used by worktree")
    with pytest.raises(cli_errors.LockConflict, match="already"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main")


def test_worktree_add_invalid_reference_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: invalid reference: nope")
    with pytest.raises(cli_errors.InvalidInput, match="invalid reference"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="nope")


def test_worktree_add_not_a_repo_maps_to_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: not a git repository")
    with pytest.raises(cli_errors.NotFound, match="not a git repository"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main")


def test_worktree_add_unknown_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="some other git boom")
    with pytest.raises(cli_errors.IntegrityViolation, match="worktree add failed"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main")


def test_worktree_add_success_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main") is None


def test_worktree_add_real_invalid_base_raises_invalid_input(dirty_repo: Path) -> None:
    """Real git: adding a worktree off a non-existent base raises InvalidInput."""
    with pytest.raises(cli_errors.InvalidInput):
        git.worktree_add(
            dirty_repo,
            branch="feature/new",
            path=dirty_repo.parent / "wt-new",
            base="does-not-exist-ref",
        )


# --- worktree_remove -----------------------------------------------------


def test_worktree_remove_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="fatal: cannot remove")
    with pytest.raises(cli_errors.IntegrityViolation, match="worktree remove failed"):
        git.worktree_remove(tmp_path, path=tmp_path / "wt")


def test_worktree_remove_success_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.worktree_remove(tmp_path, path=tmp_path / "wt", force=True) is None


def test_worktree_remove_real_missing_path_raises(dirty_repo: Path) -> None:
    with pytest.raises(cli_errors.IntegrityViolation):
        git.worktree_remove(dirty_repo, path=dirty_repo / "no-such-worktree")


# --- worktree_list -------------------------------------------------------


def test_worktree_list_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="boom")
    with pytest.raises(cli_errors.IntegrityViolation, match="worktree list failed"):
        git.worktree_list(tmp_path)


def test_worktree_list_parses_multiple_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two entries, the last with no trailing blank line, both parse."""
    stdout = (
        "worktree /repo\nHEAD abc123\nbranch refs/heads/main\nbare\n\n"
        "worktree /repo/wt\nHEAD def456\nbranch refs/heads/feature\n"
    )
    _patch_run(monkeypatch, returncode=0, stdout=stdout)
    entries = git.worktree_list(tmp_path)
    assert len(entries) == 2
    assert entries[0]["worktree"] == "/repo"
    assert entries[0]["bare"] == ""
    assert entries[1]["branch"] == "refs/heads/feature"


def test_worktree_list_real_repo_has_main_entry(dirty_repo: Path) -> None:
    entries = git.worktree_list(dirty_repo)
    assert any(Path(e.get("worktree", "")) == dirty_repo for e in entries)


# --- status_porcelain ----------------------------------------------------


def test_status_porcelain_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="fatal: not a git repository")
    with pytest.raises(cli_errors.IntegrityViolation, match="status failed"):
        git.status_porcelain(tmp_path)


def test_status_porcelain_real_dirty_repo_reports_entries(dirty_repo: Path) -> None:
    rows = git.status_porcelain(dirty_repo)
    assert any("tracked.txt" in r for r in rows)
    assert any("untracked.txt" in r for r in rows)


# --- rev_list ------------------------------------------------------------


def test_rev_list_bad_revision_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: bad revision 'x..y'")
    with pytest.raises(cli_errors.InvalidInput, match="rev-list"):
        git.rev_list(tmp_path, range_spec="x..y")


def test_rev_list_unknown_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="boom")
    with pytest.raises(cli_errors.IntegrityViolation, match="rev-list failed"):
        git.rev_list(tmp_path, range_spec="a..b")


def test_rev_list_success_returns_shas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0, stdout="aaa\nbbb\n")
    assert git.rev_list(tmp_path, range_spec="a..b") == ["aaa", "bbb"]


# --- cherry_pick ---------------------------------------------------------


def test_cherry_pick_clean_returns_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.cherry_pick(tmp_path, sha="abc") == (True, "")


def test_cherry_pick_conflict_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="error: could not apply abc... msg")
    clean, detail = git.cherry_pick(tmp_path, sha="abc")
    assert clean is False
    assert "could not apply" in detail


def test_cherry_pick_bad_revision_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: bad revision 'zzz'")
    with pytest.raises(cli_errors.InvalidInput, match="cherry-pick"):
        git.cherry_pick(tmp_path, sha="zzz")


def test_cherry_pick_unknown_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="some weird failure")
    with pytest.raises(cli_errors.IntegrityViolation, match="cherry-pick failed"):
        git.cherry_pick(tmp_path, sha="abc")


def test_cherry_pick_now_empty_auto_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 'now empty' pick triggers a ``--skip`` and reports clean."""
    _patch_git_present(monkeypatch)
    calls: list[list[str]] = []

    def _fake_run(args: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "--skip" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="The previous cherry-pick is now empty"
        )

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _fake_run)
    assert git.cherry_pick(tmp_path, sha="abc") == (True, "")
    assert any("--skip" in c for c in calls)


def test_cherry_pick_now_empty_skip_fails_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the auto ``--skip`` itself fails, the pick raises IntegrityViolation."""
    _patch_git_present(monkeypatch)

    def _fake_run(args: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        if "--skip" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="skip boom"
            )
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="previous cherry-pick is now empty"
        )

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _fake_run)
    with pytest.raises(cli_errors.IntegrityViolation):
        git.cherry_pick(tmp_path, sha="abc")


# --- cherry_pick_continue ------------------------------------------------


def test_cherry_pick_continue_clean_returns_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.cherry_pick_continue(tmp_path) == (True, "")


def test_cherry_pick_continue_conflict_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="still conflicted")
    clean, detail = git.cherry_pick_continue(tmp_path)
    assert clean is False
    assert "conflicted" in detail


def test_cherry_pick_continue_now_empty_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_git_present(monkeypatch)

    def _fake_run(args: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        if "--skip" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="now empty")

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _fake_run)
    assert git.cherry_pick_continue(tmp_path) == (True, "")


def test_cherry_pick_continue_now_empty_skip_fails_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_git_present(monkeypatch)

    def _fake_run(args: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        if "--skip" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="skip failed"
            )
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="now empty")

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _fake_run)
    clean, detail = git.cherry_pick_continue(tmp_path)
    assert clean is False
    assert "skip failed" in detail


# --- cherry_pick_abort ---------------------------------------------------


def test_cherry_pick_abort_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="nothing to abort")
    with pytest.raises(cli_errors.IntegrityViolation, match="cherry-pick --abort failed"):
        git.cherry_pick_abort(tmp_path)


def test_cherry_pick_abort_success_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.cherry_pick_abort(tmp_path) is None


# --- rebase --------------------------------------------------------------


def test_rebase_clean_returns_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.rebase(tmp_path, target="main") == (True, "")


def test_rebase_conflict_returns_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="CONFLICT (content): Merge conflict in x")
    clean, detail = git.rebase(tmp_path, target="main")
    assert clean is False
    assert "conflict" in detail.lower()


def test_rebase_invalid_upstream_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: invalid upstream 'nope'")
    with pytest.raises(cli_errors.InvalidInput, match="rebase"):
        git.rebase(tmp_path, target="nope")


def test_rebase_unknown_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="weird rebase boom")
    with pytest.raises(cli_errors.IntegrityViolation, match="rebase failed"):
        git.rebase(tmp_path, target="main")


# --- rebase_continue / rebase_abort -------------------------------------


def test_rebase_continue_clean_returns_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.rebase_continue(tmp_path) == (True, "")


def test_rebase_continue_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="still rebasing")
    clean, detail = git.rebase_continue(tmp_path)
    assert clean is False
    assert "rebasing" in detail


def test_rebase_abort_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="nothing to abort")
    with pytest.raises(cli_errors.IntegrityViolation, match="rebase --abort failed"):
        git.rebase_abort(tmp_path)


def test_rebase_abort_success_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.rebase_abort(tmp_path) is None


# --- merge_ff_only -------------------------------------------------------


def test_merge_ff_only_non_fast_forward_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: Not possible to fast-forward, aborting.")
    with pytest.raises(cli_errors.IntegrityViolation, match="non-fast-forward"):
        git.merge_ff_only(tmp_path, source="feature")


def test_merge_ff_only_unknown_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="weird merge boom")
    with pytest.raises(cli_errors.IntegrityViolation, match="merge --ff-only failed"):
        git.merge_ff_only(tmp_path, source="feature")


def test_merge_ff_only_success_returns_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_git_present(monkeypatch)

    def _fake_run(args: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="cafef00d\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("eawf.worktree.git.subprocess.run", _fake_run)
    assert git.merge_ff_only(tmp_path, source="feature") == "cafef00d"


# --- head_sha ------------------------------------------------------------


def test_head_sha_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: ambiguous argument 'HEAD'")
    with pytest.raises(cli_errors.IntegrityViolation, match="rev-parse HEAD failed"):
        git.head_sha(tmp_path)


def test_head_sha_real_dirty_repo_returns_short_sha(dirty_repo: Path) -> None:
    sha = git.head_sha(dirty_repo)
    assert sha
    assert len(sha) >= 7


# --- branch_delete -------------------------------------------------------


def test_branch_delete_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="error: branch 'x' not found")
    assert git.branch_delete(tmp_path, name="x") is False


def test_branch_delete_success_returns_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.branch_delete(tmp_path, name="feature/foo") is True


# --- cherry_pick_in_progress / rebase_in_progress -----------------------


def test_cherry_pick_in_progress_true_when_marker_present(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "CHERRY_PICK_HEAD").write_text("abc\n", encoding="utf-8")
    assert git.cherry_pick_in_progress(tmp_path) is True


def test_cherry_pick_in_progress_false_when_absent(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert git.cherry_pick_in_progress(tmp_path) is False


def test_rebase_in_progress_plain_git_dir_true(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "rebase-merge").mkdir(parents=True)
    assert git.rebase_in_progress(tmp_path) is True


def test_rebase_in_progress_plain_git_dir_false(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert git.rebase_in_progress(tmp_path) is False


def test_rebase_in_progress_gitfile_pointer_resolves(tmp_path: Path) -> None:
    """A ``.git`` *file* pointing at the real gitdir is followed for the marker."""
    real_gitdir = tmp_path / "realgit"
    (real_gitdir / "rebase-apply").mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real_gitdir}\n", encoding="utf-8")
    assert git.rebase_in_progress(worktree) is True


def test_rebase_in_progress_gitfile_relative_pointer(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "nested").mkdir()
    (worktree / "nested" / "rebase-merge").mkdir()
    (worktree / ".git").write_text("gitdir: nested\n", encoding="utf-8")
    assert git.rebase_in_progress(worktree) is True


def test_rebase_in_progress_gitfile_unreadable_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /somewhere\n", encoding="utf-8")

    def _boom(*_a: Any, **_k: Any) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert git.rebase_in_progress(worktree) is False
