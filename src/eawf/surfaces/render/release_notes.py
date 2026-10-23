"""Render release-note drafts from the unreleased section of the changelog."""

from __future__ import annotations

import re

from eawf.platform.artifacts.references import Citation
from eawf.platform.artifacts.validation import validate_markdown_artifact
from eawf.surfaces.render.artifact_chassis import render_references


class ReleaseNotesValidationError(ValueError):
    """Raised when rendered release notes fail artifact validation."""


def mine_unreleased_changelog(changelog_text: str) -> list[str]:
    """Return non-empty lines under the ``[Unreleased]`` section."""
    lines = changelog_text.splitlines()
    in_section = False
    mined: list[str] = []
    for line in lines:
        if line.startswith("## [Unreleased]"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.strip():
            mined.append(line.rstrip())
    return mined


def build_release_notes(*, changelog_text: str | None = None) -> str:
    """Render a chassis-valid release notes draft from the unreleased changelog.

    Args:
        changelog_text: The text of ``CHANGELOG.md``, or ``None`` when the
            repository has none.

    Returns:
        The draft, whose Changelog section holds the lines under the
        ``[Unreleased]`` heading.

    Raises:
        ReleaseNotesValidationError: The draft fails artifact validation.
    """
    changelog_lines = mine_unreleased_changelog(changelog_text or "")
    summary = (
        "- Unreleased changelog entries mined from `CHANGELOG.md` [1]."
        if changelog_lines
        else "- `CHANGELOG.md` holds no unreleased entry yet [1]."
    )
    body = "\n".join(
        [
            "# Release Notes Draft",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Changelog",
            "",
            *(changelog_lines or ["- No unreleased changelog entries found."]),
            "",
            *render_references([Citation(n=1, ref="CHANGELOG.md")]),
            "",
            "## Provenance",
            "",
            "- source: the unreleased section of CHANGELOG.md",
            "- renderer: eawf.surfaces.render.release_notes",
            "",
            "## Scrub",
            "",
            "- status: clean",
            "",
        ]
    )
    report = validate_markdown_artifact(body)
    if not report.ok:
        raise ReleaseNotesValidationError("; ".join(report.errors))
    return body


def release_slug(version: str) -> str:
    """Return a portable slug for a version string."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", version).strip("-") or "release"


__all__ = [
    "ReleaseNotesValidationError",
    "build_release_notes",
    "mine_unreleased_changelog",
    "release_slug",
]
