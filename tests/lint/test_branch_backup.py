"""Tests for the phase-branch backup contract.

The phase branch carries the whole P30 delivery, so its content must
exist somewhere besides the local working tree: the ``[0.6.0]``
CHANGELOG section is committed (not a dirty-tree-only hunk) and the
branch head is pushed to ``origin`` and never diverges from it.

Both checks are environment-sensitive and skip rather than fail when
the contract cannot be observed (detached HEAD, no ``origin`` remote,
network-unreachable remote).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_changelog_fixed_block_committed_in_060_section() -> None:
    shown = _git("show", "HEAD:CHANGELOG.md")
    assert shown.returncode == 0, f"cannot read committed CHANGELOG.md: {shown.stderr}"
    lines = shown.stdout.splitlines()
    section: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = "[0.6.0]" in line
            continue
        if inside:
            section.append(line)
    assert section, "no [0.6.0] section in committed CHANGELOG.md"
    assert any(line.startswith("### Fixed") for line in section), (
        "committed [0.6.0] CHANGELOG section carries no '### Fixed' block"
    )


def test_phase_branch_head_backed_up_on_origin() -> None:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not branch.startswith("feature/"):
        pytest.skip(f"not on a phase branch (HEAD is {branch!r})")
    try:
        remote = _git("ls-remote", "--heads", "origin", branch)
    except subprocess.TimeoutExpired:
        pytest.skip("origin unreachable (ls-remote timed out)")
    if remote.returncode != 0:
        pytest.skip(f"origin unreachable: {remote.stderr.strip()!r}")
    assert remote.stdout.strip(), f"phase branch {branch!r} has no backup: not pushed to origin"
    remote_sha = remote.stdout.split()[0]
    ancestry = _git("merge-base", "--is-ancestor", remote_sha, "HEAD")
    assert ancestry.returncode == 0, (
        f"origin/{branch} at {remote_sha[:12]} is not an ancestor of local HEAD: "
        "the pushed backup diverged from the local branch"
    )
