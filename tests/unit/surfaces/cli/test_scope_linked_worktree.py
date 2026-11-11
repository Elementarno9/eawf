"""``eawf status`` refuses a linked worktree named as the tree, and a bare repository."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.main import get_command

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.scope import resolve_state_path
from tests._epoch2_helpers import lay_epoch2_tree
from tests._linked_worktree import git, repo_with_linked_worktree


def _status(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Run ``eawf <argv> status`` in process; return exit code, stdout and stderr."""
    capsys.readouterr()
    code = get_command(app).main([*argv, "status"], prog_name="eawf", standalone_mode=False)
    captured = capsys.readouterr()
    return (code or 0), captured.out, captured.err


def _refusal(named: Path) -> str:
    return (
        f"{named} is a linked worktree; its .ea is never used. "
        "Unset it or point it at the main checkout"
    )


def test_linked_worktree_override_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(main)

    code, out, err = _status(["-w", str(linked)], capsys)
    assert code == exit_codes.USER_ERROR
    assert _refusal(linked) in err
    assert out == ""

    named = linked / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(named))
    code, _out, err = _status([], capsys)
    assert code == exit_codes.USER_ERROR
    assert _refusal(named) in err

    # The main checkout named either way is the tree itself, not a refusal.
    monkeypatch.setenv("EA_STATE", str(main / ".ea" / "state.json"))
    assert _status([], capsys)[0] == exit_codes.OK
    monkeypatch.delenv("EA_STATE")
    assert _status(["-w", str(main)], capsys)[0] == exit_codes.OK


def test_linked_worktree_subfolder_override_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _main, linked = repo_with_linked_worktree(tmp_path / "repo")
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(tmp_path)
    nested = linked / "src"
    nested.mkdir()
    code, _out, err = _status(["-w", str(nested)], capsys)
    assert code == exit_codes.USER_ERROR
    assert _refusal(nested) in err


def test_bare_repository_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    monkeypatch.chdir(bare)
    code, out, err = _status([], capsys)
    assert code == exit_codes.USER_ERROR
    assert "bare repository: pass -w <checkout> or set EA_STATE" in err
    assert out == ""

    # A common directory configured bare refuses its linked worktrees too.
    _main, linked = repo_with_linked_worktree(tmp_path / "repo")
    git(linked, "config", "core.bare", "true")
    monkeypatch.chdir(linked)
    code, _out, err = _status([], capsys)
    assert code == exit_codes.USER_ERROR
    assert "bare repository: pass -w <checkout> or set EA_STATE" in err

    # A separate git directory names its checkout through core.worktree.
    checkout = tmp_path / "checkout"
    lay_epoch2_tree(checkout)
    store = tmp_path / "store"
    store.mkdir()
    git(checkout, "init", "-q", f"--separate-git-dir={store / 'repo.git'}")
    git(checkout, "config", "core.worktree", str(checkout))
    sub = checkout / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert resolve_state_path(workspace=None) == checkout.resolve() / ".ea" / "state.json"
    assert _status([], capsys)[0] == exit_codes.OK


def test_bare_repository_refused_by_the_scope_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    monkeypatch.chdir(bare)
    with pytest.raises(FileNotFoundError, match="bare repository"):
        resolve_state_path(workspace=None)
    # An explicit tree is the way out the refusal names.
    tree = tmp_path / "tree"
    lay_epoch2_tree(tree)
    assert resolve_state_path(workspace=tree) == tree / ".ea" / "state.json"
