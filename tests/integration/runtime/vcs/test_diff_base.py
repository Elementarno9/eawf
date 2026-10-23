"""The diff base and the rebase-safe commit identity of :mod:`eawf.runtime.vcs.diff_base`.

``derive_diff_base`` answers the merge-base of ``HEAD`` and ``main`` and
stays fail-open on ``origin/main`` whenever git cannot answer;
``commit_identity_digest`` names a commit by what a rebase keeps.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.vcs.diff_base import (
    FALLBACK_DIFF_BASE,
    commit_identity_digest,
    derive_diff_base,
)

_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "test@example.com")
    _run(root, "config", "user.name", "Test")


def _commit(root: Path, *, name: str, msg: str) -> str:
    (root / name).write_text("x\n", encoding="utf-8")
    _run(root, "add", name)
    _run(root, "commit", "-q", "-m", msg)
    return _run(root, "rev-parse", "HEAD")


class _Completed:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_git(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    monkeypatch.setattr("eawf.runtime.vcs.diff_base.shutil.which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr("eawf.runtime.vcs.diff_base.subprocess.run", run)


# ---- derive_diff_base -------------------------------------------------------


@_GIT
def test_derive_diff_base_is_the_merge_base_with_main(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, name="base.txt", msg="chore: base")
    _run(tmp_path, "switch", "-q", "-c", "topic")
    _commit(tmp_path, name="topic.txt", msg="feat: topic")
    assert derive_diff_base(repo_root=tmp_path) == base


@_GIT
def test_derive_diff_base_falls_back_without_a_main_branch(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    _run(tmp_path, "init", "-q", "-b", "trunk")
    _run(tmp_path, "config", "user.email", "test@example.com")
    _run(tmp_path, "config", "user.name", "Test")
    _commit(tmp_path, name="a.txt", msg="chore: a")
    assert derive_diff_base(repo_root=tmp_path) == FALLBACK_DIFF_BASE


def test_derive_diff_base_falls_back_when_git_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("eawf.runtime.vcs.diff_base.shutil.which", lambda _name: None)
    assert derive_diff_base(repo_root=tmp_path) == FALLBACK_DIFF_BASE


def test_derive_diff_base_falls_back_on_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_git(monkeypatch, lambda *_a, **_k: _Completed(128, ""))
    assert derive_diff_base(repo_root=tmp_path) == FALLBACK_DIFF_BASE


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(cmd="git", timeout=1.0), FileNotFoundError("git"), OSError("io")],
    ids=["timeout", "not-found", "os-error"],
)
def test_derive_diff_base_falls_back_when_git_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    def _raise(*_a: Any, **_k: Any) -> None:
        raise error

    _fake_git(monkeypatch, _raise)
    assert derive_diff_base(repo_root=tmp_path) == FALLBACK_DIFF_BASE


@pytest.mark.parametrize(("stdout", "expected"), [("", None), ("  \n", None), ("ab12\n", "ab12")])
def test_derive_diff_base_reads_the_printed_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, expected: str | None
) -> None:
    _fake_git(monkeypatch, lambda *_a, **_k: _Completed(0, stdout))
    assert derive_diff_base(repo_root=tmp_path) == (expected or FALLBACK_DIFF_BASE)


# ---- commit_identity_digest -------------------------------------------------


@_GIT
def test_commit_identity_digest_survives_rebase_twin(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, name="base.txt", msg="chore: base")
    _run(tmp_path, "switch", "-q", "-c", "topic")
    original = _commit(tmp_path, name="work.txt", msg="feat: stable patch")
    _run(tmp_path, "switch", "-q", "main")
    _commit(tmp_path, name="other.txt", msg="chore: move parent")
    _run(tmp_path, "cherry-pick", original)
    rebased = _run(tmp_path, "rev-parse", "HEAD")

    assert rebased != original
    assert commit_identity_digest(original, repo_root=tmp_path) == commit_identity_digest(
        rebased, repo_root=tmp_path
    )


@_GIT
def test_commit_identity_digest_differs_for_another_patch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    first = _commit(tmp_path, name="a.txt", msg="feat: same message")
    second = _commit(tmp_path, name="b.txt", msg="feat: same message")
    assert commit_identity_digest(first, repo_root=tmp_path) != commit_identity_digest(
        second, repo_root=tmp_path
    )


@_GIT
def test_commit_identity_digest_is_none_for_an_unknown_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, name="a.txt", msg="chore: a")
    assert commit_identity_digest("f" * 40, repo_root=tmp_path) is None


def test_commit_identity_digest_is_none_when_git_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("git")

    _fake_git(monkeypatch, _raise)
    assert commit_identity_digest("HEAD", repo_root=tmp_path) is None
