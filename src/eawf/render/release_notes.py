"""Render release-note drafts from committed state and artifacts."""

from __future__ import annotations

import re

from eawf.artifacts.validation import validate_markdown_artifact
from eawf.state.models import State


class ReleaseNotesValidationError(ValueError):
    """Raised when rendered release notes fail artifact validation."""


def _phase_sort_key(phase_id: str) -> int:
    return int(phase_id.removeprefix("P"))


def _phase_in_range(phase_id: str, from_phase: str | None, to_phase: str | None) -> bool:
    value = _phase_sort_key(phase_id)
    if from_phase is not None and value < _phase_sort_key(from_phase):
        return False
    return not (to_phase is not None and value > _phase_sort_key(to_phase))


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


def _artifact_rows(state: State) -> list[str]:
    rows: list[str] = []
    for artifact in sorted((state.artifacts or {}).values(), key=lambda a: a.id):
        if not artifact.uri.startswith("repo:.ea/artifacts/"):
            continue
        rows.append(f"- `{artifact.id}` `{artifact.kind}` {artifact.uri}")
    return rows


def build_release_notes(
    state: State,
    *,
    from_phase: str | None = None,
    to_phase: str | None = None,
    changelog_text: str | None = None,
) -> str:
    """Render a chassis-valid release notes draft."""
    phases = [
        phase
        for phase in sorted(state.phases.values(), key=lambda p: p.id)
        if _phase_in_range(phase.id, from_phase, to_phase)
    ]
    changelog_lines = mine_unreleased_changelog(changelog_text or "")
    summary_rows = [
        f"- `{phase.id}` {phase.title} ({phase.status.value}) [1]" for phase in phases
    ] or ["- No phases matched the requested range [1]."]
    if changelog_lines:
        summary_rows.append("- Unreleased changelog entries mined from `CHANGELOG.md` [2].")
    artifact_rows = _artifact_rows(state)
    references = ["[1] .ea/state.json"]
    if changelog_lines:
        references.append("[2] CHANGELOG.md")
    body = "\n".join(
        [
            "# Release Notes Draft",
            "",
            "## Summary",
            "",
            *summary_rows,
            "",
            "## Artifacts",
            "",
            *(artifact_rows or ["- No committed artifact rows found."]),
            "",
            "## Changelog Mine",
            "",
            *(changelog_lines or ["- No unreleased changelog entries found."]),
            "",
            "## References",
            "",
            *references,
            "",
            "## Provenance",
            "",
            "- source: committed state and artifact metadata",
            "- renderer: eawf.render.release_notes",
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
