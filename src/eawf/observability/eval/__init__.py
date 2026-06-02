"""Skill-eval semantic-scoring layer (B042, P13-W03).

Public surface:

- :class:`EvalScore` — Pydantic v2 result model carrying the weighted
  total alongside a per-dimension breakdown.
- :func:`score_envelope` — entry-point used by the eval regression
  suite to score a live envelope against its golden fixture.

The shape-only guard from P12-W05 (``test_skill_envelope_matches_golden``)
still runs; the score test sits beside it as a stricter regression
check parameterised over the same six skill cases.
"""

from __future__ import annotations

from eawf.observability.eval.cross_vendor_jury import (
    JURY_QUORUM,
    JURY_RUNTIME_FAMILIES,
    CrossVendorJuryResult,
    JurorOutcome,
    SpawnFactory,
    convene_cross_vendor_jury,
)
from eawf.observability.eval.jury import (
    CONSENSUS_SPREAD,
    FAIL_SCORE_THRESHOLD,
    PASS_SCORE_THRESHOLD,
    JurorBallot,
    JuryAggregate,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.observability.eval.models import EvalScore
from eawf.observability.eval.reputation import (
    ReliabilityStatus,
    ReputationConfig,
    RoleReliability,
    VerdictOutcome,
    build_verdict_outcomes,
    compute_role_reliability,
    confidence_to_float,
)
from eawf.observability.eval.rule_adherence import (
    RULE_IDS,
    RecordedWaveCase,
    RuleAdherenceBaseline,
    RuleAdherenceReport,
    load_recorded_wave_cases,
)
from eawf.observability.eval.score import (
    score_envelope,
    score_recorded_wave_corpus,
    score_rule_adherence,
)
from eawf.observability.eval.self_eval import (
    MIN_SELF_EVAL_N,
    SelfEvalStatus,
    SelfEvalSurface,
    compute_self_eval,
    summarize_self_eval,
)

__all__ = [
    "CONSENSUS_SPREAD",
    "FAIL_SCORE_THRESHOLD",
    "JURY_QUORUM",
    "JURY_RUNTIME_FAMILIES",
    "MIN_SELF_EVAL_N",
    "PASS_SCORE_THRESHOLD",
    "RULE_IDS",
    "CrossVendorJuryResult",
    "EvalScore",
    "JurorBallot",
    "JurorOutcome",
    "JuryAggregate",
    "JuryAggregateOutcome",
    "RecordedWaveCase",
    "ReliabilityStatus",
    "ReputationConfig",
    "RoleReliability",
    "RuleAdherenceBaseline",
    "RuleAdherenceReport",
    "SelfEvalStatus",
    "SelfEvalSurface",
    "SpawnFactory",
    "VerdictOutcome",
    "aggregate_jury",
    "build_verdict_outcomes",
    "compute_role_reliability",
    "compute_self_eval",
    "confidence_to_float",
    "convene_cross_vendor_jury",
    "load_recorded_wave_cases",
    "score_envelope",
    "score_recorded_wave_corpus",
    "score_rule_adherence",
    "summarize_self_eval",
]
