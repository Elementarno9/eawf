"""Markdown artifact chassis validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from eawf.artifacts.references import (
    Citation,
    citation_numbers_in_text,
    validate_dense_citation_refs,
)
from eawf.scrub.scan import scan_text

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
) -> ArtifactValidationReport:
    """Validate chassis headings, scrub status, and dense references."""
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
    return ArtifactValidationReport(ok=not errors, errors=errors)
