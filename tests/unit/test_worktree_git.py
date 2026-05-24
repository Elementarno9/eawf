"""Unit tests for the :mod:`eawf.runtime.worktree.git` subprocess wrappers.

The wrappers themselves are thin enough that we can exercise the
error-mapping branches without spinning up a real git repo by
monkey-patching :func:`subprocess.run` and :func:`shutil.which`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.cli import errors as cli_errors
from eawf.runtime.worktree import git


def test_git_worktree_add_missing_git_raises_instrument_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``shutil.which("git")`` returning ``None`` -> :class:`InstrumentMissing`."""
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: None)
    with pytest.raises(cli_errors.UserError) as exc_info:
        git.worktree_add(
            tmp_path,
            branch="feature/test",
            path=tmp_path / "wt",
            base="main",
        )
    assert "git executable not found" in str(exc_info.value)


def test_git_worktree_add_timeout_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A :class:`subprocess.TimeoutExpired` is mapped to :class:`IntegrityViolation` (exit 8).

    A timeout is a hung-git symptom, not a sibling-lock-held condition;
    ``LockConflict`` would lie about the failure mode.
    """
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: "/usr/bin/git")

    def _raise_timeout(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1.0)

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", _raise_timeout)
    with pytest.raises(cli_errors.StateConflict) as exc_info:
        git.worktree_add(
            tmp_path,
            branch="feature/test",
            path=tmp_path / "wt",
            base="main",
        )
    assert "timed out" in str(exc_info.value)


def test_status_porcelain_parses_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty stdout from ``git status --porcelain`` -> empty list (clean)."""
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", lambda *_a, **_k: _Result())
    assert git.status_porcelain(tmp_path) == []


def test_status_porcelain_parses_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-empty stdout yields a list with one entry per dirty file."""
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = " M src/eawf/foo.py\n?? newfile\n"
        stderr = ""

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", lambda *_a, **_k: _Result())
    rows = git.status_porcelain(tmp_path)
    assert len(rows) == 2
    assert any("foo.py" in row for row in rows)
    assert any("newfile" in row for row in rows)


def test_branch_exists_returns_true_when_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-empty `branch --list` output -> True."""
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = "  feature/foo\n"
        stderr = ""

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", lambda *_a, **_k: _Result())
    assert git.branch_exists(tmp_path, "feature/foo") is True


def test_current_branch_detached_head_raises_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-zero rc from `symbolic-ref --short HEAD` -> :class:`InvalidInput`."""
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "fatal: ref HEAD is not a symbolic ref"

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", lambda *_a, **_k: _Result())
    with pytest.raises(cli_errors.UserError) as exc_info:
        git.current_branch(tmp_path)
    assert "non-detached HEAD" in str(exc_info.value)
