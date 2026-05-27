"""Unit tests for :mod:`eawf.surfaces.render.changelog`."""

from __future__ import annotations

import pytest

from eawf.surfaces.render.changelog import (
    ChangelogRewriteError,
    render_unreleased_section,
    rewrite_unreleased_section,
    unreleased_changelog_lines,
)
from eawf.surfaces.render.narrative import NarrativeBundle


def _bundle(*changelog: str) -> NarrativeBundle:
    return NarrativeBundle(
        scope_id="P28",
        title="P28: release surfaces",
        what=["Release surfaces landed."],
        why=["Keep changelog output aligned."],
        validation=["Focused tests passed."],
        risks=["No open risks recorded."],
        changelog=list(changelog),
    )


def test_unreleased_changelog_lines_deduplicates_bundles() -> None:
    first = _bundle("Render release changelog.", "Add EAWF016 lint.")
    second = _bundle("Add EAWF016 lint.", "Configure release cadence.")

    assert unreleased_changelog_lines([first, second]) == [
        "- Render release changelog.",
        "- Add EAWF016 lint.",
        "- Configure release cadence.",
    ]


def test_render_unreleased_section_uses_generated_changelog_lines() -> None:
    section = render_unreleased_section([_bundle("Render release changelog.")])

    assert section == "## [Unreleased]\n\n- Render release changelog."


def test_rewrite_unreleased_section_replaces_only_unreleased() -> None:
    changelog = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Old generated line.\n\n"
        "## [0.2.0] - 2026-05-11\n\n"
        "- Stable release line.\n"
    )

    rewritten = rewrite_unreleased_section(changelog, [_bundle("Render release changelog.")])

    assert "- Old generated line." not in rewritten
    assert "## [Unreleased]\n\n- Render release changelog.\n\n## [0.2.0]" in rewritten
    assert "- Stable release line." in rewritten


def test_rewrite_unreleased_section_requires_heading() -> None:
    with pytest.raises(ChangelogRewriteError, match="missing"):
        rewrite_unreleased_section("# Changelog\n", [_bundle("Render release changelog.")])
