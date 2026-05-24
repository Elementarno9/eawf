"""Markdown renderer for research store records."""

from __future__ import annotations

from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research import ResearchPayload
from eawf.platform.artifacts.references import citation_numbers_in_text
from eawf.surfaces.render.artifact_chassis import (
    render_provenance,
    render_references,
    render_scrub_status,
)


def render_research_markdown(envelope: Envelope, payload: ResearchPayload) -> str:
    """Render a research brief with the standard artifact chassis."""
    lines: list[str] = [
        f"# Research Brief: {envelope.id}",
        "",
        f"> Topic: {payload.topic} · Created: {envelope.created_at.isoformat()}",
        "",
        "## Summary",
        "",
    ]
    if payload.findings:
        for finding in payload.findings:
            lines.append(f"- {finding}")
    else:
        lines.append("(none)")
    summary_prose = "\n".join(lines)
    used_refs = set(citation_numbers_in_text(summary_prose))
    unused_refs = [citation.n for citation in payload.references if citation.n not in used_refs]
    if unused_refs:
        markers = " ".join(f"[{n}]" for n in unused_refs)
        lines.append(f"- References: {markers}")
    lines.append("")
    lines.extend(render_references(payload.references))
    lines.append("")
    lines.extend(
        render_provenance(
            kind=envelope.kind.value,
            record_id=envelope.id,
            scope_id=envelope.scope_id,
        )
    )
    lines.append("")
    lines.extend(render_scrub_status())
    lines.append("")
    return "\n".join(lines)
