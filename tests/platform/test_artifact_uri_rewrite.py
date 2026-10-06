"""Canonical-subdir placement for every committed artifact.

Wave P30-I14-W07 relocated the legacy loose-root artifacts (those that sat
directly under ``.ea/artifacts/`` rather than inside a kind subdir) under their
canonical kind subdir from the ``ARTIFACT_KIND_SUBDIR`` map (e.g. ``A29-P23-ship-gate``
-> ``audits/``, ``research-2026-05-30-tui-chassis`` -> ``research/``) and
rewrote the doc/code references that named the old paths.

These tests pin the post-relocation invariant: no tracked artifact markdown
sits loose at the root of ``.ea/artifacts/``, and every one lives under a
canonical kind subdir (or the ``research/long-term/`` brief convention). They
read the tracked path list, never the local files a clone does not carry.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from eawf.kernel.spec.common import ARTIFACT_KIND_SUBDIR

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Canonical first-path-segment subdirs under ``.ea/artifacts/``, read from the one
# kind map rather than restated here.
_CANONICAL_SUBDIRS = frozenset(ARTIFACT_KIND_SUBDIR.values())


def _tracked_artifact_markdown() -> list[str]:
    """Repo-relative ``.ea/artifacts/**/*.md`` paths git tracks at HEAD+index.

    Reads the git index rather than walking the filesystem so gitignored render
    outputs (the ``rendered/`` tree is not committed) and stray scratch files
    do not perturb the invariant; only committed artifacts are asserted on.
    """
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", ".ea/artifacts/*.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_some_artifacts_are_tracked() -> None:
    """Guard: the index lists committed artifacts (empty list would be a false pass)."""
    assert _tracked_artifact_markdown(), "no tracked artifact markdown found under .ea/artifacts/"


def test_no_loose_root_artifact_remains() -> None:
    """Every tracked artifact markdown lives under a canonical kind subdir.

    A *loose-root* artifact is one whose path is ``.ea/artifacts/<file>.md`` with
    no intervening kind subdir. After the W07 relocation none should remain: each
    tracked artifact's first path segment under ``artifacts/`` is a canonical
    subdir, a value of the one kind map.
    """
    loose: list[str] = []
    uncanonical: list[str] = []
    for rel in _tracked_artifact_markdown():
        sub = Path(rel).relative_to(".ea/artifacts")
        if len(sub.parts) == 1:
            loose.append(rel)
        elif sub.parts[0] not in _CANONICAL_SUBDIRS:
            uncanonical.append(rel)
    assert not loose, f"loose-root artifacts remain: {loose}"
    assert not uncanonical, f"artifacts outside a canonical kind subdir: {uncanonical}"
