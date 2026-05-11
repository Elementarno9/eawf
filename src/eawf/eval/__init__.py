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

from eawf.eval.models import EvalScore
from eawf.eval.score import score_envelope

__all__ = ["EvalScore", "score_envelope"]
