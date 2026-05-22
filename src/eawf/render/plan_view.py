"""Read-only ``plan_view`` renderer for ``eawf plan show``.

Pure projection over a validated :class:`~eawf.state.models.State`. Builds a
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
from collections import deque
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.estimation.buckets import (
    critical_path_eu,
    sum_wave_eu,
    timestamp_actual_eu,
    wave_estimate_eu,
)
from eawf.lifecycle.wave_sha import derive_wave_sha
from eawf.state.enums import (
    BacklogPriority,
    BacklogStatus,
    HypothesisVerdict,
    IncidentSeverity,
    IncidentStatus,
    WaveStatus,
)
from eawf.state.models import (
    Audit,
    BacklogItem,
    Hypothesis,
    Incident,
    Iter,
    Phase,
    State,
    Wave,
)

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
    """Compact projection of :class:`~eawf.state.models.Iter`."""

    id: str
    phase_id: str
    title: str
    status: str
    opened_at: str
    closed_at: str | None = None
    audit_id: str | None = None
    estimate_id: str | None = None


class PhaseView(_StrictModel):
    """Compact projection of :class:`~eawf.state.models.Phase`."""

    id: str
    title: str
    status: str


class WaveView(_StrictModel):
    """Compact projection of :class:`~eawf.state.models.Wave`."""

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
        actual_elapsed_eu=timestamp_actual_eu(waves),
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
