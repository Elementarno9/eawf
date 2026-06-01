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

__all__ = [
    "CONSENSUS_SPREAD",
    "FAIL_SCORE_THRESHOLD",
    "PASS_SCORE_THRESHOLD",
    "RULE_IDS",
    "EvalScore",
    "JurorBallot",
    "JuryAggregate",
    "JuryAggregateOutcome",
    "RecordedWaveCase",
    "RuleAdherenceBaseline",
    "RuleAdherenceReport",
    "aggregate_jury",
    "load_recorded_wave_cases",
    "score_envelope",
    "score_recorded_wave_corpus",
    "score_rule_adherence",
]
