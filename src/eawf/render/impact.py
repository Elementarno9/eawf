"""Read-only file-impact graph renderer for ``eawf impact``.

Pure projection over a validated :class:`~eawf.state.models.State`. Each
:class:`~eawf.state.models.Decision` is joined to the waves that landed
under its scope and the file globs those waves touch (``wave.file_scopes``).

Join rule:

::

    decision (scope_id=PXX)
        → iters (phase_id=PXX)
            → waves (iter_id=PXX-INN)
                → wave.file_scopes

The join is exact-match on phase id when ``decision.scope_id`` matches a
known phase id; otherwise the decision is rendered with an empty file-glob
list. Decisions are EAWF-scoped in v0.2 so the typical caller passes
``--decision=D01`` and uses the substring match on the decision's
``summary`` to surface phase-relevant impact.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from eawf.state.ids import is_phase_id
from eawf.state.models import State

logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    """Base model: forbids unknown keys (rule 2)."""

    model_config = ConfigDict(extra="forbid")


class ImpactNode(_StrictModel):
    """One impact entry: decision → file globs (deduped, id-sorted waves)."""

    decision_id: str
    decision_summary: str
    scope_id: str
    wave_ids: list[str] = Field(default_factory=list)
    file_globs: list[str] = Field(default_factory=list)


class ImpactGraph(_StrictModel):
    """Materialised impact graph."""

    nodes: list[ImpactNode] = Field(default_factory=list)


def _phase_ids_for_scope(state: State, scope_id: str) -> set[str]:
    """Return the phase ids matching *scope_id*.

    A scope id may be a phase id directly (e.g. ``"P06"``), the project
    code (every closed phase), or a free-form scope tag (no match).
    """
    if is_phase_id(scope_id):
        return {scope_id}
    project_code = state.project.code if state.project is not None else None
    if scope_id == project_code:
        return set(state.phases.keys())
    return set()


def build_impact_graph(state: State, *, decision_id: str | None = None) -> ImpactGraph:
    """Project ``state`` into an :class:`ImpactGraph`.

    Args:
        state: Loaded :class:`State`.
        decision_id: Optional filter — when set, only the named decision
            contributes a node. Missing ids yield an empty graph (the CLI
            surface can emit a "no impact" placeholder).
    """
    decisions = state.decisions or {}
    if decision_id is not None:
        d = decisions.get(decision_id)
        candidates = [d] if d is not None else []
    else:
        candidates = sorted(decisions.values(), key=lambda r: r.id)

    iters = state.iters or {}
    waves = state.waves or {}

    nodes: list[ImpactNode] = []
    for d in candidates:
        if d is None:
            continue
        scope_phases = _phase_ids_for_scope(state, d.scope_id)
        if not scope_phases:
            nodes.append(
                ImpactNode(
                    decision_id=d.id,
                    decision_summary=d.summary,
                    scope_id=d.scope_id,
                    wave_ids=[],
                    file_globs=[],
                )
            )
            continue
        scope_iters = {it.id for it in iters.values() if it.phase_id in scope_phases}
        scope_waves = sorted(
            (w for w in waves.values() if w.iter_id in scope_iters),
            key=lambda r: r.id,
        )
        wave_ids = [w.id for w in scope_waves]
        file_globs = sorted({g for w in scope_waves for g in w.file_scopes})
        nodes.append(
            ImpactNode(
                decision_id=d.id,
                decision_summary=d.summary,
                scope_id=d.scope_id,
                wave_ids=wave_ids,
                file_globs=file_globs,
            )
        )
    return ImpactGraph(nodes=nodes)


def render_text(graph: ImpactGraph) -> str:
    """Render *graph* as a deterministic text block."""
    if not graph.nodes:
        return "(no impact entries)"
    lines: list[str] = []
    for node in graph.nodes:
        lines.append(f"{node.decision_id} [{node.scope_id}]  {node.decision_summary}")
        if node.wave_ids:
            lines.append(f"  waves: {', '.join(node.wave_ids)}")
        else:
            lines.append("  waves: (none)")
        if node.file_globs:
            for glob in node.file_globs:
                lines.append(f"  - {glob}")
        else:
            lines.append("  - (no file scopes recorded)")
    return "\n".join(lines)


def _dot_edge(src: str, dst: str, *, label: str = "") -> str:
    """Shared DOT edge formatter — extracted for reuse in :mod:`render.decision_graph`."""
    if label:
        return f'  "{src}" -> "{dst}" [label="{label}"];'
    return f'  "{src}" -> "{dst}";'


def render_dot(graph: ImpactGraph) -> str:
    """Render *graph* as a ``digraph impact { ... }`` block."""
    lines: list[str] = ["digraph impact {", "  rankdir=LR;"]
    seen_files: set[str] = set()
    for node in graph.nodes:
        lines.append(f'  "{node.decision_id}" [label="{node.decision_id}\\n{node.scope_id}"];')
        for wave_id in node.wave_ids:
            lines.append(f'  "{wave_id}" [shape=box];')
            lines.append(_dot_edge(node.decision_id, wave_id))
            for glob in node.file_globs:
                file_node = f"file::{glob}"
                if file_node not in seen_files:
                    lines.append(f'  "{file_node}" [shape=note, label="{glob}"];')
                    seen_files.add(file_node)
                lines.append(_dot_edge(wave_id, file_node))
    lines.append("}")
    return "\n".join(lines)


__all__ = [
    "ImpactGraph",
    "ImpactNode",
    "build_impact_graph",
    "render_dot",
    "render_text",
]
