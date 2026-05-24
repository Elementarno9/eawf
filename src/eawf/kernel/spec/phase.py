"""PhaseSpec — typed charter for a phase.

A PhaseSpec is the durable intent document for a phase: outcome
statement, KPI targets, success/failure modes, ship-criteria gates,
effort envelope, iter membership, and cross-cites to the verdicts the
phase implements. The companion runtime state row
(:class:`eawf.kernel.state.models.Phase`) tracks reality (status, claim/close
times, agent attempts); the spec describes intent.

The spec validators (PSV-01..PSV-06) live in the loader and the
``eawf phase spec validate`` CLI; this module ships only the schema
shape + structural invariants enforceable inside Pydantic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.kernel.spec.common import FileScopeRef, VerdictCitation, _StrictModel
from eawf.kernel.state.models import IterIdStr, PhaseIdStr


class PhaseKPI(_StrictModel):
    """Quantitative KPI for the phase.

    ``direction`` picks whether the metric should be minimised,
    maximised, or held to a fixed value; ``threshold_kind`` separates
    hard gates (block phase close on miss) from soft signals
    (advisory).
    """

    metric: str
    target: float
    direction: Literal["min", "max", "equal"]
    threshold_kind: Literal["hard", "soft"]
    note: str | None = None


class PhaseShipCriterion(_StrictModel):
    """One ship-gate criterion for closing the phase.

    ``audit_kind`` (optional) ties the criterion to a registered
    audit-DSL kind so a ship audit can mechanically verify it; without
    one the criterion is a prose-only gate the operator confirms.
    """

    id: str
    text: str
    audit_kind: str | None = None


class PhaseEUEnvelope(_StrictModel):
    """Effort-unit envelope for the phase.

    EU calibration: 1 EU ≈ 25-30 min of agent-driven focused work.
    ``pessimistic_eu_total`` (optional) records the upper bound when
    the planner has high uncertainty.
    """

    expected_eu_total: float = Field(ge=0)
    pessimistic_eu_total: float | None = Field(default=None, ge=0)
    confidence: Literal["low", "medium", "high"] = "medium"


class PhaseSpec(_StrictModel):
    """Phase charter spec.

    Required non-empty fields (Pydantic-enforced):

    - ``failure_modes`` — forces the planner to enumerate negative
      space before activation.
    - ``ship_criteria`` — no PhaseSpec graduates to READY without at
      least one ship gate.

    Additional invariants (PSV-01..PSV-06) live in the loader and CLI
    validators because they need cross-state lookups.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["PhaseSpec"] = "PhaseSpec"

    id: PhaseIdStr
    title: str = Field(min_length=1, max_length=120)
    outcome: str = Field(min_length=20, max_length=1500)
    kpis: list[PhaseKPI] = Field(default_factory=list)
    success_modes: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(min_length=1)
    depends_on: list[PhaseIdStr] = Field(default_factory=list)
    eu_envelope: PhaseEUEnvelope | None = None
    ship_criteria: list[PhaseShipCriterion] = Field(min_length=1)
    iter_ids: list[IterIdStr] = Field(default_factory=list)
    profile_constraints: list[str] = Field(default_factory=list)
    implements: list[VerdictCitation] = Field(default_factory=list)
    consumed_by: list[PhaseIdStr] = Field(default_factory=list)
    related_file_scopes: list[FileScopeRef] = Field(default_factory=list)
