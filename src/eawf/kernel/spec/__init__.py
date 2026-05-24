"""Typed spec models for phases, iters, waves, and audits.

This package owns the durable intent documents for the eawf scope
hierarchy. Each spec is a Pydantic v2 :class:`pydantic.BaseModel` with
``ConfigDict(extra="forbid")`` so YAML / JSON ingestion fails fast on
unknown keys (AGENTS rule 2).

Public surface:

- :class:`~eawf.kernel.spec.phase.PhaseSpec` — phase charter
- :class:`~eawf.kernel.spec.iter.IterSpec` — iter intent
- :class:`~eawf.kernel.spec.wave.WaveSpec` — wave deliverable
- :class:`~eawf.kernel.spec.audit.AuditSpec` — declarative audit doc
  consumed by the audit-DSL runner
- :class:`~eawf.kernel.spec.common.VerdictCitation` — citation tying a spec
  to the verdict (V/D/R/H) it implements
- :class:`~eawf.kernel.spec.common.EvidenceRef` — one row of a hypothesis
  evidence chain
- :data:`~eawf.kernel.spec.common.TestRef`,
  :data:`~eawf.kernel.spec.common.FileScopeRef` — annotated path types

Loader, validator, and CLI surfaces consume these models; they are
not built or populated here.
"""

from __future__ import annotations

from eawf.kernel.spec.audit import AUDIT_CADENCE_VALUES, AuditCadence, AuditSpec
from eawf.kernel.spec.common import (
    BriefPathStr,
    EvidenceRef,
    FileScopeRef,
    TestRef,
    VerdictCitation,
    VerdictIdStr,
)
from eawf.kernel.spec.heuristics import (
    UI_SCOPE_PREFIXES,
    is_ui_scope,
    missing_test_paths,
    requires_mockup_reference,
)
from eawf.kernel.spec.iter import IterAuditCadence, IterSpec, IterWaveGroup
from eawf.kernel.spec.phase import (
    PhaseEUEnvelope,
    PhaseKPI,
    PhaseShipCriterion,
    PhaseSpec,
)
from eawf.kernel.spec.validators import (
    SpecValidationError,
    validate_phase_spec_at_load,
    validate_phase_spec_has_kpis,
    validate_wave_spec_at_load,
    validate_wave_spec_brief_paths_exist,
    validate_wave_spec_tests_exist,
)
from eawf.kernel.spec.wave import WaveBehavior, WaveMockup, WaveSpec

__all__ = [
    "AUDIT_CADENCE_VALUES",
    "UI_SCOPE_PREFIXES",
    "AuditCadence",
    "AuditSpec",
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
    "SpecValidationError",
    "TestRef",
    "VerdictCitation",
    "VerdictIdStr",
    "WaveBehavior",
    "WaveMockup",
    "WaveSpec",
    "is_ui_scope",
    "missing_test_paths",
    "requires_mockup_reference",
    "validate_phase_spec_at_load",
    "validate_phase_spec_has_kpis",
    "validate_wave_spec_at_load",
    "validate_wave_spec_brief_paths_exist",
    "validate_wave_spec_tests_exist",
]
