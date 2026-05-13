"""Markdown artifact chassis validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from eawf.artifacts.references import Citation, validate_dense_citation_refs
from eawf.scrub.scan import scan_text

REQUIRED_CHASSIS_HEADINGS: tuple[str, ...] = (
    "## Summary",
    "## References",
    "## Provenance",
    "## Scrub",
)


def _content_after_sentinel(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("<!-- eawf-template:"):
        return stripped
    end = stripped.find("-->")
    if end == -1:
        return stripped
    return stripped[end + 3 :].lstrip()


@dataclass(frozen=True)
class ArtifactValidationReport:
    """Artifact markdown validation result."""

    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_markdown_artifact(
    text: str,
    *,
    references: list[Citation] | None = None,
    require_template_sentinel: bool = False,
) -> ArtifactValidationReport:
    """Validate chassis headings, scrub status, and dense references."""
    errors: list[str] = []
    body = _content_after_sentinel(text)
    if require_template_sentinel and not text.lstrip().startswith("<!-- eawf-template:"):
        errors.append("missing draft sentinel")
    if not body.startswith("# "):
        errors.append("missing H1 title")
    for heading in REQUIRED_CHASSIS_HEADINGS:
        if heading not in text:
            errors.append(f"missing chassis heading: {heading}")
    findings = scan_text(text)
    if findings:
        errors.append("scrub findings present")
    if references is not None:
        try:
            validate_dense_citation_refs(text, references)
        except ValueError as exc:
            errors.append(str(exc))
    return ArtifactValidationReport(ok=not errors, errors=errors)
