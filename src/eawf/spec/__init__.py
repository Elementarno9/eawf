"""Typed spec models for phases, iters, and waves.

This package owns the durable intent documents for the eawf scope
hierarchy. Each spec is a Pydantic v2 :class:`pydantic.BaseModel` with
``ConfigDict(extra="forbid")`` so YAML / JSON ingestion fails fast on
unknown keys (AGENTS rule 2).

Public surface:

- :class:`~eawf.spec.phase.PhaseSpec` — phase charter
- :class:`~eawf.spec.iter.IterSpec` — iter intent
- :class:`~eawf.spec.wave.WaveSpec` — wave deliverable
- :class:`~eawf.spec.common.VerdictCitation` — citation tying a spec
  to the verdict (V/D/R/H) it implements
- :class:`~eawf.spec.common.EvidenceRef` — one row of a hypothesis
  evidence chain
- :data:`~eawf.spec.common.TestRef`,
  :data:`~eawf.spec.common.FileScopeRef` — annotated path types

Loader, validator, and CLI surfaces consume these models; they are
not built or populated here.
"""

from __future__ import annotations

from eawf.spec.common import (
    BriefPathStr,
    EvidenceRef,
    FileScopeRef,
    TestRef,
    VerdictCitation,
    VerdictIdStr,
)
from eawf.spec.iter import IterAuditCadence, IterSpec, IterWaveGroup
from eawf.spec.phase import (
    PhaseEUEnvelope,
    PhaseKPI,
    PhaseShipCriterion,
    PhaseSpec,
)
from eawf.spec.wave import WaveBehavior, WaveMockup, WaveSpec

__all__ = [
    "BriefPathStr",
    "EvidenceRef",
    "FileScopeRef",
    "IterAuditCadence",
    "IterSpec",
    "IterWaveGroup",
    "PhaseEUEnvelope",
    "PhaseKPI",
    "PhaseShipCriterion",
    "PhaseSpec",
    "TestRef",
    "VerdictCitation",
    "VerdictIdStr",
    "WaveBehavior",
    "WaveMockup",
    "WaveSpec",
]
