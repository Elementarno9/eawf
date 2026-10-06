"""Regression: commit identity digests survive non-UTF-8 patch bytes.

A repository that tracks legacy-codepage text (cp1251 sources, say) makes
``git show`` emit bytes that are not valid UTF-8. The git probes in
:mod:`eawf.workflow.lifecycle.wave_sha` must decode them leniently instead of
losing stdout to a reader-thread ``UnicodeDecodeError`` and crashing
``eawf status`` / ``eawf doctor`` on the drift scan.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.workflow.lifecycle.wave_sha import commit_identity_digest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True, stdin=subprocess.DEVNULL
    )
    return out.stdout.decode("utf-8").strip()


def _repo_with_commit(tmp_path: Path, payload: bytes) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitattributes").write_bytes(b"*.lua text\n")
    (repo / "legacy.lua").write_bytes(payload)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add legacy source")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_digest_handles_non_utf8_patch_bytes(tmp_path: Path) -> None:
    cp1251_text = "-- Привет, мир\nlocal x = 1\n".encode("cp1251") + b"-- \xff\xfe\n"
    repo, sha = _repo_with_commit(tmp_path, cp1251_text)

    digest = commit_identity_digest(sha, repo_root=repo)

    assert digest is not None
    assert digest.startswith("sha256:")
    assert digest == commit_identity_digest(sha, repo_root=repo)


def test_digest_unchanged_for_utf8_patch(tmp_path: Path) -> None:
    repo, sha = _repo_with_commit(tmp_path, "-- Привет\nlocal x = 1\n".encode())

    digest = commit_identity_digest(sha, repo_root=repo)

    assert digest is not None
    assert digest.startswith("sha256:")
