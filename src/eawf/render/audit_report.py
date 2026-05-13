"""Markdown renderer for audit state records."""

from __future__ import annotations

from eawf.render.artifact_chassis import render_provenance, render_references, render_scrub_status
from eawf.state.models import Audit


def render_audit_markdown(audit: Audit) -> str:
    """Render audit metadata in canonical body order."""
    verdict = audit.verdict.value if audit.verdict else "n/a"
    lines: list[str] = [
        f"# Audit: {audit.id}",
        "",
        f"> Kind: {audit.kind.value} · Status: {audit.status.value} · Verdict: {verdict}",
        "",
        "## Summary",
        "",
        f"- scope_id: {audit.scope_id}",
        f"- report_artifact_id: {audit.report_artifact_id or '-'}",
        "",
        "## Checks",
        "",
    ]
    if audit.check_results:
        for row in audit.check_results:
            if isinstance(row, dict):
                name = row.get("name", "unknown")
                passed = bool(row.get("passed", False))
                details = row.get("details")
            else:
                name = getattr(row, "name", "unknown")
                passed = bool(getattr(row, "passed", False))
                details = getattr(row, "details", None)
            marker = "pass" if passed else "fail"
            suffix = f" — {details}" if details else ""
            lines.append(f"- {marker}: {name}{suffix}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Integrity")
    lines.append("")
    if audit.integrity_results:
        for row in audit.integrity_results:
            lines.append(f"- {row}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.extend(render_references([]))
    lines.append("")
    lines.extend(
        render_provenance(
            kind=audit.kind.value,
            record_id=audit.id,
            scope_id=audit.scope_id,
        )
    )
    lines.append("")
    lines.extend(render_scrub_status())
    lines.append("")
    return "\n".join(lines)
