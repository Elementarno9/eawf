"""Unit tests for the delete step of :func:`prune_branches`.

The git seam is replaced by an in-memory ref store whose ``update-ref
--stdin`` honours the ``delete <ref> <old>`` expected-value check as one
transaction, the way git does, and whose ``branch -D`` deletes
unconditionally. That difference is the whole point: a branch whose tip
moves after selection must survive a prune, which only the guarded
transaction guarantees. The real-git counterpart lives in
``tests/integration/workflow/lifecycle/test_wave_branch_prune.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.workflow.lifecycle import wave_archive
from eawf.workflow.lifecycle.wave_archive import (
    WaveArchiveError,
    WaveBranchMovedError,
    archive_ref_for_branch,
    misc_archive_ref_for_branch,
    prune_branches,
)

pytestmark = pytest.mark.unit

_OLD = "a" * 40
_NEW = "b" * 40
_WAVE = "feature/zz-v1.0-p28-w01"
_PHASE = "feature/zz-v1.0"


class FakeRepo:
    """An in-memory stand-in for the refs and worktrees prune reads and writes."""

    def __init__(self, heads: dict[str, str], *, current: str | None = "main") -> None:
        self.heads = dict(heads)
        self.current = current
        self.archive: dict[str, str] = {}
        self.move_on_worktree_scan: dict[str, str] = {}
        self.fail_transaction = False

    def archive_all(self) -> None:
        for branch, head in self.heads.items():
            ref = archive_ref_for_branch(branch) or misc_archive_ref_for_branch(branch)
            self.archive[ref] = head

    def refs(self, pattern: str, *, repo_root: Path | None) -> dict[str, str]:
        if pattern == "refs/heads":
            return {f"refs/heads/{b}": h for b, h in self.heads.items()}
        return {ref: head for ref, head in self.archive.items() if ref.startswith(pattern)}

    def current_branch(self, *, repo_root: Path | None) -> str | None:
        return self.current

    def worktree_entries(self, *, repo_root: Path | None) -> list[str]:
        if self.current is None:
            return []
        return ["worktree /repo", f"branch refs/heads/{self.current}"]

    def prunable_worktrees(self, *, repo_root: Path | None) -> list[str]:
        # Runs after selection and before the delete: the window a
        # concurrent commit on a candidate branch lands in.
        self.heads.update(self.move_on_worktree_scan)
        return []

    def mutate(
        self,
        args: list[str],
        *,
        repo_root: Path | None,
        input_text: str | None = None,
        label: str,
    ) -> None:
        if args[:2] == ["branch", "-D"]:
            self.heads.pop(args[2])
            return
        assert args == ["update-ref", "--stdin"], args
        assert input_text is not None
        deletes = [line.split(" ") for line in input_text.splitlines() if line]
        if self.fail_transaction:
            raise WaveArchiveError(f"{label} failed: fatal: unable to lock")
        for verb, ref, old in deletes:
            assert verb == "delete"
            if self.heads.get(ref.removeprefix("refs/heads/")) != old:
                raise WaveArchiveError(f"{label} failed: fatal: cannot lock ref '{ref}'")
        for _verb, ref, _old in deletes:
            del self.heads[ref.removeprefix("refs/heads/")]


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fake = FakeRepo({"main": _OLD, _WAVE: _OLD, "worktree-agent-1": _OLD})
    monkeypatch.setattr(wave_archive, "_refs", fake.refs)
    monkeypatch.setattr(wave_archive, "_current_branch", fake.current_branch)
    monkeypatch.setattr(wave_archive, "_worktree_entries", fake.worktree_entries)
    monkeypatch.setattr(wave_archive, "_prunable_worktrees", fake.prunable_worktrees)
    monkeypatch.setattr(wave_archive, "_run_git_mutation", fake.mutate)
    return fake


def test_prune_branches_refuses_a_branch_that_moved_after_archiving(repo: FakeRepo) -> None:
    repo.archive_all()
    repo.move_on_worktree_scan = {_WAVE: _NEW}

    with pytest.raises(WaveBranchMovedError) as excinfo:
        prune_branches()

    assert excinfo.value.moved == [(_WAVE, _OLD)]
    assert "branch tip moved" in str(excinfo.value)
    # One transaction: the unmoved candidate survives alongside the moved one.
    assert repo.heads == {"main": _OLD, _WAVE: _NEW, "worktree-agent-1": _OLD}


def test_prune_branches_deletes_every_archived_branch_at_its_head(repo: FakeRepo) -> None:
    repo.archive_all()

    result = prune_branches()

    assert sorted(c.branch for c in result.deleted) == [_WAVE, "worktree-agent-1"]
    assert repo.heads == {"main": _OLD}


def test_prune_branches_with_no_candidates_writes_nothing(
    repo: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.heads = {"main": _OLD}

    def no_mutation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an empty prune must not touch git")

    monkeypatch.setattr(wave_archive, "_run_git_mutation", no_mutation)
    result = prune_branches()

    assert result.deleted == []
    assert result.skipped == [("main", "checked out (current branch)")]


def test_prune_branches_reraises_a_transaction_failure_with_no_moved_branch(
    repo: FakeRepo,
) -> None:
    repo.archive_all()
    repo.fail_transaction = True

    with pytest.raises(WaveArchiveError, match="unable to lock") as excinfo:
        prune_branches()

    assert not isinstance(excinfo.value, WaveBranchMovedError)
    assert _WAVE in repo.heads


@pytest.mark.parametrize("phase_branch", [_PHASE, "feature/eawf-v0.7-p35", "feature/x-v1.2.3"])
def test_prune_branches_protects_a_phase_branch_not_checked_out(
    repo: FakeRepo, phase_branch: str
) -> None:
    repo.heads[phase_branch] = _OLD
    repo.archive_all()

    result = prune_branches()

    assert (phase_branch, "phase branch") in result.skipped
    assert phase_branch in repo.heads


@pytest.mark.parametrize(
    "branch", ["feature/zz-v1.0-p28-w01", "feature/eawf-v0.7-p35-x", "feature/zz", "zz-v1.0"]
)
def test_prune_branches_does_not_treat_other_branches_as_phase_branches(
    repo: FakeRepo, branch: str
) -> None:
    repo.heads[branch] = _OLD
    repo.archive_all()

    result = prune_branches()

    assert branch in {c.branch for c in result.deleted}
