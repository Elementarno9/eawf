"""Close-workspace removal for directories Git no longer lists.

A close workspace can outlive its Git admin entry (a prune, or a creation
that stopped half way). ``git worktree remove`` refuses such a directory, so
cleanup falls back to deleting it, confined to the attempt directory.
Every repository here lives under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import CloseAttemptStatus
from eawf.kernel.state.models import State
from eawf.runtime.daemon.close_workspace import (
    CloseWorkspaceError,
    cleanup_close_workspace,
    prepare_close_workspace,
    resolve_exact_revision,
    workspace_path,
)
from eawf.runtime.daemon.methods import close as close_module
from eawf.runtime.daemon.methods.close import _run_attempt, submit
from tests.integration.runtime.daemon.test_close_lock_split import _WAVE
from tests.integration.runtime.daemon.test_close_workspace import _git, _repo
from tests.integration.runtime.daemon.test_durable_close import _repo_with_state

pytestmark = pytest.mark.integration


def _close_root(repo: Path) -> Path:
    return repo / ".ea" / "worktrees" / "close"


def _listed(repo: Path) -> set[Path]:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }


def _forget_worktree(repo: Path, path: Path) -> None:
    """Drop Git's admin entry for *path* so Git stops listing the directory."""
    admin_root = repo / ".git" / "worktrees"
    target = (path / ".git").resolve()
    for admin in admin_root.iterdir():
        gitdir = Path((admin / "gitdir").read_text(encoding="utf-8").strip())
        if gitdir.resolve() == target:
            shutil.rmtree(admin)
            break
    else:
        raise AssertionError(f"no admin entry for {path.name}")
    assert path.resolve() not in _listed(repo)


def _populate(path: Path, marker: str) -> None:
    (path / "nested" / "deeper").mkdir(parents=True)
    (path / "nested" / "deeper" / "marker.txt").write_text(marker, encoding="utf-8")
    (path / "top.txt").write_text(marker, encoding="utf-8")


def test_cleanup_close_workspace_removes_unlisted_directory_and_spares_siblings(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    close_root = _close_root(repo)
    target = close_root / "close-unlisted"
    sibling = close_root / "close-unlisted-sibling"
    neighbour = close_root / "close-other"
    target.mkdir(parents=True)
    sibling.mkdir()
    neighbour.mkdir()
    _populate(target, "doomed")
    _populate(sibling, "keep-sibling")
    _populate(neighbour, "keep-neighbour")
    parent_file = repo / ".ea" / "worktrees" / "keep.txt"
    parent_file.write_text("keep", encoding="utf-8")
    assert target.resolve() not in _listed(repo)

    assert cleanup_close_workspace(repo, attempt_id="close-unlisted") is True

    assert not target.exists()
    assert (sibling / "nested" / "deeper" / "marker.txt").read_text() == "keep-sibling"
    assert (neighbour / "top.txt").read_text() == "keep-neighbour"
    assert parent_file.read_text() == "keep"
    assert close_root.is_dir()
    assert (repo / "payload.txt").read_text() == "frozen\n"


def test_cleanup_close_workspace_removes_forgotten_worktree_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    commit_sha, tree_sha = resolve_exact_revision(repo, "HEAD")
    prepared = prepare_close_workspace(
        repo,
        attempt_id="close-forgotten",
        commit_ref=commit_sha,
        expected_tree_sha=tree_sha,
    )
    sibling = _close_root(repo) / "close-sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")
    _forget_worktree(repo, prepared.path)

    assert cleanup_close_workspace(repo, attempt_id="close-forgotten") is True

    assert not prepared.path.exists()
    assert (sibling / "keep.txt").read_text() == "keep"
    assert cleanup_close_workspace(repo, attempt_id="close-forgotten") is False


def test_cleanup_close_workspace_removes_listed_worktree_through_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    commit_sha, tree_sha = resolve_exact_revision(repo, "HEAD")
    prepared = prepare_close_workspace(
        repo,
        attempt_id="close-listed",
        commit_ref=commit_sha,
        expected_tree_sha=tree_sha,
    )
    assert prepared.path.resolve() in _listed(repo)

    def _no_rmtree(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a listed worktree must be removed through git")

    monkeypatch.setattr(shutil, "rmtree", _no_rmtree)

    assert cleanup_close_workspace(repo, attempt_id="close-listed") is True

    assert not prepared.path.exists()
    assert prepared.path.resolve() not in _listed(repo)
    admin_root = repo / ".git" / "worktrees"
    assert not admin_root.exists() or not any(admin_root.iterdir())


def test_cleanup_close_workspace_removes_worktree_through_symlinked_repo_root(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    linked_root = tmp_path / "linked-repo"
    linked_root.symlink_to(repo, target_is_directory=True)
    commit_sha, tree_sha = resolve_exact_revision(linked_root, "HEAD")
    prepared = prepare_close_workspace(
        linked_root,
        attempt_id="close-via-link",
        commit_ref=commit_sha,
        expected_tree_sha=tree_sha,
    )

    assert cleanup_close_workspace(linked_root, attempt_id="close-via-link") is True

    assert not prepared.path.exists()
    assert (repo / "payload.txt").read_text() == "frozen\n"
    assert _listed(repo) == {repo.resolve()}


def test_cleanup_close_workspace_absent_path_returns_false(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sibling = _close_root(repo) / "close-present"
    sibling.mkdir(parents=True)

    assert cleanup_close_workspace(repo, attempt_id="close-absent") is False

    assert sibling.is_dir()


def test_cleanup_close_workspace_refuses_symlink_leaving_close_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "operator-data"
    _populate(outside, "precious")
    link = _close_root(repo) / "close-escape"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        CloseWorkspaceError,
        match=r"does not resolve to its own directory under \.ea/worktrees/close/",
    ):
        cleanup_close_workspace(repo, attempt_id="close-escape")

    assert link.is_symlink()
    assert (outside / "nested" / "deeper" / "marker.txt").read_text() == "precious"
    assert (outside / "top.txt").read_text() == "precious"


def test_cleanup_close_workspace_refuses_symlink_to_sibling_attempt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    close_root = _close_root(repo)
    sibling = close_root / "close-real"
    sibling.mkdir(parents=True)
    _populate(sibling, "sibling")
    link = close_root / "close-alias"
    link.symlink_to(sibling, target_is_directory=True)

    with pytest.raises(CloseWorkspaceError, match="refusing to remove close workspace"):
        cleanup_close_workspace(repo, attempt_id="close-alias")

    assert link.is_symlink()
    assert (sibling / "top.txt").read_text() == "sibling"


@pytest.mark.parametrize(
    "attempt_id",
    ["../escape", "close/../../escape", "..", "", "x" * 129],
)
def test_cleanup_close_workspace_rejects_attempt_id_outside_close_root(
    tmp_path: Path,
    attempt_id: str,
) -> None:
    repo = _repo(tmp_path)
    escape = repo / ".ea" / "worktrees" / "escape"
    _populate(escape, "outside")
    (_close_root(repo)).mkdir(parents=True)

    with pytest.raises(ValueError, match="invalid close attempt id"):
        cleanup_close_workspace(repo, attempt_id=attempt_id)

    assert (escape / "top.txt").read_text() == "outside"
    assert (_close_root(repo)).is_dir()


def test_cleanup_close_workspace_unlisted_removal_failure_raises_workspace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    target = _close_root(repo) / "close-stuck"
    target.mkdir(parents=True)
    _populate(target, "stuck")

    def _denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(shutil, "rmtree", _denied)

    with pytest.raises(CloseWorkspaceError, match="remove unlisted close workspace failed") as info:
        cleanup_close_workspace(repo, attempt_id="close-stuck")

    assert isinstance(info.value.__cause__, PermissionError)
    assert target.is_dir()


def test_cleanup_close_workspace_git_listing_failure_raises_workspace_error(
    tmp_path: Path,
) -> None:
    not_a_repo = tmp_path / "plain"
    target = _close_root(not_a_repo) / "close-orphan"
    target.mkdir(parents=True)

    with pytest.raises(CloseWorkspaceError, match="list worktrees failed"):
        cleanup_close_workspace(not_a_repo, attempt_id="close-orphan")

    assert target.is_dir()


def test_run_attempt_forgotten_workspace_closes_without_harness_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git forgetting the workspace mid-close no longer fails a finished close."""
    from eawf.runtime.daemon.methods import state as state_methods

    repo, state_path, ctx = _repo_with_state(tmp_path)
    monkeypatch.setattr(close_module, "schedule_attempt", lambda *_args, **_kwargs: False)
    real_mutate = state_methods.mutate

    async def _mutate_then_forget(ctx: Any, params: dict[str, Any]) -> dict[str, Any]:
        result = await real_mutate(ctx, params)
        workspace = Path(params["mutation"]["params"]["verification_repo_root"])
        _forget_worktree(repo, workspace)
        return result

    monkeypatch.setattr(state_methods, "mutate", _mutate_then_forget)

    async def body() -> str:
        submitted = await submit(
            ctx,
            {
                "wave_id": _WAVE,
                "outcome": "verified integrated revision",
                "repo_root": str(repo),
                "no_runtime_waiver": True,
            },
        )
        attempt_id = str(submitted["attempt"]["id"])
        await _run_attempt(ctx, repo_root=repo, attempt_id=attempt_id)
        return attempt_id

    attempt_id = asyncio.run(body())

    row = State.model_validate_json(state_path.read_bytes()).close_attempts[attempt_id]
    assert row.status is CloseAttemptStatus.CLOSED
    assert row.failure_kind is None
    assert row.failure_detail_ref is None
    assert not workspace_path(repo, attempt_id).exists()
