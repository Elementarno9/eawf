"""Read-only decision-graph renderer for ``eawf decision graph``.

Pure projection over a validated :class:`~eawf.kernel.state.models.State`. Builds a
:class:`DecisionGraph` (nodes + edges) and renders it as deterministic text,
Graphviz DOT, or Mermaid markdown.

Edges in v0.2 come from a single source: ``Decision.superseded_by`` (when the
referenced target id exists in ``state.decisions``). Future extensions (audit →
decision, hypothesis → decision) hang off this module without changing the
public render signatures.

The module never opens locks, never writes to disk, and never mutates state.
Public API:

- :func:`build_decision_graph` — projection from ``State`` to ``DecisionGraph``.
- :func:`render_text` / :func:`render_dot` / :func:`render_mermaid` — three
  deterministic emitters.
- :class:`DecisionGraph` / :class:`DecisionNode` / :class:`DecisionEdge` —
  Pydantic ``extra="forbid"`` projections.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


# Mermaid identifiers cannot contain hyphens; sanitise via "-" -> "_".
_MERMAID_ID_RE = re.compile(r"-")


class _StrictModel(BaseModel):
    """Base model: forbids unknown keys (rule 2)."""

    model_config = ConfigDict(extra="forbid")


class DecisionNode(_StrictModel):
    """One decision projected into the graph."""

    id: str
    summary: str
    status: str
    scope_id: str


class DecisionEdge(_StrictModel):
    """One directed edge between two decisions."""

    src: str
    dst: str
    kind: str = "superseded_by"


class DecisionGraph(_StrictModel):
    """Materialised decision graph."""

    nodes: list[DecisionNode] = Field(default_factory=list)
    edges: list[DecisionEdge] = Field(default_factory=list)


def build_decision_graph(state: State) -> DecisionGraph:
    """Project ``state.decisions`` into a :class:`DecisionGraph`.

    Nodes are sorted by id for deterministic output. Edges are populated only
    when ``Decision.superseded_by`` references an id that exists in
    ``state.decisions``; dangling references are silently skipped (the
    schema-level invariant covers the dangling-link surface separately).
    """
    decisions = state.decisions or {}
    nodes = [
        DecisionNode(
            id=d.id,
            summary=d.title,
            status=d.status.value if hasattr(d.status, "value") else str(d.status),
            scope_id=d.scope_id,
        )
        for d in sorted(decisions.values(), key=lambda r: r.id)
    ]
    edges: list[DecisionEdge] = []
    for d in sorted(decisions.values(), key=lambda r: r.id):
        if d.superseded_by and d.superseded_by in decisions:
            edges.append(DecisionEdge(src=d.id, dst=d.superseded_by, kind="superseded_by"))
    return DecisionGraph(nodes=nodes, edges=edges)


def render_text(graph: DecisionGraph) -> str:
    """Render *graph* as a deterministic text block.

    Empty graph emits a one-line ``"(no decisions)"`` placeholder so the CLI
    surface stays diff-friendly.
    """
    if not graph.nodes:
        return "(no decisions)"
    lines: list[str] = [f"Decision graph ({len(graph.nodes)} nodes, {len(graph.edges)} edges):"]
    for n in graph.nodes:
        lines.append(f"  {n.id} [{n.status}]  {n.summary}")
    lines.append("Edges:")
    if not graph.edges:
        lines.append("  (none)")
    else:
        for e in graph.edges:
            lines.append(f"  {e.src} --{e.kind}--> {e.dst}")
    return "\n".join(lines)


def _dot_escape_quotes(value: str) -> str:
    """Escape only quote chars for embedding inside a DOT-quoted string literal.

    The DOT label syntax treats ``\\n`` as a line break, so we do NOT escape
    backslashes — callers may use ``\\n`` to split a label across rows.
    """
    return value.replace('"', '\\"')


def render_dot(graph: DecisionGraph) -> str:
    """Render *graph* as a Graphviz ``digraph decisions { ... }`` block."""
    lines: list[str] = ["digraph decisions {", "  rankdir=LR;"]
    for n in graph.nodes:
        summary = _dot_escape_quotes(n.summary)
        lines.append(f'  "{n.id}" [label="{n.id}\\n{summary}"];')
    for e in graph.edges:
        lines.append(f'  "{e.src}" -> "{e.dst}" [label="{e.kind}"];')
    lines.append("}")
    return "\n".join(lines)


def _mermaid_id(decision_id: str) -> str:
    """Sanitise *decision_id* for use as a Mermaid node identifier."""
    return _MERMAID_ID_RE.sub("_", decision_id)


def render_mermaid(graph: DecisionGraph) -> str:
    """Render *graph* as a Mermaid ``graph TD`` block."""
    lines: list[str] = ["graph TD"]
    for n in graph.nodes:
        nid = _mermaid_id(n.id)
        summary = n.summary.replace('"', "'")
        lines.append(f'  {nid}["{n.id}: {summary}"]')
    for e in graph.edges:
        lines.append(f"  {_mermaid_id(e.src)} -->|{e.kind}| {_mermaid_id(e.dst)}")
    return "\n".join(lines)
