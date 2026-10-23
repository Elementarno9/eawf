"""Unit tests for :mod:`eawf.surfaces.render.narrative`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.surfaces.render.narrative import (
    NarrativeBundle,
    generated_changelog_lines,
)


def test_narrative_bundle_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        NarrativeBundle.model_validate(
            {
                "scope_id": "P28",
                "title": "P28",
                "what": ["one"],
                "why": ["two"],
                "validation": ["three"],
                "risks": ["four"],
                "extra": True,
            }
        )


def test_generated_changelog_lines_prefixes_bullets() -> None:
    bundle = NarrativeBundle(
        scope_id="P28",
        title="P28",
        what=["one"],
        why=["two"],
        validation=["three"],
        risks=["four"],
        changelog=["Render narrative.", "Bound the bullets."],
    )

    assert generated_changelog_lines(bundle) == ["- Render narrative.", "- Bound the bullets."]


def test_generated_changelog_lines_of_a_bundle_without_a_changelog_is_empty() -> None:
    bundle = NarrativeBundle(
        scope_id="P28", title="P28", what=["one"], why=["two"], validation=["three"], risks=["4"]
    )

    assert generated_changelog_lines(bundle) == []


# ---------------------------------------------------------------------------
# Per-scope-kind dispatch
# ---------------------------------------------------------------------------
