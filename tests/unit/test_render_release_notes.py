from __future__ import annotations

from types import SimpleNamespace

import pytest

from eawf.platform.artifacts.references import Citation
from eawf.platform.artifacts.validation import validate_markdown_artifact
from eawf.surfaces.render import release_notes
from eawf.surfaces.render.artifact_chassis import render_references
from eawf.surfaces.render.release_notes import (
    ReleaseNotesValidationError,
    build_release_notes,
    mine_unreleased_changelog,
)

_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n- PR hardening\n- Faster reads\n\n## [0.1.0]\n"


def test_mine_unreleased_changelog_extracts_section() -> None:
    text = "# Changelog\n\n## [Unreleased]\n\n- One\n\n## [0.1.0]\n"
    assert mine_unreleased_changelog(text) == ["- One"]


def test_mine_unreleased_changelog_without_the_section_is_empty() -> None:
    assert mine_unreleased_changelog("") == []
    assert mine_unreleased_changelog("# Changelog\n\n## [0.1.0]\n\n- Old\n") == []


def test_build_release_notes_validates_as_artifact() -> None:
    body = build_release_notes(changelog_text=_CHANGELOG)
    assert "- Unreleased changelog entries mined from `CHANGELOG.md` [1]." in body
    assert "## Changelog" in body
    assert "- PR hardening\n- Faster reads" in body
    assert "## What" not in body
    assert validate_markdown_artifact(body).ok


@pytest.mark.parametrize("changelog_text", [None, "# Changelog\n\n## [Unreleased]\n\n"])
def test_build_release_notes_without_unreleased_entries_says_so(
    changelog_text: str | None,
) -> None:
    body = build_release_notes(changelog_text=changelog_text)
    assert "- `CHANGELOG.md` holds no unreleased entry yet [1]." in body
    assert "- No unreleased changelog entries found." in body
    assert validate_markdown_artifact(body).ok


def test_build_release_notes_routes_references_through_render_references() -> None:
    body = build_release_notes(changelog_text=_CHANGELOG)
    expected = render_references([Citation(n=1, ref="CHANGELOG.md")])
    assert "\n".join(expected) in body


def test_build_release_notes_refuses_a_draft_that_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _failing(_body: str) -> object:
        return SimpleNamespace(ok=False, errors=["missing Scrub section"])

    monkeypatch.setattr(release_notes, "validate_markdown_artifact", _failing)
    with pytest.raises(ReleaseNotesValidationError, match="missing Scrub section"):
        build_release_notes(changelog_text=_CHANGELOG)
