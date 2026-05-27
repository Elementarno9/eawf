"""Render laconic changelog sections from narrative bundles."""

from __future__ import annotations

from collections.abc import Sequence

from eawf.surfaces.render.narrative import NarrativeBundle, generated_changelog_lines

UNRELEASED_HEADING = "## [Unreleased]"


class ChangelogRewriteError(ValueError):
    """Raised when a changelog cannot be rewritten safely."""


def unreleased_changelog_lines(bundles: Sequence[NarrativeBundle]) -> list[str]:
    """Return de-duplicated ``[Unreleased]`` bullets for narrative bundles."""
    lines: list[str] = []
    seen: set[str] = set()
    for bundle in bundles:
        for line in generated_changelog_lines(bundle):
            if line in seen:
                continue
            lines.append(line)
            seen.add(line)
    return lines


def render_unreleased_section(bundles: Sequence[NarrativeBundle]) -> str:
    """Render the complete ``[Unreleased]`` changelog section."""
    lines = unreleased_changelog_lines(bundles)
    body = lines or ["- No unreleased changes recorded."]
    return "\n".join([UNRELEASED_HEADING, "", *body])


def _unreleased_bounds(lines: list[str]) -> tuple[int, int] | None:
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == UNRELEASED_HEADING),
        None,
    )
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def rewrite_unreleased_section(
    changelog_text: str,
    bundles: Sequence[NarrativeBundle],
) -> str:
    """Rewrite ``CHANGELOG.md`` ``[Unreleased]`` from narrative bundles.

    Args:
        changelog_text: Existing changelog Markdown.
        bundles: Narrative bundles to mine for generated changelog bullets.

    Returns:
        Changelog text with the old ``[Unreleased]`` section replaced.

    Raises:
        ChangelogRewriteError: When the changelog has no ``[Unreleased]`` heading.
    """
    lines = changelog_text.splitlines()
    bounds = _unreleased_bounds(lines)
    if bounds is None:
        raise ChangelogRewriteError("changelog missing [Unreleased] section")
    start, end = bounds
    replacement = [*render_unreleased_section(bundles).splitlines(), ""]
    rewritten = [*lines[:start], *replacement, *lines[end:]]
    return "\n".join(rewritten).rstrip() + "\n"


__all__ = [
    "UNRELEASED_HEADING",
    "ChangelogRewriteError",
    "render_unreleased_section",
    "rewrite_unreleased_section",
    "unreleased_changelog_lines",
]
