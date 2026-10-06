"""Typed models of the iter plan view and the ``tui.eu_view`` config.

:class:`PlanView` and its parts are the closed shape that
``src/eawf/schemas/plan-view.schema.json`` is generated from, so the
committed schema keeps a typed source. :func:`_eu_view_config` reads the
``tui.eu_view`` leaves of the layered config.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


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
