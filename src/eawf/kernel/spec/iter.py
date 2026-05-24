"""IterSpec — typed intent document for an iter.

An iter is the unit of delivery sitting between a phase and its waves.
The IterSpec records the iter's sub-goal, the rationale for the
ordering of waves within it, optional wave groupings (narrative
labels over disjoint subsets of waves), and the audit-DSL cadence
that fires on iter close or phase close. The runtime row
(:class:`eawf.kernel.state.models.Iter`) tracks status; the spec describes
intent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.kernel.spec.common import VerdictCitation, _StrictModel
from eawf.kernel.state.models import IterIdStr, PhaseIdStr, WaveIdStr


class IterAuditCadence(_StrictModel):
    """When audit-DSL kinds fire for this iter.

    Each list holds registered ``audit_kind`` names from
    :mod:`eawf.audit_dsl.registry`; the audit runner consults the spec
    at iter-close and phase-close events and dispatches the named
    kinds.
    """

    on_iter_close: list[str] = Field(default_factory=list)
    on_phase_close: list[str] = Field(default_factory=list)


class IterWaveGroup(_StrictModel):
    """One grouping of waves under a shared narrative label.

    ``wave_ids`` is non-empty (a group with no waves is a typo, not a
    grouping); ``rationale`` is prose explaining why those waves form
    a unit.
    """

    label: str
    wave_ids: list[WaveIdStr] = Field(min_length=1)
    rationale: str = Field(min_length=20, max_length=600)


class IterSpec(_StrictModel):
    """Iter intent spec.

    Length floors on ``sub_goal`` and ``ordering_rationale`` are
    deliberate: titles-only iters with one-line "because" rationales
    are rejected at load time so the operator is forced to articulate
    why the waves are ordered the way they are.

    Cross-state invariants (ISV-04: ``wave_ids`` matches
    ``state.iters[id].wave_ids``; ISV-06: ``phase_id`` is the prefix
    of ``id``) live in the loader/CLI validators because they need
    state-tree lookups.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["IterSpec"] = "IterSpec"

    id: IterIdStr
    phase_id: PhaseIdStr
    title: str = Field(min_length=1, max_length=120)
    sub_goal: str = Field(min_length=20, max_length=800)
    ordering_rationale: str = Field(min_length=20, max_length=1000)
    wave_groups: list[IterWaveGroup] = Field(default_factory=list)
    audit_cadence: IterAuditCadence = Field(default_factory=IterAuditCadence)
    profile_constraints: list[str] = Field(default_factory=list)
    implements: list[VerdictCitation] = Field(default_factory=list)
    wave_ids: list[WaveIdStr] = Field(default_factory=list)
