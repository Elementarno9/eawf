"""Read-only ``plan_view`` renderer for ``eawf plan show``.

Pure projection over a validated :class:`~eawf.kernel.state.models.State`. Builds a
:class:`PlanView` that captures every aspect of an active iter (waves, DAG,
acceptance checks, composed risks, summary) and renders it as either a
deterministic markdown body or a JSON envelope.

The module never opens locks, never appends to JSONL stores, and never
mutates state. Its public API:

- :func:`build_view` — projection from ``State`` + iter id to ``PlanView``.
- :func:`render_markdown` — deterministic markdown body (sections in fixed
  order; mermaid DAG by default, ASCII opt-out).
- :func:`render_json` — dict matching ``plan-view.schema.json``.
- :class:`PlanSection` — section selector enum.
- :class:`PlanView` — Pydantic model carrying the projection.

Design notes:

- *Risks* are not a first-class entity. They are composed from open
  ``BacklogItem`` (P0/P1), open ``Incident``, and rejected ``Hypothesis``,
  filtered by iter / phase / wave scope ids.
- *Audit check_results* shape is best-effort. ``state.audits[*].check_results``
  is typed ``list[Any]`` (state/models.py:244) but the store payload
  (store/kinds/audit.py) is ``list[CheckResult]``. The defensive parser
  accepts both: a dict shape ``{"name", "passed", "details"}`` and a
  ``CheckResult``-like object exposing ``.name``/``.passed``/``.details``
  attributes. Mismatched rows are logged + skipped, never raised.
- The markdown DAG defaults to mermaid (richer in GitHub PR previews);
  ``--ascii`` opt-out swaps the block for an indented adjacency list.
  Mermaid identifiers cannot contain hyphens, so wave-id node ids are
  sanitised via ``-`` → ``_``; the canonical hyphenated id stays in the
  visible label.
- Cycles never raise. ``build_view`` returns ``cycle = [...]`` and
  ``topo_order = None`` so the caller can render a warning instead of
  aborting; ``eawf validate`` is the canonical surface for cycle errors
  (exit 4).
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.state.enums import (
    BacklogPriority,
    BacklogStatus,
    HypothesisVerdict,
    IncidentSeverity,
    IncidentStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import (
    Audit,
    BacklogItem,
    Hypothesis,
    Incident,
    Iter,
    Phase,
    State,
    Wave,
)
from eawf.workflow.estimation.buckets import (
    critical_path_eu,
    sum_wave_eu,
    wave_estimate_eu,
)
from eawf.workflow.estimation.metrics import (
    RealisticWallClockMetric,
    compute_realistic_wall_clock,
)
from eawf.workflow.lifecycle.wave_sha import derive_wave_sha

logger = logging.getLogger(__name__)


# ---- Section selector ------------------------------------------------------


class PlanSection(StrEnum):
    """Markdown / JSON section selector for ``--show <section>``."""

    ALL = "all"
    DAG = "dag"
    CHECKS = "checks"
    RISKS = "risks"
    WAVES = "waves"


# ---- View models -----------------------------------------------------------


class _StrictModel(BaseModel):
    """Base model: forbids unknown keys (rule 2)."""

    model_config = ConfigDict(extra="forbid")


class IterView(_StrictModel):
    """Compact projection of :class:`~eawf.kernel.state.models.Iter`."""

    id: str
    phase_id: str
    title: str
    status: str
    opened_at: str
    closed_at: str | None = None
    audit_id: str | None = None
    estimate_id: str | None = None


class PhaseView(_StrictModel):
    """Compact projection of :class:`~eawf.kernel.state.models.Phase`."""

    id: str
    title: str
    status: str


class WaveView(_StrictModel):
    """Compact projection of :class:`~eawf.kernel.state.models.Wave`."""

    id: str
    title: str
    status: str
    deps: list[str]
    file_scopes: list[str]
    success_criteria: list[str]
    agent_role: str | None = None
    effort_bucket: str | None = None
    estimate_eu: float = 0.0
    claim_session_id: str | None = None
    commit: str | None = None
    outcome: str | None = None
    # The wave's optional long-form purpose (the ≤500-char field W23 split
    # off the bounded ≤72-char ``title``). A markdown-only detail-render
    # field: ``render_markdown`` surfaces it under the waves table, but
    # ``exclude=True`` drops it from both ``model_dump`` (the JSON envelope)
    # and the serialization JSON schema — keeping ``plan-view.schema.json``
    # (WaveView is ``additionalProperties: false``) unchanged.
    description: str | None = Field(default=None, exclude=True)


class DagNode(_StrictModel):
    """A node in the DAG view."""

    id: str
    status: str


class DagEdge(_StrictModel):
    """A directed edge in the DAG view (``from`` → ``to``)."""

    from_: str = Field(alias="from")
    to: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DagView(_StrictModel):
    """The DAG projection: nodes + edges + topo order or cycle."""

    nodes: list[DagNode]
    edges: list[DagEdge]
    topo_order: list[str] | None
    cycle: list[str] | None


class CheckView(_StrictModel):
    """A single acceptance check surfaced in the plan view."""

    source: str  # "iter_audit" | "wave_audit" | "wave_outcome"
    audit_id: str | None
    wave_id: str | None
    name: str
    passed: bool
    details: str | None = None


class RiskView(_StrictModel):
    """A composed risk row (backlog | incident | hypothesis_rejected)."""

    kind: str  # "backlog" | "incident" | "hypothesis_rejected"
    id: str
    severity: str | None
    title: str
    status: str


class SummaryView(_StrictModel):
    """High-level counts and blocked-wave list."""

    wave_count: int
    wave_status_counts: dict[str, int]
    check_count: int
    check_passed: int
    risk_count: int
    blocked_waves: list[str]
    sum_wave_eu: float = 0.0
    critical_path_eu: float = 0.0
    actual_elapsed_eu: float = 0.0


class PlanView(_StrictModel):
    """Top-level container for an iter plan view."""

    iter: IterView
    phase: PhaseView | None
    waves: list[WaveView]
    dag: DagView
    checks: list[CheckView]
    risks: list[RiskView]
    summary: SummaryView


# ---- Errors ---------------------------------------------------------------


class PlanViewNotFound(LookupError):  # noqa: N818 — semantic name, not an *Error* class
    """Raised by :func:`build_view` when *iter_id* is not in ``state.iters``."""


# ---- Helpers --------------------------------------------------------------


def _isoformat(value: Any) -> str:
    """Return an ISO-8601 string for *value* (datetime or already-string)."""
    if hasattr(value, "isoformat"):
        out: str = value.isoformat()
        return out.replace("+00:00", "Z")
    return str(value)


def _isoformat_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return _isoformat(value)


def _sanitise_mermaid_id(wave_id: str) -> str:
    """Replace hyphens with underscores for mermaid node ids.

    Mermaid does not accept hyphens in raw identifiers. The visible label
    keeps the canonical hyphenated id; only the node identifier is
    sanitised.
    """
    return wave_id.replace("-", "_")


def _kahn_topo(
    nodes: list[str], edges: list[tuple[str, str]]
) -> tuple[list[str] | None, list[str] | None]:
    """Kahn's topological sort.

    Returns ``(topo_order, None)`` for an acyclic graph, or
    ``(None, cycle_nodes)`` when one or more nodes participate in a cycle.
    Determinism: nodes are processed in lexicographic order at each layer
    so the output stays stable across dict iteration orderings.
    """
    indeg: dict[str, int] = dict.fromkeys(nodes, 0)
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for src, dst in edges:
        if src not in adj or dst not in indeg:
            continue
        adj[src].append(dst)
        indeg[dst] += 1
    # Sort each adjacency list once for determinism.
    for src in adj:
        adj[src].sort()
    queue: deque[str] = deque(sorted(n for n, d in indeg.items() if d == 0))
    order: list[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for child in adj[n]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
        # Re-sort the queue so siblings come out lexicographically.
        items = sorted(queue)
        queue = deque(items)
    if len(order) != len(nodes):
        cycle_nodes = sorted(n for n in nodes if n not in order)
        return None, cycle_nodes
    return order, None


def _scope_ids_for_iter(iter_obj: Iter, waves: list[Wave]) -> set[str]:
    """Return the set of scope ids that count as 'within this iter'."""
    scopes: set[str] = {iter_obj.id, iter_obj.phase_id}
    scopes.update(w.id for w in waves)
    return scopes


def _parse_check_result(
    raw: Any,
    *,
    source: str,
    audit_id: str | None,
    wave_id: str | None,
) -> CheckView | None:
    """Parse a single audit ``check_results`` row into a :class:`CheckView`.

    The store-side payload (``store/kinds/audit.CheckResult``) is a Pydantic
    object with ``name``, ``passed``, ``details`` attributes. The state-side
    projection (``state.audits[*].check_results``) is typed ``list[Any]``,
    so callers may have stored either dicts or model instances. This parser
    accepts both shapes and returns ``None`` (logged) on a mismatch — never
    raises.
    """
    name: Any = None
    passed: Any = None
    details: Any = None
    if isinstance(raw, dict):
        name = raw.get("name")
        passed = raw.get("passed")
        details = raw.get("details")
    else:
        name = getattr(raw, "name", None)
        passed = getattr(raw, "passed", None)
        details = getattr(raw, "details", None)
    if not isinstance(name, str) or not isinstance(passed, bool):
        logger.warning(
            f"_parse_check_result skipping malformed check_result audit_id={audit_id!r} "
            f"wave={wave_id!r} raw={raw!r}"
        )
        return None
    if details is not None and not isinstance(details, str):
        details = str(details)
    return CheckView(
        source=source,
        audit_id=audit_id,
        wave_id=wave_id,
        name=name,
        passed=passed,
        details=details,
    )


# ---- Risk composition -----------------------------------------------------


_SEVERITY_RANK: dict[str, int] = {
    # Order: P0 backlog > critical incident > high incident > P1 backlog
    # > medium incident > rejected hypothesis > rest. Lower rank wins.
    "P0": 0,
    "critical": 1,
    "high": 2,
    "P1": 3,
    "medium": 4,
    "rejected": 5,
    "low": 6,
}

_KIND_RANK: dict[str, int] = {
    "backlog": 0,
    "incident": 1,
    "hypothesis_rejected": 2,
}


def _risk_sort_key(r: RiskView) -> tuple[int, int, str]:
    sev_rank = _SEVERITY_RANK.get(r.severity or "", 99)
    kind_rank = _KIND_RANK.get(r.kind, 99)
    return (sev_rank, kind_rank, r.id)


def _collect_risks(
    state: State,
    iter_obj: Iter,
    waves: list[Wave],
) -> list[RiskView]:
    """Compose the risk list from BacklogItem + Incident + Hypothesis."""
    scopes = _scope_ids_for_iter(iter_obj, waves)
    out: list[RiskView] = []

    # Backlog items: open OR in_progress, P0/P1, scope inside iter.
    if state.backlog:
        for bl in state.backlog.values():
            out.extend(_backlog_to_risk(bl, scopes))

    # Incidents: open or mitigated, scope inside iter.
    if state.incidents:
        for inc in state.incidents.values():
            out.extend(_incident_to_risk(inc, scopes))

    # Hypotheses: rejected verdict, scope inside iter.
    if state.hypotheses:
        for h in state.hypotheses.values():
            out.extend(_hypothesis_to_risk(h, scopes))

    out.sort(key=_risk_sort_key)
    return out


def _backlog_to_risk(bl: BacklogItem, scopes: set[str]) -> list[RiskView]:
    if bl.scope_id not in scopes:
        return []
    if bl.priority not in (BacklogPriority.P0, BacklogPriority.P1):
        return []
    if bl.status not in (BacklogStatus.OPEN, BacklogStatus.IN_PROGRESS):
        return []
    return [
        RiskView(
            kind="backlog",
            id=bl.id,
            severity=bl.priority.value,
            title=bl.title,
            status=bl.status.value,
        )
    ]


def _incident_to_risk(inc: Incident, scopes: set[str]) -> list[RiskView]:
    if inc.scope_id not in scopes:
        return []
    if inc.status not in (IncidentStatus.OPEN, IncidentStatus.MITIGATED):
        return []
    sev_str: str | None = inc.severity.value if isinstance(inc.severity, IncidentSeverity) else None
    return [
        RiskView(
            kind="incident",
            id=inc.id,
            severity=sev_str,
            title=inc.title,
            status=inc.status.value,
        )
    ]


def _hypothesis_to_risk(h: Hypothesis, scopes: set[str]) -> list[RiskView]:
    if h.scope_id not in scopes:
        return []
    if h.verdict != HypothesisVerdict.REJECTED:
        return []
    return [
        RiskView(
            kind="hypothesis_rejected",
            id=h.id,
            severity="rejected",
            title=h.title,
            status=h.status.value,
        )
    ]


# ---- Build view -----------------------------------------------------------


def _wave_status_counts(waves: list[Wave]) -> dict[str, int]:
    """Return zero-default counts for every WaveStatus value."""
    counts: dict[str, int] = {st.value: 0 for st in WaveStatus}
    for w in waves:
        counts[w.status.value] += 1
    return counts


def _blocked_waves(waves: list[Wave]) -> list[str]:
    """Return wave ids that are PENDING with at least one un-closed dep.

    A wave is *blocked* when its own status is ``pending`` and any dep wave
    has a status other than ``closed``. Waves whose deps point at ids
    missing from the iter are treated as un-closed (defensive: dangling refs
    block the wave, the validator surfaces the dangling-ref error).
    """
    by_id = {w.id: w for w in waves}
    out: list[str] = []
    for w in waves:
        if w.status != WaveStatus.PENDING:
            continue
        for dep in w.deps:
            dep_w = by_id.get(dep)
            if dep_w is None or dep_w.status != WaveStatus.CLOSED:
                out.append(w.id)
                break
    return sorted(out)


def _collect_checks(
    state: State,
    iter_obj: Iter,
    waves: list[Wave],
) -> list[CheckView]:
    """Surface every relevant check_result + synthetic wave-outcome rows."""
    checks: list[CheckView] = []

    # Iter-level audit.
    if iter_obj.audit_id and state.audits and iter_obj.audit_id in state.audits:
        audit: Audit = state.audits[iter_obj.audit_id]
        for raw in audit.check_results:
            cv = _parse_check_result(
                raw,
                source="iter_audit",
                audit_id=audit.id,
                wave_id=None,
            )
            if cv is not None:
                checks.append(cv)

    # Wave-level audits: any audit with scope_id matching a wave id.
    if state.audits:
        wave_ids = {w.id for w in waves}
        # Sort by audit id so output is deterministic.
        for audit in sorted(state.audits.values(), key=lambda a: a.id):
            if audit.scope_id not in wave_ids:
                continue
            for raw in audit.check_results:
                cv = _parse_check_result(
                    raw,
                    source="wave_audit",
                    audit_id=audit.id,
                    wave_id=audit.scope_id,
                )
                if cv is not None:
                    checks.append(cv)

    # Wave outcome strings — synthetic checks for closed waves.
    for w in waves:
        if w.status == WaveStatus.CLOSED and w.outcome:
            checks.append(
                CheckView(
                    source="wave_outcome",
                    audit_id=None,
                    wave_id=w.id,
                    name=w.outcome,
                    passed=True,
                    details=None,
                )
            )

    return checks


def build_view(state: State, iter_id: str) -> PlanView:
    """Project *state* into a :class:`PlanView` for *iter_id*.

    Raises:
        PlanViewNotFound: if *iter_id* is not present in ``state.iters``.
    """
    iter_obj = state.iters.get(iter_id) if state.iters else None
    if iter_obj is None:
        raise PlanViewNotFound(f"iter {iter_id!r} not in state.iters")

    phase_obj: Phase | None = state.phases.get(iter_obj.phase_id) if state.phases else None

    waves: list[Wave] = []
    for wid in iter_obj.wave_ids:
        if wid in state.waves:
            waves.append(state.waves[wid])
        else:
            # Dangling refs are silently skipped at the plan-view layer
            # (the canonical surface for cycle / dangling-ref errors is
            # ``eawf validate``); the debug log lets an operator
            # cross-reference the absence without re-running validate.
            logger.debug(f"build_view dangling wave={wid!r} skipped iter={iter_obj.id!r}")

    iter_view = IterView(
        id=iter_obj.id,
        phase_id=iter_obj.phase_id,
        title=iter_obj.title,
        status=iter_obj.status.value,
        opened_at=_isoformat(iter_obj.opened_at),
        closed_at=_isoformat_or_none(iter_obj.closed_at),
        audit_id=iter_obj.audit_id,
        estimate_id=iter_obj.estimate_id,
    )
    phase_view = (
        PhaseView(id=phase_obj.id, title=phase_obj.title, status=phase_obj.status.value)
        if phase_obj is not None
        else None
    )

    wave_views = [
        WaveView(
            id=w.id,
            title=w.title,
            status=w.status.value,
            deps=list(w.deps),
            file_scopes=list(w.file_scopes),
            success_criteria=list(w.success_criteria),
            agent_role=w.agent_role.value if w.agent_role else None,
            effort_bucket=w.effort_bucket.value if w.effort_bucket else None,
            estimate_eu=wave_estimate_eu(w),
            claim_session_id=w.claim_session_id,
            commit=derive_wave_sha(w.id),
            outcome=w.outcome,
            description=w.description,
        )
        for w in waves
    ]

    wave_id_set = {w.id for w in waves}
    nodes = [DagNode(id=w.id, status=w.status.value) for w in waves]
    edges: list[DagEdge] = []
    for w in waves:
        for dep in w.deps:
            if dep in wave_id_set:
                edges.append(DagEdge.model_validate({"from": dep, "to": w.id}))
    topo_order, cycle = _kahn_topo([w.id for w in waves], [(e.from_, e.to) for e in edges])

    dag_view = DagView(nodes=nodes, edges=edges, topo_order=topo_order, cycle=cycle)

    checks = _collect_checks(state, iter_obj, waves)
    risks = _collect_risks(state, iter_obj, waves)
    blocked = _blocked_waves(waves)

    summary = SummaryView(
        wave_count=len(waves),
        wave_status_counts=_wave_status_counts(waves),
        check_count=len(checks),
        check_passed=sum(1 for c in checks if c.passed),
        risk_count=len(risks),
        blocked_waves=blocked,
        sum_wave_eu=sum_wave_eu(waves),
        critical_path_eu=critical_path_eu(waves),
        actual_elapsed_eu=sum(
            a.elapsed_eu for wid, a in (state.actuals or {}).items() if wid in wave_id_set
        ),
    )

    return PlanView(
        iter=iter_view,
        phase=phase_view,
        waves=wave_views,
        dag=dag_view,
        checks=checks,
        risks=risks,
        summary=summary,
    )


# ---- JSON rendering -------------------------------------------------------


def render_json(
    view: PlanView,
    *,
    sections: PlanSection = PlanSection.ALL,
) -> dict[str, Any]:
    """Return a JSON-serialisable dict matching ``plan-view.schema.json``.

    The header (``iter``, ``phase``, ``summary``) is always present.
    ``sections`` restricts the body keys (``waves``, ``dag``, ``checks``,
    ``risks``); when ``sections != ALL`` the omitted lists are emitted as
    empty containers / placeholder objects so the envelope shape stays
    stable.
    """
    iter_dict = view.iter.model_dump(mode="json")
    phase_dict = view.phase.model_dump(mode="json") if view.phase is not None else None

    # ``WaveView.description`` is declared ``exclude=True`` so ``model_dump``
    # already drops it here — the JSON envelope stays pinned to
    # ``plan-view.schema.json``. The field surfaces in markdown only.
    waves_dict = [w.model_dump(mode="json") for w in view.waves]
    dag_dict = {
        "nodes": [n.model_dump(mode="json") for n in view.dag.nodes],
        "edges": [e.model_dump(by_alias=True, mode="json") for e in view.dag.edges],
        "topo_order": view.dag.topo_order,
        "cycle": view.dag.cycle,
    }
    checks_dict = [c.model_dump(mode="json") for c in view.checks]
    risks_dict = [r.model_dump(mode="json") for r in view.risks]
    summary_dict = view.summary.model_dump(mode="json")

    if sections == PlanSection.ALL:
        return {
            "iter": iter_dict,
            "phase": phase_dict,
            "waves": waves_dict,
            "dag": dag_dict,
            "checks": checks_dict,
            "risks": risks_dict,
            "summary": summary_dict,
        }

    # Restricted sections: keep header + summary, only the named section's
    # body is populated; other body keys are emitted empty so the envelope
    # remains shape-stable.
    empty_dag: dict[str, Any] = {
        "nodes": [],
        "edges": [],
        "topo_order": None,
        "cycle": None,
    }
    base: dict[str, Any] = {
        "iter": iter_dict,
        "phase": phase_dict,
        "waves": [],
        "dag": empty_dag,
        "checks": [],
        "risks": [],
        "summary": summary_dict,
    }
    if sections == PlanSection.WAVES:
        base["waves"] = waves_dict
    elif sections == PlanSection.DAG:
        base["dag"] = dag_dict
    elif sections == PlanSection.CHECKS:
        base["checks"] = checks_dict
    elif sections == PlanSection.RISKS:
        base["risks"] = risks_dict
    return base


# ---- Markdown rendering ---------------------------------------------------


_WAVE_STATUS_DISPLAY: dict[str, str] = {
    "pending": "pending",
    "claimed": "claimed",
    "in_progress": "in_progress",
    "closed": "closed",
    "failed": "failed",
    "abandoned": "abandoned",
}


def _format_summary(view: PlanView) -> list[str]:
    counts = view.summary.wave_status_counts
    breakdown_pieces = [f"{n} {st}" for st, n in counts.items() if n]
    breakdown = ", ".join(breakdown_pieces) if breakdown_pieces else "none"
    risk_total = view.summary.risk_count
    blocked = view.summary.blocked_waves

    lines: list[str] = ["## Summary"]
    lines.append(f"- waves: {view.summary.wave_count} ({breakdown})")
    lines.append(
        "- effort: "
        f"sum_wave_eu={view.summary.sum_wave_eu:g}, "
        f"critical_path_eu={view.summary.critical_path_eu:g}, "
        f"actual_elapsed_eu={view.summary.actual_elapsed_eu:g}"
    )
    lines.append(f"- checks: {view.summary.check_passed}/{view.summary.check_count} passed")
    lines.append(f"- risks: {risk_total} open")
    if blocked:
        lines.append(f"- blocked: {', '.join(blocked)}")
    else:
        lines.append("- blocked: none")
    return lines


def _format_dag_mermaid(view: PlanView) -> list[str]:
    lines: list[str] = ["## DAG"]
    if view.dag.cycle is not None:
        lines.append(f"WARNING: cycle detected: {' -> '.join(view.dag.cycle)}")
    lines.append("```mermaid")
    lines.append("flowchart LR")
    if not view.dag.nodes:
        lines.append("  %% no waves")
    else:
        for n in view.dag.nodes:
            mid = _sanitise_mermaid_id(n.id)
            label = f"{n.id} {n.status}"
            lines.append(f'  {mid}["{label}"]:::status_{n.status}')
        for e in view.dag.edges:
            src = _sanitise_mermaid_id(e.from_)
            dst = _sanitise_mermaid_id(e.to)
            lines.append(f"  {src} --> {dst}")
        # ClassDef block for status colours (kept stable across fixtures).
        lines.append("  classDef status_pending fill:#999,stroke:#444;")
        lines.append("  classDef status_claimed fill:#88a,stroke:#444;")
        lines.append("  classDef status_in_progress fill:#8a8,stroke:#444;")
        lines.append("  classDef status_closed fill:#0a0,stroke:#040;")
        lines.append("  classDef status_failed fill:#a00,stroke:#400;")
        lines.append("  classDef status_abandoned fill:#666,stroke:#222;")
    lines.append("```")
    return lines


def _format_dag_ascii(view: PlanView) -> list[str]:
    lines: list[str] = ["## DAG"]
    if view.dag.cycle is not None:
        lines.append(f"WARNING: cycle detected: {' -> '.join(view.dag.cycle)}")
        for n in sorted(view.dag.nodes, key=lambda x: x.id):
            lines.append(f"{n.id} ({n.status})")
        return lines

    if not view.dag.nodes:
        lines.append("(no waves)")
        return lines

    # Build adjacency from edges.
    children: dict[str, list[str]] = {n.id: [] for n in view.dag.nodes}
    parents: dict[str, list[str]] = {n.id: [] for n in view.dag.nodes}
    for e in view.dag.edges:
        children[e.from_].append(e.to)
        parents[e.to].append(e.from_)
    for k in children:
        children[k] = sorted(children[k])
    status_by_id = {n.id: n.status for n in view.dag.nodes}

    # Levelisation: BFS from roots (nodes with no parent in this iter).
    visited: set[str] = set()
    roots = sorted(n.id for n in view.dag.nodes if not parents[n.id])

    def _emit(node: str, depth: int) -> None:
        if node in visited:
            return
        visited.add(node)
        prefix = "  " * depth
        arrow = "-> " if depth > 0 else ""
        lines.append(f"{prefix}{arrow}{node} ({status_by_id[node]})")
        for child in children[node]:
            _emit(child, depth + 1)

    for r in roots:
        _emit(r, 0)
    # Cover any nodes not reachable from roots (orphans / cycle remnants).
    for n in sorted(view.dag.nodes, key=lambda x: x.id):
        if n.id not in visited:
            _emit(n.id, 0)
    return lines


def _format_waves(view: PlanView) -> list[str]:
    lines: list[str] = ["## Waves"]
    if not view.waves:
        lines.append("(none)")
        return lines
    lines.append("| Wave | Status | Bucket | Role | Estimate EU | Success criteria | Files |")
    lines.append("| --- | --- | --- | --- | ---: | --- | --- |")
    for w in view.waves:
        marker = "[x]" if w.status == "closed" else "[ ]"
        suffix_parts: list[str] = []
        if w.status == "closed":
            sha = (w.commit or "")[:7]
            suffix_parts.append(f"closed @ {sha}" if sha else "closed")
        elif w.status == "claimed" and w.claim_session_id:
            suffix_parts.append(f"claimed by {w.claim_session_id}")
        else:
            suffix_parts.append(w.status)
            if w.deps:
                deps_short = ", ".join(d.split("-")[-1] for d in w.deps)
                suffix_parts.append(f"deps: {deps_short}")
        suffix = "; ".join(suffix_parts)
        role = w.agent_role or "-"
        bucket = w.effort_bucket or "-"
        criteria = "<br>".join(w.success_criteria) if w.success_criteria else "-"
        files = "<br>".join(w.file_scopes) if w.file_scopes else "-"
        lines.append(
            f"| {marker} **{w.id}** {w.title} | {suffix} | {bucket} | {role} | "
            f"{w.estimate_eu:g} | {criteria} | {files} |"
        )
    detail_lines = [f"- **{w.id}** — {w.description}" for w in view.waves if w.description]
    if detail_lines:
        lines.append("")
        lines.append("### Wave details")
        lines.extend(detail_lines)
    return lines


def _format_checks(view: PlanView) -> list[str]:
    lines: list[str] = ["## Checks"]
    if not view.checks:
        lines.append("(none)")
        return lines
    for c in view.checks:
        marker = "[x]" if c.passed else "[ ]"
        if c.source == "iter_audit":
            origin = f"iter audit {c.audit_id or ''}".strip()
        elif c.source == "wave_audit":
            origin = f"{c.wave_id or ''} audit {c.audit_id or ''}".strip()
        elif c.source == "wave_outcome":
            origin = f"{c.wave_id or ''} outcome".strip()
        else:
            origin = c.source
        if c.passed:
            tail = "passed"
        elif c.details:
            tail = f"failed: {c.details}"
        else:
            tail = "failed"
        lines.append(f"- {marker} **{c.name}** ({origin}) — {tail}")
    return lines


def _format_risks(view: PlanView) -> list[str]:
    lines: list[str] = ["## Risks"]
    if not view.risks:
        lines.append("(none)")
        return lines
    lines.append("| ID | Kind | Severity | Title |")
    lines.append("| --- | --- | --- | --- |")
    for r in view.risks:
        sev = r.severity or ""
        lines.append(f"| {r.id} | {r.kind} | {sev} | {r.title} |")
    return lines


class RoadmapRow(_StrictModel):
    """One row in the roadmap-show table (P28-W18 unification).

    Compact projection of a phase that ``plan_view`` exposes to both the
    ``roadmap show --md`` CLI surface and the TUI roadmap tree — the
    two consumers walk the same typed row instead of re-walking
    ``state.phases`` independently. ``wave_count`` totals the phase's
    waves; ``depends_on`` mirrors :attr:`Phase.depends_on`.
    """

    id: str
    status: str
    title: str
    depends_on: list[str]
    wave_count: int
    iter_ids: list[str]
    source_brief_ids: list[str]
    release: str | None = None


EuRollupField = Literal["work_sum", "critical_path", "queue", "realistic"]
EuRollupDensity = Literal["full", "compact"]

_DEFAULT_EU_ROLLUP_FIELDS: tuple[EuRollupField, ...] = (
    "work_sum",
    "critical_path",
    "queue",
    "realistic",
)
_EU_ROLLUP_FIELD_ALIASES: dict[str, EuRollupField] = {
    "work-sum": "work_sum",
    "work_sum": "work_sum",
    "critical-path": "critical_path",
    "critical_path": "critical_path",
    "queue": "queue",
    "queued": "queue",
    "realistic": "realistic",
    "realistic-wall-clock": "realistic",
    "realistic_wall_clock": "realistic",
}


class EuViewConfig(_StrictModel):
    """Config consumed from ``tui.eu_view`` for roadmap EU rollups."""

    density: EuRollupDensity = "full"
    fields: tuple[EuRollupField, ...] = Field(default=_DEFAULT_EU_ROLLUP_FIELDS, min_length=1)


def build_roadmap_rows(state: State, *, phase_id_filter: str | None = None) -> list[RoadmapRow]:
    """Project *state* into the ordered roadmap-row list.

    Walks ``state.phases`` in ``id`` order (matching :mod:`roadmap` CLI
    behaviour); when *phase_id_filter* is set only the matching phase is
    returned. Wave count tallies every wave whose ``iter_id`` belongs to
    the phase's ``iter_ids``.

    Args:
        state: The validated state document.
        phase_id_filter: Restrict the rows to a single phase id, or
            ``None`` to project every phase.

    Returns:
        The ordered :class:`RoadmapRow` list (possibly empty).
    """
    rows: list[RoadmapRow] = []
    phases = sorted(state.phases.values(), key=lambda p: natural_key(p.id))
    if phase_id_filter is not None:
        phases = [p for p in phases if p.id == phase_id_filter]
    for phase in phases:
        iter_ids = [iid for iid in phase.iter_ids if iid in state.iters]
        iter_id_set = set(iter_ids)
        wave_count = sum(1 for w in state.waves.values() if w.iter_id in iter_id_set)
        rows.append(
            RoadmapRow(
                id=phase.id,
                status=phase.status.value,
                title=phase.title,
                depends_on=list(phase.depends_on),
                wave_count=wave_count,
                iter_ids=iter_ids,
                source_brief_ids=list(phase.source_brief_ids),
                release=phase.release,
            )
        )
    return rows


def _coerce_eu_rollup_fields(raw: Any) -> Any:
    """Normalise comma strings and hyphenated aliases before strict validation."""
    if isinstance(raw, str):
        values: list[Any] = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list | tuple):
        values = list(raw)
    else:
        return raw
    return [_EU_ROLLUP_FIELD_ALIASES.get(str(item), item) for item in values]


def _eu_view_config(config: Mapping[str, Any] | None) -> EuViewConfig:
    """Return strict ``tui.eu_view`` config with a safe default fallback."""
    raw: Any = None
    if isinstance(config, Mapping):
        tui = config.get("tui")
        if isinstance(tui, Mapping):
            raw = tui.get("eu_view")
    if raw is None:
        return EuViewConfig()
    if not isinstance(raw, Mapping):
        logger.warning(f"eu_view_config invalid_shape={type(raw).__name__!r}; using defaults")
        return EuViewConfig()
    normalised = dict(raw)
    if "fields" in normalised:
        normalised["fields"] = _coerce_eu_rollup_fields(normalised["fields"])
    try:
        return EuViewConfig.model_validate(normalised)
    except ValidationError as exc:
        err_type = exc.errors()[0]["type"]
        logger.warning(f"eu_view_config validation_error={err_type!r}; using defaults")
        return EuViewConfig()


def _phase_waves(state: State, phase_id: str) -> list[Wave]:
    """Return waves under *phase_id* in phase/iter order."""
    phase = state.phases.get(phase_id) if state.phases else None
    if phase is None:
        return []
    waves: list[Wave] = []
    for iter_id in phase.iter_ids:
        iter_obj = state.iters.get(iter_id) if state.iters else None
        if iter_obj is None:
            continue
        for wave_id in iter_obj.wave_ids:
            wave = state.waves.get(wave_id) if state.waves else None
            if wave is not None:
                waves.append(wave)
    return waves


def _lookup_by_key_or_scope(rows: Mapping[str, Any], scope_id: str) -> Any | None:
    """Return a state summary row keyed by *scope_id* or carrying it as ``scope_id``."""
    direct = rows.get(scope_id)
    if direct is not None:
        return direct
    for row in rows.values():
        if getattr(row, "scope_id", None) == scope_id:
            return row
    return None


def _inside_pessimistic_share(state: State, wave_ids: set[str]) -> float | None:
    """Return calibrated inside-pessimistic share for the given wave ids."""
    estimates = state.estimates or {}
    actuals = state.actuals or {}
    sample_count = 0
    inside = 0
    for wave_id in sorted(wave_ids, key=natural_key):
        est = _lookup_by_key_or_scope(estimates, wave_id)
        act = _lookup_by_key_or_scope(actuals, wave_id)
        if est is None or act is None:
            continue
        sample_count += 1
        if act.elapsed_eu <= est.pessimistic_eu:
            inside += 1
    if sample_count == 0:
        return None
    return inside / sample_count


def _positive_int_from_config(
    config: Mapping[str, Any] | None,
    *,
    section: str,
    key: str,
    default: int,
) -> int:
    """Read a positive integer leaf from nested config or return *default*."""
    if isinstance(config, Mapping):
        section_value = config.get(section)
        if isinstance(section_value, Mapping):
            raw = section_value.get(key)
            if isinstance(raw, int) and raw >= 1:
                return raw
    return default


def _positive_float_from_config(
    config: Mapping[str, Any] | None,
    *,
    section: str,
    key: str,
    default: float,
) -> float:
    """Read a positive float leaf from nested config or return *default*."""
    if isinstance(config, Mapping):
        section_value = config.get(section)
        if isinstance(section_value, Mapping):
            raw = section_value.get(key)
            if isinstance(raw, int | float) and raw > 0:
                return float(raw)
    return default


def _phase_eu_rollup(
    state: State,
    phase_id: str,
    *,
    config: Mapping[str, Any] | None,
) -> RealisticWallClockMetric:
    """Compute the phase EU rollup used by roadmap markdown."""
    waves = _phase_waves(state, phase_id)
    wave_ids = {wave.id for wave in waves}
    return compute_realistic_wall_clock(
        waves,
        max_parallel_waves=_positive_int_from_config(
            config,
            section="planning",
            key="max_parallel_waves",
            default=4,
        ),
        inside_pessimistic_share=_inside_pessimistic_share(state, wave_ids),
        eu_minutes=_positive_float_from_config(
            config,
            section="estimation",
            key="eu_minutes",
            default=30.0,
        ),
    )


def _metric_eu(rollup: RealisticWallClockMetric, field: EuRollupField) -> float:
    """Return the EU value for one rollup field."""
    if field == "work_sum":
        return rollup.work_sum_eu
    if field == "critical_path":
        return rollup.critical_path_eu
    if field == "queue":
        return rollup.queue_wall_clock_eu
    return rollup.realistic_wall_clock_eu


def _metric_detail(rollup: RealisticWallClockMetric, field: EuRollupField) -> str:
    """Return explanatory detail for one full-density rollup row."""
    if field == "work_sum":
        return "serial wave work"
    if field == "critical_path":
        return "longest dependency path"
    if field == "queue":
        return f"DAG queue at {rollup.max_parallel_waves} workers"
    share = (
        "n/a"
        if rollup.inside_pessimistic_share is None
        else f"{rollup.inside_pessimistic_share:.0%}"
    )
    return f"queue x {rollup.pessimism_multiplier:g}; inside_pess={share}"


def _format_hours(eu: float, eu_minutes: float) -> str:
    """Render an EU value as hours via the configured EU-minute factor."""
    return f"{(eu * eu_minutes / 60.0):g}"


def _render_eu_rollup_markdown(
    state: State,
    rows: list[RoadmapRow],
    *,
    config: Mapping[str, Any] | None,
) -> list[str]:
    """Render phase-level EU/hour rows for roadmap markdown."""
    eu_view = _eu_view_config(config)
    lines: list[str] = ["", "## EU/hour rollup", ""]
    if eu_view.density == "compact":
        lines.append("| Phase | Metric | EU | Hours |")
        lines.append("|---|---|---:|---:|")
    else:
        lines.append("| Phase | Metric | EU | Hours | Detail |")
        lines.append("|---|---|---:|---:|---|")
    label_by_field: dict[EuRollupField, str] = {
        "work_sum": "work-sum",
        "critical_path": "critical-path",
        "queue": "queue",
        "realistic": "realistic",
    }
    for row in rows:
        rollup = _phase_eu_rollup(state, row.id, config=config)
        for field in eu_view.fields:
            eu = _metric_eu(rollup, field)
            hours = _format_hours(eu, rollup.eu_minutes)
            if eu_view.density == "compact":
                lines.append(f"| `{row.id}` | {label_by_field[field]} | {eu:g} | {hours} |")
            else:
                detail = _metric_detail(rollup, field)
                lines.append(
                    f"| `{row.id}` | {label_by_field[field]} | {eu:g} | {hours} | {detail} |"
                )
    return lines


#: Band header for phases that carry no ``release`` version.
_UNRELEASED_BAND = "Unreleased"

_ROADMAP_TABLE_HEADER: tuple[str, str] = (
    "| Phase | Status | Waves | Depends on | Title |",
    "|---|---|---|---|---|",
)


#: Rank of each PEP-440 pre-release marker, lowest first. A final release
#: (no marker) outranks every pre-release of the same semver core, so under
#: newest-first ordering ``v0.5.0`` sorts above ``v0.5.0rc1``.
_PRERELEASE_RANK: dict[str, int] = {"a": 0, "b": 1, "rc": 2, "": 3}


def _release_sort_key(release: str) -> tuple[int, int, int, int, int, str]:
    """Return a totally-ordered sort key for a ``vMAJOR.MINOR.PATCH`` label.

    The key is ``(major, minor, patch, prerelease_rank, prerelease_num,
    raw)``. The semver core orders first; a final release (rank 3) outranks
    its pre-releases (``a`` < ``b`` < ``rc``) of the same core so newest-first
    ordering places ``v0.5.0`` above ``v0.5.0rc1``; the raw string is the
    final tiebreaker so the order is deterministic. A non-conforming label
    (which the model pattern rejects on the write path, but a hand-edited
    state could still carry) yields ``-1`` cores so it sorts last under
    newest-first and stays visible.
    """
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", release)
    if match is None:
        return (-1, -1, -1, -1, -1, release)
    major, minor, patch = int(match[1]), int(match[2]), int(match[3])
    marker = match[4] or ""
    pre_num = int(match[5]) if match[5] is not None else 0
    return (major, minor, patch, _PRERELEASE_RANK[marker], pre_num, release)


def _band_release_labels(rows: list[RoadmapRow]) -> list[str]:
    """Return the ordered band labels: release versions then ``Unreleased``.

    Release versions sort newest-first by semver core; the ``Unreleased``
    band (phases with ``release is None``) is always last so in-flight,
    un-banded work reads at the bottom. The ``Unreleased`` band is only
    included when at least one row lacks a release.
    """
    releases = {row.release for row in rows if row.release is not None}
    ordered = sorted(releases, key=_release_sort_key, reverse=True)
    if any(row.release is None for row in rows):
        ordered.append(_UNRELEASED_BAND)
    return ordered


def _render_phase_table_body(rows: list[RoadmapRow]) -> list[str]:
    """Render the per-phase markdown body rows (no header) for *rows*."""
    body: list[str] = []
    for row in rows:
        deps = ", ".join(row.depends_on) or "—"
        body.append(f"| `{row.id}` | `{row.status}` | {row.wave_count} | {deps} | {row.title} |")
    return body


def _render_banded_phase_tables(rows: list[RoadmapRow]) -> list[str]:
    """Render release-banded phase tables, one ``### <band>`` block per version.

    Each band carries an H3 header (the release version, or ``### Unreleased``
    for phases without one) above the existing phase table. Used only when at
    least one phase carries a ``release``; the no-release case renders a single
    unbanded table so legacy output stays byte-stable.
    """
    out: list[str] = []
    for label in _band_release_labels(rows):
        if label == _UNRELEASED_BAND:
            band_rows = [row for row in rows if row.release is None]
        else:
            band_rows = [row for row in rows if row.release == label]
        out.append(f"### {label}")
        out.append("")
        out.append(_ROADMAP_TABLE_HEADER[0])
        out.append(_ROADMAP_TABLE_HEADER[1])
        out.extend(_render_phase_table_body(band_rows))
        out.append("")
    # Drop the trailing blank so the EU-rollup block joins cleanly.
    if out and out[-1] == "":
        out.pop()
    return out


def render_roadmap_markdown(
    state: State,
    *,
    phase_id_filter: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Render the roadmap-show markdown table from *state*, banded by release.

    Canonical markdown surface for ``eawf roadmap show --md`` after the
    P28-W18 unification: the CLI's ``_render_show_md`` thin-wraps this
    helper so the renderer lives in ``plan_view`` alongside per-iter
    :func:`render_markdown`. Output is byte-stable: empty-state literal
    when *state* has no phases (or none match the filter), otherwise a
    pipe-delimited table with one row per phase.

    When at least one phase carries a :attr:`~eawf.kernel.state.models.Phase.release`
    version the table is split into ``### <version>`` bands (newest first)
    with an ``### Unreleased`` band trailing for phases without one. When no
    phase carries a release the output is a single unbanded table, identical
    to the pre-banding layout.

    Args:
        state: The validated state document.
        phase_id_filter: Restrict the rendered queue to one phase, or
            ``None`` for the full queue.
        config: Optional merged layered config. ``tui.eu_view.density`` and
            ``tui.eu_view.fields`` control the EU/hour rollup table.

    Returns:
        A markdown string — either the empty-state literal or the
        rendered table.
    """
    rows = build_roadmap_rows(state, phase_id_filter=phase_id_filter)
    if not rows:
        return "_(no phases in state)_"
    if any(row.release is not None for row in rows):
        out = _render_banded_phase_tables(rows)
    else:
        out = [_ROADMAP_TABLE_HEADER[0], _ROADMAP_TABLE_HEADER[1]]
        out.extend(_render_phase_table_body(rows))
    out.extend(_render_eu_rollup_markdown(state, rows, config=config))
    return "\n".join(out)


def render_phase_markdown(state: State, phase_id: str) -> str:
    """Render *phase_id*'s plan as the per-iter ``plan_view`` markdown.

    The body the Claude-runtime ``EnterPlanMode`` (and Codex
    text-prompt) surfaces for ``/prep`` plan-mode: a phase header
    followed by :func:`render_markdown` over each iter under the phase
    so the operator reads the full DAG + wave table per iter from the
    same renderer that ``eawf plan show`` uses. A phase missing from
    *state* renders an empty-state line.

    Args:
        state: The validated state document.
        phase_id: The target phase id (resolved against
            ``state.phases``).

    Returns:
        A markdown string — either the empty-state literal or the
        header + per-iter plan-view body.
    """
    phase = state.phases.get(phase_id) if state.phases else None
    if phase is None:
        return f"_(phase {phase_id!r} not in state)_"
    out: list[str] = [
        f"# Roadmap: {phase.id} — {phase.title}",
        "",
        f"> Status: {phase.status.value}",
        "",
    ]
    rendered_any = False
    for iter_id in phase.iter_ids:
        if iter_id not in state.iters:
            continue
        try:
            view = build_view(state, iter_id)
        except PlanViewNotFound:
            continue
        out.append(render_markdown(view))
        out.append("")
        rendered_any = True
    if not rendered_any:
        out.append("_(no iters under phase)_")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_markdown(
    view: PlanView,
    *,
    ascii_dag: bool = False,
    sections: PlanSection = PlanSection.ALL,
) -> str:
    """Render *view* as a deterministic markdown body.

    Always-on header (H1 + status line). Body sections appear in fixed
    order — Summary, DAG, Waves, Checks, Risks — so golden fixtures stay
    diff-stable. ``sections`` restricts the body to a single section + the
    always-on header. Trailing newline is included.

    For an empty iter (``waves == []``) renders the header + a one-line
    "No waves planned yet." paragraph in place of the body sections.
    """
    iter_view = view.iter
    phase_view = view.phase

    out: list[str] = []
    out.append(f"# Plan: {iter_view.id} — {iter_view.title}")
    out.append("")
    phase_part = (
        f"Phase: {phase_view.id} ({phase_view.title})" if phase_view is not None else "Phase: -"
    )
    out.append(f"> Status: {iter_view.status} · {phase_part} · Opened: {iter_view.opened_at}")
    out.append("")

    if not view.waves and sections == PlanSection.ALL:
        out.append("No waves planned yet.")
        out.append("")
        return "\n".join(out)

    body_blocks: list[list[str]] = []
    if sections == PlanSection.ALL:
        body_blocks.append(_format_summary(view))
        if ascii_dag:
            body_blocks.append(_format_dag_ascii(view))
        else:
            body_blocks.append(_format_dag_mermaid(view))
        body_blocks.append(_format_waves(view))
        body_blocks.append(_format_checks(view))
        body_blocks.append(_format_risks(view))
    elif sections == PlanSection.DAG:
        body_blocks.append(_format_dag_ascii(view) if ascii_dag else _format_dag_mermaid(view))
    elif sections == PlanSection.WAVES:
        body_blocks.append(_format_waves(view))
    elif sections == PlanSection.CHECKS:
        body_blocks.append(_format_checks(view))
    elif sections == PlanSection.RISKS:
        body_blocks.append(_format_risks(view))

    for i, block in enumerate(body_blocks):
        out.extend(block)
        if i < len(body_blocks) - 1:
            out.append("")
    out.append("")
    return "\n".join(out)
