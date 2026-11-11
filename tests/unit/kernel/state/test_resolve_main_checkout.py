"""State resolution goes through the repository's main checkout, never a worktree copy."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.kernel.state.resolve import (
    REASON_PWD_UPWARD,
    LinkedWorktreeStateError,
    MissingMainCheckoutError,
    home_state,
    refuse_linked_worktree,
    resolve_with_reason,
)
from eawf.runtime.vcs import branch_name
from eawf.runtime.vcs.branch_name import (
    BARE_REPOSITORY_MESSAGE,
    BareRepositoryError,
    RepositoryHome,
    main_checkout,
    repository_home,
)
from eawf.surfaces.cli.scope import resolve_state_path
from tests._epoch2_helpers import lay_epoch2_tree
from tests._linked_worktree import git, repo_with_linked_worktree


@pytest.fixture(autouse=True)
def _no_state_override(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("EA_STATE", raising=False)
    yield


def test_worktree_resolves_main_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    expected = main / ".ea" / "state.json"
    # The worktree carries its own frozen tree, which the walk must step over.
    assert (linked / ".ea" / "generations").is_dir()
    subfolder = main / "src" / "pkg"
    subfolder.mkdir(parents=True)
    # A nested tree under the main checkout is no more the repository's than a copy.
    lay_epoch2_tree(subfolder)

    for start in (linked, linked / ".ea", subfolder):
        monkeypatch.chdir(start)
        assert resolve_with_reason(workspace=None) == (expected, REASON_PWD_UPWARD)
        assert resolve_state_path(workspace=None) == expected

    gitless = tmp_path / "plain"
    lay_epoch2_tree(gitless)
    deep = gitless / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    plain_expected = gitless.resolve() / ".ea" / "state.json"
    assert resolve_with_reason(workspace=None) == (plain_expected, REASON_PWD_UPWARD)
    assert resolve_state_path(workspace=None) == plain_expected


def test_env_and_flag_still_win_over_the_main_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    other = tmp_path / "other"
    lay_epoch2_tree(other)
    monkeypatch.chdir(linked)
    assert resolve_with_reason(workspace=other)[0] == other / ".ea" / "state.json"
    assert resolve_with_reason(workspace=main)[0] == main / ".ea" / "state.json"
    env_state = other / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(env_state))
    assert resolve_with_reason(workspace=main)[0] == env_state
    assert resolve_state_path(workspace=main) == env_state


def test_home_state_reports_a_missing_tree_at_the_main_checkout(tmp_path: Path) -> None:
    main = tmp_path / "bare-tree"
    main.mkdir()
    git(main, "init", "-q")
    (main / "sub").mkdir()
    assert home_state(main / "sub") == (main.resolve() / ".ea" / "state.json", False)


def test_home_state_walks_above_the_main_checkout(tmp_path: Path) -> None:
    """A repository nested in a tree with no tree of its own reaches the outer one."""
    lay_epoch2_tree(tmp_path)
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    assert home_state(inner) == (tmp_path.resolve() / ".ea" / "state.json", True)


def test_missing_tree_raises_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    monkeypatch.chdir(repo)
    with pytest.raises(FileNotFoundError, match=r"not an eawf-managed repository"):
        resolve_state_path(workspace=None)
    assert resolve_with_reason(workspace=None)[0] == repo.resolve() / ".ea" / "state.json"


def test_repository_home_outside_git_is_none(tmp_path: Path) -> None:
    assert repository_home(tmp_path) is None
    assert main_checkout(tmp_path) == tmp_path


def test_repository_home_of_a_missing_directory_is_none(tmp_path: Path) -> None:
    assert repository_home(tmp_path / "absent") is None


def test_repository_home_marks_only_the_linked_tree(tmp_path: Path) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    assert repository_home(main) == RepositoryHome(main_root=main, linked=False)
    assert repository_home(main / ".ea") == RepositoryHome(main_root=main, linked=False)
    assert repository_home(linked) == RepositoryHome(main_root=main, linked=True)
    assert main_checkout(linked) == main


def test_repository_home_refuses_a_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    with pytest.raises(BareRepositoryError, match="bare repository: pass -w"):
        repository_home(bare)
    assert main_checkout(bare) == bare


def test_repository_home_refuses_a_worktree_on_a_bare_common_dir(tmp_path: Path) -> None:
    _main, linked = repo_with_linked_worktree(tmp_path / "repo")
    git(linked, "config", "core.bare", "true")
    with pytest.raises(BareRepositoryError) as caught:
        repository_home(linked)
    assert str(caught.value) == BARE_REPOSITORY_MESSAGE


def test_repository_home_follows_core_worktree_of_a_separate_git_dir(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git_dir = tmp_path / "store" / "repo.git"
    git_dir.parent.mkdir()
    git(checkout, "init", "-q", f"--separate-git-dir={git_dir}")
    git(checkout, "config", "core.worktree", str(checkout))
    assert repository_home(checkout) == RepositoryHome(main_root=checkout.resolve(), linked=False)


def test_repository_home_of_a_separate_git_dir_without_core_worktree_is_its_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    lay_epoch2_tree(checkout)
    git_dir = tmp_path / "store" / "repo.git"
    git_dir.parent.mkdir()
    git(checkout, "init", "-q", f"--separate-git-dir={git_dir}")
    git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    sub = checkout / "sub"
    sub.mkdir()
    expected = RepositoryHome(main_root=checkout.resolve(), linked=False)
    assert repository_home(checkout) == expected
    assert repository_home(sub) == expected
    monkeypatch.chdir(sub)
    assert resolve_state_path(workspace=None) == checkout.resolve() / ".ea" / "state.json"
    # The git directory itself is no checkout.
    assert repository_home(git_dir) is None
    # git records no main tree for a separate git directory: its linked tree is refused.
    linked = tmp_path / "linked"
    git(checkout, "worktree", "add", "-q", str(linked), "-b", "side")
    with pytest.raises(BareRepositoryError):
        repository_home(linked)


def test_home_state_refuses_a_worktree_whose_main_checkout_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    moved = tmp_path / "moved"
    main.rename(moved)
    # The worktree still holds its own frozen .ea, which must not be picked.
    assert (linked / ".ea" / "generations").is_dir()
    missing = re.escape(f"main checkout {main} is missing")
    with pytest.raises(MissingMainCheckoutError, match=missing):
        home_state(linked / ".ea")
    monkeypatch.chdir(linked)
    with pytest.raises(LinkedWorktreeStateError, match="is a linked worktree whose main"):
        resolve_with_reason(None)
    with pytest.raises(FileNotFoundError, match=missing):
        resolve_state_path(workspace=None)


def test_home_state_refuses_a_checkout_whose_git_directory_is_gone(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    lay_epoch2_tree(checkout)
    (checkout / ".git").write_text(f"gitdir: {tmp_path / 'gone.git'}\n", encoding="utf-8")
    gone = re.escape(f"git directory {tmp_path / 'gone.git'} is missing")
    with pytest.raises(MissingMainCheckoutError, match=gone):
        home_state(checkout)


def test_home_state_keeps_walking_past_a_plain_gitfile(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    lay_epoch2_tree(tree)
    (tree / ".git").write_text("not a gitdir line\n", encoding="utf-8")
    assert home_state(tree) == (tree.resolve() / ".ea" / "state.json", True)


@pytest.mark.parametrize(
    "error",
    [OSError("no git"), branch_name.subprocess.TimeoutExpired(cmd="git", timeout=2.0)],  # type: ignore[attr-defined]
    ids=["git-missing", "git-timeout"],
)
def test_repository_home_without_an_answering_git_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("eawf.runtime.vcs.branch_name.subprocess.run", refuse)
    assert repository_home(tmp_path) is None
    assert main_checkout(tmp_path) == tmp_path


def test_refuse_linked_worktree_passes_trees_outside_a_linked_worktree(tmp_path: Path) -> None:
    main, _linked = repo_with_linked_worktree(tmp_path / "repo")
    refuse_linked_worktree(main)
    refuse_linked_worktree(main / ".ea" / "state.json")
    refuse_linked_worktree(tmp_path / "nowhere" / ".ea" / "state.json")
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    refuse_linked_worktree(bare)


def test_refuse_linked_worktree_names_the_path_it_was_given(tmp_path: Path) -> None:
    _main, linked = repo_with_linked_worktree(tmp_path / "repo")
    missing = linked / ".ea" / "absent" / "state.json"
    with pytest.raises(LinkedWorktreeStateError) as caught:
        refuse_linked_worktree(missing)
    assert str(caught.value) == (
        f"{missing} is a linked worktree; its .ea is never used. "
        "Unset it or point it at the main checkout"
    )
    assert isinstance(caught.value, FileNotFoundError)


def test_resolvers_refuse_a_linked_worktree_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked = repo_with_linked_worktree(tmp_path / "repo")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LinkedWorktreeStateError):
        resolve_with_reason(workspace=linked)
    with pytest.raises(LinkedWorktreeStateError):
        resolve_state_path(workspace=linked)
    monkeypatch.setenv("EA_STATE", str(linked / ".ea" / "state.json"))
    with pytest.raises(LinkedWorktreeStateError):
        resolve_with_reason(workspace=None)
