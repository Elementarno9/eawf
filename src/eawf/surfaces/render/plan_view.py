"""Typed models of the iter plan view.

:class:`PlanView` and its parts are the closed shape that
``src/eawf/schemas/plan-view.schema.json`` is generated from, so the
committed schema keeps a typed source.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
