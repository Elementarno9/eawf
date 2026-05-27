"""Typed spec models for phases, iters, waves, and audits.

This package owns the durable intent documents for the eawf scope
hierarchy. Each spec is a Pydantic v2 :class:`pydantic.BaseModel` with
``ConfigDict(extra="forbid")`` so YAML / JSON ingestion fails fast on
unknown keys (AGENTS rule 2).

Public surface:

- :class:`~eawf.kernel.spec.phase.PhaseSpec` — phase charter
- :class:`~eawf.kernel.spec.iter.IterSpec` — iter intent
- :class:`~eawf.kernel.spec.wave.WaveSpec` — wave deliverable
- :class:`~eawf.kernel.spec.intent.IntentBrief` — typed intent doc
  carried by lifecycle entities
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

The public symbols are resolved lazily through :func:`__getattr__` so
:mod:`eawf.kernel.state.models` can import :class:`IntentBrief` from
:mod:`eawf.kernel.spec.intent` without triggering the eager spec-module
import chain (``audit`` → ``common`` → ``state.models.IdStr``) — the
circular import that chain would otherwise create is sidestepped at the
package init boundary. The lazy table mirrors the public ``__all__``
list, so ``from eawf.kernel.spec import X`` still works for every
documented re-export.
"""

from __future__ import annotations

from typing import Any

#: Map of public symbol → (submodule, attribute-name). Populated once;
#: :func:`__getattr__` resolves the submodule on first attribute access
#: and caches the resolved object on the package itself so subsequent
#: lookups bypass the dispatch.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ARGV_BEARING_GATE_KINDS": ("promotion", "ARGV_BEARING_GATE_KINDS"),
    "AUDIT_CADENCE_VALUES": ("audit", "AUDIT_CADENCE_VALUES"),
    "DEFAULT_GATE_ARGV_ALLOWLIST": ("promotion", "DEFAULT_GATE_ARGV_ALLOWLIST"),
    "UI_SCOPE_PREFIXES": ("heuristics", "UI_SCOPE_PREFIXES"),
    "AuditCadence": ("audit", "AuditCadence"),
    "AuditSpec": ("audit", "AuditSpec"),
    "BriefPathStr": ("common", "BriefPathStr"),
    "CriterionAcceptanceStyle": ("common", "CriterionAcceptanceStyle"),
    "CriterionEvidenceKind": ("common", "CriterionEvidenceKind"),
    "CriterionSpec": ("common", "CriterionSpec"),
    "EvidenceRef": ("common", "EvidenceRef"),
    "FileScopeRef": ("common", "FileScopeRef"),
    "GateCadence": ("common", "GateCadence"),
    "GatePolicy": ("common", "GatePolicy"),
    "GateSpec": ("common", "GateSpec"),
    "IntentBrief": ("intent", "IntentBrief"),
    "IterAuditCadence": ("iter", "IterAuditCadence"),
    "IterSpec": ("iter", "IterSpec"),
    "IterWaveGroup": ("iter", "IterWaveGroup"),
    "PhaseEUEnvelope": ("phase", "PhaseEUEnvelope"),
    "PhaseKPI": ("phase", "PhaseKPI"),
    "PhaseShipCriterion": ("phase", "PhaseShipCriterion"),
    "PhaseSpec": ("phase", "PhaseSpec"),
    "SpecPromoteValidationError": ("promotion", "SpecPromoteValidationError"),
    "SpecValidationError": ("validators", "SpecValidationError"),
    "TestRef": ("common", "TestRef"),
    "VerdictCitation": ("common", "VerdictCitation"),
    "VerdictIdStr": ("common", "VerdictIdStr"),
    "WaveBehavior": ("wave", "WaveBehavior"),
    "WaveMockup": ("wave", "WaveMockup"),
    "WaveSpec": ("wave", "WaveSpec"),
    "is_ui_scope": ("heuristics", "is_ui_scope"),
    "missing_test_paths": ("heuristics", "missing_test_paths"),
    "requires_mockup_reference": ("heuristics", "requires_mockup_reference"),
    "validate_argv_gates": ("promotion", "validate_argv_gates"),
    "validate_phase_spec_at_load": ("validators", "validate_phase_spec_at_load"),
    "validate_phase_spec_has_kpis": ("validators", "validate_phase_spec_has_kpis"),
    "validate_wave_spec_at_load": ("validators", "validate_wave_spec_at_load"),
    "validate_wave_spec_brief_paths_exist": (
        "validators",
        "validate_wave_spec_brief_paths_exist",
    ),
    "validate_wave_spec_tests_exist": ("validators", "validate_wave_spec_tests_exist"),
}


def __getattr__(name: str) -> Any:
    """Resolve a public spec symbol on first access (PEP 562).

    Looks the symbol up in :data:`_LAZY_EXPORTS`, imports the named
    submodule via :func:`importlib.import_module`, fetches the
    attribute, caches it on the package globals for subsequent
    lookups, and returns it. An unknown name raises
    :class:`AttributeError` so ``from eawf.kernel.spec import bogus``
    fails fast.

    Args:
        name: The attribute name being resolved.

    Returns:
        The lazily resolved attribute.

    Raises:
        AttributeError: when *name* is not in :data:`_LAZY_EXPORTS`.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'eawf.kernel.spec' has no attribute {name!r}")
    submodule, attr = target
    import importlib

    module = importlib.import_module(f"eawf.kernel.spec.{submodule}")
    value = getattr(module, attr)
    globals()[name] = value
    return value


__all__ = sorted(_LAZY_EXPORTS)
