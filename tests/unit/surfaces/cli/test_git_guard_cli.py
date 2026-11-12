"""``eawf git commit``, ``cherry-pick`` and ``push`` hand git's arguments to the library.

The handler resolves the main checkout, passes every argument after ``--`` to
:func:`run_git_holding_document_lock` and exits with git's status; the lock is
the library's alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is the verb's exit status

from eawf.runtime.lock import portalock
from eawf.runtime.lock.portalock import LockTimeout
from eawf.runtime.vcs.document_hold import document_lock_target
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import git_guard
from tests._linked_worktree import git, repo_with_linked_worktree

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_COMMAND_ROW_RE = re.compile(r"^│ (\S+)\s", re.MULTILINE)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a managed repository with one root commit, as the working directory."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "GIT_AUTHOR_NAME": "ci",
        "GIT_AUTHOR_EMAIL": "ci@example.com",
        "GIT_COMMITTER_NAME": "ci",
        "GIT_COMMITTER_EMAIL": "ci@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("EA_STATE", raising=False)
    main, _linked = repo_with_linked_worktree(tmp_path / "trees")
    git(main, "config", "commit.gpgsign", "false")
    monkeypatch.chdir(main)
    return main


class _Recorder:
    """Stands in for the library: records its call and returns a fixed status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[tuple[Path, list[str]]] = []
        self.lock_free_during_call: bool | None = None

    def __call__(self, repo_root: Path, git_args: Sequence[str]) -> int:
        self.calls.append((repo_root, list(git_args)))
        try:
            with portalock.acquire(document_lock_target(repo_root), timeout=0):
                self.lock_free_during_call = True
        except LockTimeout:
            self.lock_free_during_call = False
        return self.status


def _subject(repo: Path) -> str:
    return git(repo, "log", "--format=%s", "-1").strip()


def test_a_commit_passes_every_argument_after_the_separator(repo: Path) -> None:
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    result = runner.invoke(app, ["git", "commit", "--", "-q", "-m", "two words", "--no-verify"])
    assert result.exit_code == 0, result.output
    assert _subject(repo) == "two words"


def test_a_cherry_pick_lands_the_named_commit(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "side")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "picked")
    picked = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "switch", "-q", "main")
    result = runner.invoke(app, ["git", "cherry-pick", "--", picked])
    assert result.exit_code == 0, result.output
    assert _subject(repo) == "picked"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["git", "commit", "--", "-q", "-m", "x"], 1),
        (["git", "commit"], 1),
        (["git", "commit", "--", "--no-such-option"], 129),
        (["git", "cherry-pick", "--", "no-such-revision"], 128),
    ],
    ids=["nothing-to-commit", "no-arguments", "usage-error", "unknown-revision"],
)
def test_the_verb_exits_with_gits_status(repo: Path, argv: list[str], expected: int) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == expected, result.output


def _remote(repo: Path) -> Path:
    """Add a local bare repository as ``origin`` and return it; nothing leaves the disk."""
    bare = repo.parent.parent / "remote.git"
    git(repo.parent, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    return bare


def test_a_push_passes_every_argument_after_the_separator(repo: Path) -> None:
    bare = _remote(repo)
    result = runner.invoke(app, ["git", "push", "--", "-q", "origin", "main:pushed"])
    assert result.exit_code == 0, result.output
    assert git(bare, "rev-parse", "pushed") == git(repo, "rev-parse", "HEAD")


def test_a_push_exits_with_the_status_of_a_failing_pre_push_hook(repo: Path) -> None:
    bare = _remote(repo)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    hook.chmod(0o755)
    result = runner.invoke(app, ["git", "push", "--", "-q", "origin", "main"])
    assert result.exit_code == 1, result.output
    assert git(bare, "branch", "--list") == ""


def test_the_git_group_help_names_push(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich styles the help wherever this variable is set, as on the hosted runners.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = runner.invoke(app, ["git", "--help"])
    assert result.exit_code == 0, result.output
    verbs = _COMMAND_ROW_RE.findall(_ANSI_RE.sub("", result.output))
    assert {"commit", "cherry-pick", "push"} <= set(verbs), result.output


@pytest.mark.parametrize("verb", ["commit", "cherry-pick", "push"])
def test_the_handler_dispatches_to_the_library_and_takes_no_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    recorder = _Recorder(status=5)
    monkeypatch.setattr(git_guard, "run_git_holding_document_lock", recorder)
    result = runner.invoke(app, ["git", verb, "--", "-m", "x", "--", "path"])
    assert result.exit_code == 5, result.output
    assert recorder.calls == [(repo, [verb, "-m", "x", "--", "path"])]
    assert recorder.lock_free_during_call is True


def test_a_linked_worktree_holds_the_main_checkouts_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(status=0)
    monkeypatch.setattr(git_guard, "run_git_holding_document_lock", recorder)
    monkeypatch.chdir(repo.parent / "linked")
    result = runner.invoke(app, ["git", "commit", "--", "-m", "x"])
    assert result.exit_code == 0, result.output
    assert [root for root, _args in recorder.calls] == [repo]


def test_a_directory_no_eawf_tree_holds_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    git(plain, "init", "-q")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(plain)
    result = runner.invoke(app, ["git", "commit", "--", "-m", "x"])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "holds no .ea tree" in result.output


def test_a_held_lock_is_a_state_conflict(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(repo_root: Path, git_args: Sequence[str]) -> int:
        raise LockTimeout("Could not acquire lock within 120.0s")

    monkeypatch.setattr(git_guard, "run_git_holding_document_lock", _timeout)
    result = runner.invoke(app, ["git", "commit", "--", "-m", "x"])
    assert result.exit_code == exit_codes.STATE_CONFLICT
    assert "Could not acquire lock" in result.output


def test_options_before_the_separator_are_refused(repo: Path) -> None:
    result = runner.invoke(app, ["git", "commit", "-m", "x"])
    assert result.exit_code == 2
    assert _subject(repo) == "tree"
