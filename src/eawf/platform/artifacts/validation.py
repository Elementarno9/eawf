"""Markdown artifact chassis validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.platform.artifacts.references import (
    Citation,
    citation_numbers_in_text,
    validate_dense_citation_refs,
)
from eawf.platform.scrub.scan import scan_text

if TYPE_CHECKING:
    from eawf.kernel.spec.intent import IntentBrief

_SHA256_CHUNK_BYTES: int = 65536

REQUIRED_CHASSIS_HEADINGS: tuple[str, ...] = (
    "## Summary",
    "## References",
    "## Provenance",
    "## Scrub",
)
_SECTION_HEADING_RE = re.compile(r"^## (?P<title>[^\n#]+)\s*$", re.MULTILINE)
_REFERENCE_ROW_RE = re.compile(r"^\[(?P<n>[1-9][0-9]*)\]\s+(?P<ref>\S+)")
_SCRUB_CLEAN_RE = re.compile(r"^\s*-?\s*status:\s*clean\s*$", re.IGNORECASE | re.MULTILINE)


def _content_after_sentinel(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("<!-- eawf-template:"):
        return stripped
    end = stripped.find("-->")
    if end == -1:
        return stripped
    return stripped[end + 3 :].lstrip()


def _sections(body: str) -> dict[str, str]:
    matches = list(_SECTION_HEADING_RE.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = f"## {match.group('title').strip()}"
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[heading] = body[start:end].strip()
    return sections


def _prose_without_references(body: str) -> str:
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if not matches:
        return body
    chunks: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        heading = f"## {match.group('title').strip()}"
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if heading == "## References":
            chunks.append(body[cursor:start])
            cursor = end
    chunks.append(body[cursor:])
    return "".join(chunks)


def _has_meaningful_content(section: str) -> bool:
    return any(
        line.strip() and line.strip() not in {"(none)", "(draft)"} for line in section.splitlines()
    )


def _reference_rows(section: str) -> list[Citation]:
    rows: list[Citation] = []
    for line in section.splitlines():
        match = _REFERENCE_ROW_RE.match(line.strip())
        if match is None:
            continue
        rows.append(Citation.from_legacy_source(int(match.group("n")), match.group("ref")))
    return rows


@dataclass(frozen=True)
class ArtifactValidationReport:
    """Artifact markdown validation result."""

    ok: bool
    errors: list[str] = field(default_factory=list)


TextSurfaceKind = str


@dataclass(frozen=True)
class TextSurfaceValidationReport:
    """PR/coauthor/release text validation result."""

    ok: bool
    errors: list[str] = field(default_factory=list)


def _citation_errors_for_text(
    text: str,
    references: list[Citation] | None,
) -> list[str]:
    errors: list[str] = []
    citation_rows = references
    if citation_rows is None:
        sections = _sections(text)
        try:
            citation_rows = _reference_rows(sections.get("## References", ""))
        except ValueError as exc:
            errors.append(str(exc))
            citation_rows = []
    prose = _prose_without_references(text)
    if citation_rows:
        try:
            validate_dense_citation_refs(prose, citation_rows)
        except ValueError as exc:
            errors.append(str(exc))
    else:
        missing_rows = sorted(set(citation_numbers_in_text(prose)))
        if missing_rows:
            errors.append(f"citation references missing rows: {missing_rows}")
    return errors


def validate_text_surface(
    text: str,
    *,
    surface: TextSurfaceKind,
    references: list[Citation] | None = None,
) -> TextSurfaceValidationReport:
    """Validate outbound PR/coauthor/release prose for scrub + citations."""
    errors: list[str] = []
    findings = scan_text(text)
    if findings:
        kinds = sorted({finding.kind for finding in findings})
        errors.append(f"{surface} scrub findings present: {kinds}")
    errors.extend(_citation_errors_for_text(text, references))
    return TextSurfaceValidationReport(ok=not errors, errors=errors)


def sha256_file(path: Path) -> str:
    """Return the lowercase sha256 hex digest of *path*'s bytes.

    Streams the file in 64 KiB chunks so the call works on artifacts larger
    than memory. Callers that need to verify a registered ``Artifact.sha256``
    should normalise the recorded value to lowercase before comparison.

    Args:
        path: Filesystem path to the artifact body.

    Returns:
        Lowercase hex digest of the file contents.

    Raises:
        FileNotFoundError: When *path* does not resolve to a readable file.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_SHA256_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_markdown_artifact(
    text: str,
    *,
    references: list[Citation] | None = None,
    require_template_sentinel: bool = False,
    intent: IntentBrief | None = None,
    project_root: Path | None = None,
) -> ArtifactValidationReport:
    """Validate chassis headings, scrub status, dense references, and EviBound.

    When *intent* is supplied the EviBound rung-1 gate runs over the
    brief's ``evidence_refs`` via
    :func:`eawf.workflow.evidence.check_brief_promotable`: every ref
    must resolve (rung-1) for the brief to validate. This is the
    promotion-time enforcement the
    :attr:`eawf.kernel.spec.intent.IntentBrief.evidence_refs` field's
    docstring promised — the gate fails the brief here rather than at
    ingestion time. *intent* is ``None`` for the chassis-only callers
    (PR / release-notes / coauthor text surfaces) so their behaviour is
    unchanged.

    Args:
        text: The artifact markdown body.
        references: Optional explicit citation rows; when ``None`` they
            are parsed out of the ``## References`` section.
        require_template_sentinel: When ``True`` a missing
            ``<!-- eawf-template: -->`` sentinel is an error (draft
            validation).
        intent: Optional typed brief whose ``evidence_refs`` are gated
            through EviBound rung-1. ``None`` skips the EviBound check.
        project_root: Absolute path the rung-1 disk-exists check
            resolves repo-relative refs against. Defaults to
            :func:`Path.cwd` when *intent* is supplied without an
            explicit root.

    Returns:
        An :class:`ArtifactValidationReport`; ``ok`` is ``False`` when
        any chassis, scrub, citation, or EviBound check fails.
    """
    errors: list[str] = []
    body = _content_after_sentinel(text)
    sections = _sections(body)
    if require_template_sentinel and not text.lstrip().startswith("<!-- eawf-template:"):
        errors.append("missing draft sentinel")
    if not body.startswith("# "):
        errors.append("missing H1 title")
    for heading in REQUIRED_CHASSIS_HEADINGS:
        if heading not in sections:
            errors.append(f"missing chassis heading: {heading}")
    references_section = sections.get("## References", "")
    provenance_section = sections.get("## Provenance", "")
    scrub_section = sections.get("## Scrub", "")
    if "## References" in sections and not references_section:
        errors.append("references section is empty")
    if "## Provenance" in sections and not _has_meaningful_content(provenance_section):
        errors.append("provenance section is empty")
    if "## Scrub" in sections and not _SCRUB_CLEAN_RE.search(scrub_section):
        errors.append("scrub status must be clean")
    findings = scan_text(text)
    if findings:
        errors.append("scrub findings present")
    errors.extend(_citation_errors_for_text(body, references))
    if intent is not None:
        errors.extend(_evibound_errors_for_intent(intent, project_root))
    return ArtifactValidationReport(ok=not errors, errors=errors)


def _evibound_errors_for_intent(
    intent: IntentBrief,
    project_root: Path | None,
) -> list[str]:
    """Run the EviBound rung-1 gate over *intent* and return rejection lines.

    Delegates to :func:`eawf.workflow.evidence.check_brief_promotable`
    (imported lazily to keep the ``platform`` layer free of a
    ``workflow`` import at module load). Returns the gate's ``reasons``
    list verbatim — empty when the brief is promotable.

    Args:
        intent: The typed brief whose ``evidence_refs`` are gated.
        project_root: Root the rung-1 disk-exists check resolves
            against; ``None`` falls back to :func:`Path.cwd`.

    Returns:
        One rejection line per ref that failed rung-1; ``[]`` when the
        brief is promotable.
    """
    from eawf.workflow.evidence import check_brief_promotable

    root = project_root if project_root is not None else Path.cwd()
    gate = check_brief_promotable(intent, project_root=root)
    return list(gate.reasons)
