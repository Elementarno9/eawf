"""Markdown renderer for research store records."""

from __future__ import annotations

from eawf.render.artifact_chassis import (
    render_provenance,
    render_references,
    render_scrub_status,
)
from eawf.store.envelope import Envelope
from eawf.store.kinds.research import ResearchPayload


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
