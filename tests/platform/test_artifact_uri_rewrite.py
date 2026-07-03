"""Canonical-subdir placement for every committed artifact + rewritten refs.

Wave P30-I14-W07 relocated the legacy loose-root artifacts (those that sat
directly under ``.ea/artifacts/`` rather than inside a kind subdir) under their
canonical kind subdir from the ``_KIND_SUBDIR`` map (e.g. ``A29-P23-ship-gate``
-> ``audits/``, ``research-2026-05-30-tui-chassis`` -> ``research/``) and
rewrote the doc/code references that named the old paths.

These tests pin the post-relocation invariant: no tracked artifact markdown
sits loose at the root of ``.ea/artifacts/``, every one lives under a canonical
kind subdir (or the ``research/long-term/`` brief convention), and a sampled
rewritten reference resolves to a real file at its new path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eawf.surfaces.cli.commands.draft import _KIND_SUBDIR

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACTS_DIR = _REPO_ROOT / ".ea" / "artifacts"

# Canonical first-path-segment subdirs under ``.ea/artifacts/``: the promotable
# kind subdirs from the single-source-of-truth map, plus the renderer-owned
# ``rendered/`` output tree (not a promotable artifact kind, never loose).
# ``evidence`` is a recorded-validation-bundle kind (P30-I23-W33), not a
# draft-promotable prose kind, so it extends the set here alongside the
# render-only ``rendered`` tree.
_CANONICAL_SUBDIRS = frozenset(_KIND_SUBDIR.values()) | {"rendered", "evidence"}


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
    subdir (``audits``, ``research``, ``plans``, ``hypotheses``, ``decisions``,
    ``incidents``, or the renderer-owned ``rendered``).
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


def test_relocated_research_brief_resolves_under_canonical_subdir() -> None:
    """The relocated TUI-chassis research brief resolves at its new ``research/`` path."""
    new_path = _ARTIFACTS_DIR / "research" / "research-2026-05-30-tui-chassis.md"
    old_path = _ARTIFACTS_DIR / "research-2026-05-30-tui-chassis.md"
    assert new_path.is_file(), f"relocated research brief missing: {new_path}"
    assert not old_path.exists(), f"loose-root copy lingers: {old_path}"


@pytest.mark.parametrize(
    ("citing_file", "new_ref"),
    [
        # criterion_drift.py docstring cites the P23 ship-gate audit by its new path.
        (
            "src/eawf/workflow/lifecycle/criterion_drift.py",
            ".ea/artifacts/audits/A29-P23-ship-gate.md",
        ),
        # The A48 flow audit cites the relocated TUI-chassis brief by its new path.
        (
            ".ea/artifacts/audits/2026-06-03-A48-P29-i05-flow-audit.md",
            ".ea/artifacts/research/research-2026-05-30-tui-chassis.md",
        ),
    ],
)
def test_rewritten_reference_resolves(citing_file: str, new_ref: str) -> None:
    """A rewritten reference names the new path AND that path exists on disk."""
    citing_text = (_REPO_ROOT / citing_file).read_text(encoding="utf-8")
    assert new_ref in citing_text, f"{citing_file} does not cite rewritten path {new_ref!r}"
    assert (_REPO_ROOT / new_ref).is_file(), f"rewritten reference target missing: {new_ref}"
