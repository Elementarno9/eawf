"""The main checkout a repository's git directories name, shared by the hook wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.vcs.branch_name import BareRepositoryError, named_main_root
from tests._linked_worktree import git


def test_a_common_dir_named_git_names_its_parent(tmp_path: Path) -> None:
    common = tmp_path / "repo" / ".git"
    assert named_main_root(common, common) == tmp_path / "repo"
    # A linked tree of it reads the common config; with nothing set, the parent stands.
    common.mkdir(parents=True)
    git(tmp_path / "repo", "init", "-q")
    assert named_main_root(common, common / "worktrees" / "w") == tmp_path / "repo"


def test_the_main_tree_of_a_separate_git_dir_names_none(tmp_path: Path) -> None:
    store = tmp_path / "store.git"
    (tmp_path / "checkout").mkdir()
    git(tmp_path / "checkout", "init", "-q", f"--separate-git-dir={store}")
    assert named_main_root(store, store) is None


def test_core_worktree_names_the_checkout(tmp_path: Path) -> None:
    store = tmp_path / "store.git"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q", f"--separate-git-dir={store}")
    git(checkout, "config", "core.worktree", "../checkout")
    assert named_main_root(store, store / "worktrees" / "w") == checkout.resolve()


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "on", "1"])
def test_a_bare_common_config_names_none_it_can_use(tmp_path: Path, value: str) -> None:
    store = tmp_path / "store.git"
    git(tmp_path, "init", "-q", "--bare", str(store))
    git(store, "config", "core.bare", value)
    with pytest.raises(BareRepositoryError):
        named_main_root(store, store / "worktrees" / "w")


def test_a_linked_tree_of_a_separate_git_dir_without_core_worktree_is_refused(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store.git"
    (tmp_path / "checkout").mkdir()
    git(tmp_path / "checkout", "init", "-q", f"--separate-git-dir={store}")
    with pytest.raises(BareRepositoryError):
        named_main_root(store, store / "worktrees" / "w")


def _separate(tmp_path: Path) -> tuple[Path, Path]:
    store = tmp_path / "store.git"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q", f"--separate-git-dir={store}")
    git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    return store.resolve(), checkout.resolve()


def test_a_tree_registered_inside_names_the_main_checkout(tmp_path: Path) -> None:
    store, checkout = _separate(tmp_path)
    git(checkout, "worktree", "add", "-q", str(checkout / ".ea" / "worktrees" / "w"), "-b", "w")
    git(checkout, "worktree", "add", "-q", str(tmp_path / "beside"), "-b", "beside")
    for name in ("w", "beside"):
        assert named_main_root(store, store / "worktrees" / name) == checkout
    # The main tree itself still names none: its top is the checkout.
    assert named_main_root(store, store) is None


def test_trees_registered_outside_name_no_main_checkout(tmp_path: Path) -> None:
    store, checkout = _separate(tmp_path)
    git(checkout, "worktree", "add", "-q", str(tmp_path / "beside"), "-b", "beside")
    with pytest.raises(BareRepositoryError):
        named_main_root(store, store / "worktrees" / "beside")


@pytest.mark.parametrize(
    "registration",
    ["", "\n", "not-a-path\n", "gitdir-gone"],
    ids=["empty", "blank-line", "relative-nowhere", "missing"],
)
def test_an_unreadable_registration_names_nothing(tmp_path: Path, registration: str) -> None:
    store, checkout = _separate(tmp_path)
    git(checkout, "worktree", "add", "-q", str(checkout / "w"), "-b", "w")
    entry = store / "worktrees" / "w" / "gitdir"
    if registration == "gitdir-gone":
        entry.unlink()
    else:
        entry.write_text(registration, encoding="utf-8")
    with pytest.raises(BareRepositoryError):
        named_main_root(store, store / "worktrees" / "w")


@pytest.mark.parametrize(
    "marker",
    ["", "gitdir: /nowhere\n", "not a gitfile\n", b"\xff\xfe"],
    ids=["empty", "elsewhere", "no-prefix", "not-utf8"],
)
def test_a_checkout_whose_git_file_names_another_store_is_not_the_main(
    tmp_path: Path, marker: str | bytes
) -> None:
    store, checkout = _separate(tmp_path)
    git(checkout, "worktree", "add", "-q", str(checkout / "w"), "-b", "w")
    gitfile = checkout / ".git"
    if isinstance(marker, bytes):
        gitfile.write_bytes(marker)
    else:
        gitfile.write_text(marker, encoding="utf-8")
    with pytest.raises(BareRepositoryError):
        named_main_root(store, store / "worktrees" / "w")


def test_a_missing_common_config_reads_as_unset(tmp_path: Path) -> None:
    store = tmp_path / "gone.git"
    assert named_main_root(store, store) is None
    with pytest.raises(BareRepositoryError):
        named_main_root(store, store / "worktrees" / "w")
