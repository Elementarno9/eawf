"""Distil ``state.json`` into a per-phase Markdown narrative ("project wiki").

Output structure (one section per closed phase, in phase-id order):

::

    # P00 — Repo bootstrap

    Audit `A01-P00` verdict: **pass**.

    ## Iters
    - P00-I01: ...

    ## Deliverables
    | Wave | Title | Commit | Outcome |
    |------|-------|--------|---------|

The top of the document carries a global "Decisions" section listing every
:class:`~eawf.kernel.state.models.Decision` in the project. The wiki is intentionally
read-only — it ships zero ``state.json`` mutations.
"""

from __future__ import annotations

import logging

from eawf.kernel.state.enums import PhaseStatus
from eawf.kernel.state.models import State
from eawf.lifecycle.wave_sha import derive_wave_sha

logger = logging.getLogger(__name__)


def _short_sha(value: str | None) -> str:
    return value[:7] if value else "(pending)"


def _phase_section(state: State, phase_id: str) -> list[str]:
    phases = state.phases or {}
    phase = phases[phase_id]
    iters = state.iters or {}
    waves = state.waves or {}
    audits = state.audits or {}

    lines: list[str] = [f"# {phase_id} — {phase.title}", ""]
    if phase.audit_id and (audit := audits.get(phase.audit_id)) is not None:
        verdict = audit.verdict.value if audit.verdict is not None else "(pending)"
        lines.append(f"Audit `{phase.audit_id}` verdict: **{verdict}**.")
        lines.append("")

    phase_iters = sorted(
        (it for it in iters.values() if it.phase_id == phase_id),
        key=lambda r: r.id,
    )
    if phase_iters:
        lines.extend(["## Iters", ""])
        for it in phase_iters:
            lines.append(f"- `{it.id}`: {it.title}")
        lines.append("")

    iter_ids = {it.id for it in phase_iters}
    phase_waves = sorted(
        (w for w in waves.values() if w.iter_id in iter_ids),
        key=lambda r: r.id,
    )
    if phase_waves:
        lines.extend(
            [
                "## Deliverables",
                "",
                "| Wave | Title | Commit | Outcome |",
                "|------|-------|--------|---------|",
            ]
        )
        for w in phase_waves:
            outcome = (w.outcome or "").replace("|", "\\|")
            title = w.title.replace("|", "\\|")
            lines.append(
                f"| `{w.id}` | {title} | `{_short_sha(derive_wave_sha(w.id))}` | {outcome} |"
            )
        lines.append("")
    return lines


def build_wiki(state: State) -> str:
    """Render the full project wiki as a single Markdown string.

    Sections are emitted in phase-id order. Only phases whose status is
    :class:`~eawf.kernel.state.enums.PhaseStatus.CLOSED` are included; open phases
    are omitted so the wiki is a stable historical record.
    """
    decisions = state.decisions or {}
    lines: list[str] = []
    if state.project is not None:
        lines.append(f"# {state.project.code} — {state.project.title}")
        if state.project.description:
            lines.extend(["", state.project.description])
        lines.append("")

    if decisions:
        lines.extend(["## Decisions", ""])
        for d in sorted(decisions.values(), key=lambda r: r.id):
            status = d.status.value if hasattr(d.status, "value") else str(d.status)
            lines.append(f"- `{d.id}` [{status}] {d.title}")
        lines.append("")

    phases = state.phases or {}
    closed_phases = sorted(
        (p for p in phases.values() if p.status == PhaseStatus.CLOSED),
        key=lambda r: r.id,
    )
    for phase in closed_phases:
        lines.extend(_phase_section(state, phase.id))
    return "\n".join(lines) + ("\n" if not lines or lines[-1] != "" else "")


__all__ = ["build_wiki"]
