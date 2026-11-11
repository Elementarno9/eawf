"""``resolve_hook_context`` names where a host hook fired.

A subfolder and a linked worktree of a managed repository answer for its main
checkout; any other git working tree is ``unmanaged-git``; outside git the
directory itself is ``no-git``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.hooks.context import MANAGED_ANCHORS, HookContext, resolve_hook_context
from tests._linked_worktree import git, repo_with_linked_worktree


def _managed_with_state_json(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".ea").mkdir()
    (root / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    return root.resolve()


def test_context_kinds(tmp_path: Path) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "managed")
    subfolder = main / "src"
    subfolder.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-q")
    plain = tmp_path / "plain"
    plain.mkdir()

    assert resolve_hook_context(main) == HookContext(kind="managed", top=main, main=main)
    assert resolve_hook_context(subfolder) == HookContext(kind="managed", top=main, main=main)
    assert resolve_hook_context(linked) == HookContext(kind="managed", top=linked, main=main)
    assert resolve_hook_context(foreign) == HookContext(
        kind="unmanaged-git", top=foreign.resolve(), main=foreign.resolve()
    )
    assert resolve_hook_context(plain) == HookContext(kind="no-git", top=plain, main=plain)


def test_state_json_alone_marks_a_repository_managed(tmp_path: Path) -> None:
    root = _managed_with_state_json(tmp_path / "repo")
    assert resolve_hook_context(root).kind == "managed"


def test_an_ea_directory_without_an_anchor_is_unmanaged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".ea" / "generations").mkdir(parents=True)
    assert resolve_hook_context(root).kind == "unmanaged-git"


def test_an_anchor_that_is_a_directory_does_not_count(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / MANAGED_ANCHORS[0]).mkdir(parents=True)
    assert resolve_hook_context(root).kind == "unmanaged-git"


def test_a_missing_directory_is_no_git_at_itself(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    assert resolve_hook_context(missing) == HookContext(kind="no-git", top=missing, main=missing)


def test_a_bare_repository_is_no_git(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    assert resolve_hook_context(bare).kind == "no-git"


def test_a_missing_git_is_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _managed_with_state_json(tmp_path / "repo")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert resolve_hook_context(root) == HookContext(kind="no-git", top=root, main=root)


@pytest.mark.parametrize("cwd", ["/tmp", None, b"/tmp"])
def test_a_non_path_cwd_is_refused(cwd: object) -> None:
    with pytest.raises(TypeError, match="cwd must be a Path"):
        resolve_hook_context(cwd)  # type: ignore[arg-type]
