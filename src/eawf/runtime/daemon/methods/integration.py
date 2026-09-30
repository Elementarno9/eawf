"""Daemon-owned Wave integration and dependency-barrier mutations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _AdoptionFacts:
    """Git facts pinned by one explicit adoption."""

    base_sha: str
    candidate_sha: str
    integrated_sha: str
    tree_sha: str
    diff_digest: str


def _git(
    repo_root: Path,
    *args: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded Git query without invoking a shell."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode not in allowed_returncodes:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git verification failed: {detail or 'unknown error'}")
    return result


def _stdout(result: subprocess.CompletedProcess[bytes]) -> str:
    """Decode and trim one Git query result."""
    return result.stdout.decode("utf-8", errors="strict").strip()


__all__ = []
