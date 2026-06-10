"""Canonical kind->subdir router for promotable draft artifacts.

The draft builder routes a slash-bearing slug to its artifact home through
the explicit ``_KIND_SUBDIR`` map (e.g. ``audit`` -> ``audits/``) rather than
treating the singular kind token as the subdir. These tests pin both the
per-kind placement and the totality of the map over the promotable-kind set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.surfaces.cli.commands.draft import (
    _KIND_SUBDIR,
    _PROMOTABLE_KINDS,
    _artifact_path,
)


@pytest.mark.parametrize("kind", sorted(_PROMOTABLE_KINDS))
def test_builder_places_kind_in_canonical_subdir(kind: str) -> None:
    """A nested (slash-bearing) slug lands under the kind's canonical subdir."""
    root = Path("/repo")
    slug = "long-term/2026-06-10-topic"
    dest = _artifact_path(root, kind, slug)
    expected = root / ".ea" / "artifacts" / _KIND_SUBDIR[kind] / "long-term" / "2026-06-10-topic.md"
    assert dest == expected
    # The canonical subdir is the first path segment under ``artifacts/``.
    rel = dest.relative_to(root / ".ea" / "artifacts")
    assert rel.parts[0] == _KIND_SUBDIR[kind]


def test_kind_subdir_covers_every_promotable_kind() -> None:
    """Every promotable kind has a subdir row (no kind left unmapped)."""
    assert set(_KIND_SUBDIR) == set(_PROMOTABLE_KINDS)


def test_kind_subdir_uses_canonical_artifact_tree_names() -> None:
    """Subdir names match the committed ``.ea/artifacts/`` tree layout."""
    assert _KIND_SUBDIR == {
        "research": "research",
        "audit": "audits",
        "plan": "plans",
        "hypothesis": "hypotheses",
        "decision": "decisions",
        "incident": "incidents",
    }


def test_unmapped_kind_raises_key_error() -> None:
    """A kind outside the map has no subdir placement (router has no row for it).

    ``_validate_kind_slug`` rejects unmapped kinds before placement on the real
    promote path, so a raw nested-slug call for an unknown kind surfaces the
    missing map row as a ``KeyError`` rather than a silent guess.
    """
    with pytest.raises(KeyError):
        _artifact_path(Path("/repo"), "release", "long-term/2026-06-10-v0-6-0")
