"""Canonical kind->subdir router for promotable draft artifacts.

The draft builder routes a slash-bearing slug to its artifact home through
the explicit ``ARTIFACT_KIND_SUBDIR`` map (e.g. ``audit`` -> ``audits/``) rather than
treating the singular kind token as the subdir. These tests pin both the
per-kind placement and the totality of the map over the promotable-kind set.
"""

from __future__ import annotations

from eawf.kernel.spec.common import ARTIFACT_KIND_SUBDIR


def test_kind_subdir_uses_canonical_artifact_tree_names() -> None:
    """Subdir names match the committed ``.ea/artifacts/`` tree layout."""
    assert ARTIFACT_KIND_SUBDIR == {
        "research": "research",
        "audit": "audits",
        "plan": "plans",
        "hypothesis": "hypotheses",
        "decision": "decisions",
        "incident": "incidents",
        "evidence": "evidence",
        "review": "reviews",
    }
