"""Shared markdown chassis for durable artifacts."""

from __future__ import annotations

from collections.abc import Iterable

from eawf.artifacts.references import Citation


def render_references(references: Iterable[Citation]) -> list[str]:
    """Render dense citation rows."""
    rows = list(references)
    if not rows:
        return ["## References", "", "(none)"]
    lines = ["## References", ""]
    for citation in rows:
        title = f" — {citation.title}" if citation.title else ""
        note = f" ({citation.note})" if citation.note else ""
        lines.append(f"[{citation.n}] {citation.ref}{title}{note}")
    return lines


def render_provenance(*, kind: str, record_id: str, scope_id: str | None) -> list[str]:
    """Render provenance block for a store-backed artifact."""
    return [
        "## Provenance",
        "",
        f"- kind: {kind}",
        f"- record_id: {record_id}",
        f"- scope_id: {scope_id or '-'}",
    ]


def render_scrub_status(*, status: str = "clean") -> list[str]:
    """Render scrub block."""
    return ["## Scrub", "", f"- status: {status}"]
