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
- :class:`~eawf.kernel.spec.research.ResearchDepth` — canonical
  ``/research`` survey-depth ladder
- :class:`~eawf.kernel.spec.audit.AuditSpec` — declarative audit doc
  consumed by the audit-DSL runner
- :class:`~eawf.kernel.spec.operator_input.OperatorInput` — one append-only
  mid-run operator input; :class:`~eawf.kernel.spec.operator_input.OperatorInputChannel`
  folds the log (D-2 blocking-only pause / D-3 override persists-locked)
- :class:`~eawf.kernel.spec.operator_input.CampaignProgressState` — pure
  projection of a campaign's round + per-domain progress
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
    "BLOCKING_URGENCY": ("operator_input", "BLOCKING_URGENCY"),
    "DEFAULT_CAMPAIGN_ROLE": ("research_campaign", "DEFAULT_CAMPAIGN_ROLE"),
    "DEFAULT_CHECKPOINT_INTERVAL": ("round_loop", "DEFAULT_CHECKPOINT_INTERVAL"),
    "DEFAULT_GATE_ARGV_ALLOWLIST": ("promotion", "DEFAULT_GATE_ARGV_ALLOWLIST"),
    "DEFAULT_NOVELTY_FLOOR": ("saturation", "DEFAULT_NOVELTY_FLOOR"),
    "DEFAULT_NOVELTY_WINDOW": ("saturation", "DEFAULT_NOVELTY_WINDOW"),
    "DEFAULT_RESEARCH_DEPTH": ("research", "DEFAULT_RESEARCH_DEPTH"),
    "DEFAULT_RETENTION_WINDOW": ("pruning", "DEFAULT_RETENTION_WINDOW"),
    "DEFAULT_ROUND_BUDGET": ("round_loop", "DEFAULT_ROUND_BUDGET"),
    "DEFAULT_TOP_K": ("pruning", "DEFAULT_TOP_K"),
    "MAX_STAGED_DISPATCHES": ("research_campaign", "MAX_STAGED_DISPATCHES"),
    "RESEARCH_DEPTH_VALUES": ("research", "RESEARCH_DEPTH_VALUES"),
    "UI_SCOPE_PREFIXES": ("heuristics", "UI_SCOPE_PREFIXES"),
    "AddQuestionPayload": ("operator_input", "AddQuestionPayload"),
    "AuditCadence": ("audit", "AuditCadence"),
    "AuditSpec": ("audit", "AuditSpec"),
    "BriefPathStr": ("common", "BriefPathStr"),
    "CampaignProgressKind": ("operator_input", "CampaignProgressKind"),
    "CampaignProgressState": ("operator_input", "CampaignProgressState"),
    "ChannelFold": ("operator_input", "ChannelFold"),
    "Checkpoint": ("round_loop", "Checkpoint"),
    "CheckpointPolicy": ("round_loop", "CheckpointPolicy"),
    "CheckpointTier": ("round_loop", "CheckpointTier"),
    "CockpitLevel": ("live_rounds", "CockpitLevel"),
    "CriterionAcceptanceStyle": ("common", "CriterionAcceptanceStyle"),
    "CriterionEvidenceKind": ("common", "CriterionEvidenceKind"),
    "CriterionSpec": ("common", "CriterionSpec"),
    "DomainProgress": ("operator_input", "DomainProgress"),
    "DomainProgressStatus": ("operator_input", "DomainProgressStatus"),
    "DropReason": ("pruning", "DropReason"),
    "DroppedClaim": ("pruning", "DroppedClaim"),
    "EffectiveOverride": ("operator_input", "EffectiveOverride"),
    "EvidenceRef": ("common", "EvidenceRef"),
    "FileScopeRef": ("common", "FileScopeRef"),
    "GateCadence": ("common", "GateCadence"),
    "GatePolicy": ("common", "GatePolicy"),
    "GateSpec": ("common", "GateSpec"),
    "IntentBrief": ("intent", "IntentBrief"),
    "IterAuditCadence": ("iter", "IterAuditCadence"),
    "IterSpec": ("iter", "IterSpec"),
    "IterWaveGroup": ("iter", "IterWaveGroup"),
    "OperatorInput": ("operator_input", "OperatorInput"),
    "OperatorInputChannel": ("operator_input", "OperatorInputChannel"),
    "OperatorInputKind": ("operator_input", "OperatorInputKind"),
    "OverridePayload": ("operator_input", "OverridePayload"),
    "PhaseEUEnvelope": ("phase", "PhaseEUEnvelope"),
    "PhaseKPI": ("phase", "PhaseKPI"),
    "PhaseShipCriterion": ("phase", "PhaseShipCriterion"),
    "PhaseSpec": ("phase", "PhaseSpec"),
    "PruneConfig": ("pruning", "PruneConfig"),
    "PruneLevel": ("pruning", "PruneLevel"),
    "PruningResult": ("pruning", "PruningResult"),
    "ResearchDepth": ("research", "ResearchDepth"),
    "ResearchDomainConfig": ("research_campaign", "ResearchDomainConfig"),
    "ResearchProfileBlock": ("research_campaign", "ResearchProfileBlock"),
    "RoundHaltReason": ("round_loop", "RoundHaltReason"),
    "RoundLoopResult": ("round_loop", "RoundLoopResult"),
    "RoundOutcome": ("round_loop", "RoundOutcome"),
    "SaturationGateResult": ("saturation", "SaturationGateResult"),
    "SaturationReport": ("saturation", "SaturationReport"),
    "SpecPromoteValidationError": ("promotion", "SpecPromoteValidationError"),
    "SpecValidationError": ("validators", "SpecValidationError"),
    "StagedCampaign": ("research_campaign", "StagedCampaign"),
    "StagedDispatch": ("research_campaign", "StagedDispatch"),
    "SteerAction": ("operator_input", "SteerAction"),
    "SteerPayload": ("operator_input", "SteerPayload"),
    "SubLiveLevelError": ("live_rounds", "SubLiveLevelError"),
    "SupervisedCadenceError": ("live_rounds", "SupervisedCadenceError"),
    "TestRef": ("common", "TestRef"),
    "VerdictCitation": ("common", "VerdictCitation"),
    "VerdictIdStr": ("common", "VerdictIdStr"),
    "WaveBehavior": ("wave", "WaveBehavior"),
    "WaveMockup": ("wave", "WaveMockup"),
    "WaveSpec": ("wave", "WaveSpec"),
    "coerce_research_depth": ("research", "coerce_research_depth"),
    "is_ui_scope": ("heuristics", "is_ui_scope"),
    "missing_test_paths": ("heuristics", "missing_test_paths"),
    "prune": ("pruning", "prune"),
    "requires_mockup_reference": ("heuristics", "requires_mockup_reference"),
    "research_depth_emits_fanout": ("research", "research_depth_emits_fanout"),
    "research_depth_question_slots": ("research", "research_depth_question_slots"),
    "run_live_rounds": ("live_rounds", "run_live_rounds"),
    "run_round_loop": ("round_loop", "run_round_loop"),
    "stage_campaign": ("research_campaign", "stage_campaign"),
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
