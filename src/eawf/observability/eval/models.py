"""Typed result model for the skill-eval semantic scoring loop (B042).

The :class:`EvalScore` model wraps the output of
:func:`eawf.observability.eval.score.score_envelope` so callers (the regression test
and operator-facing CLI tools downstream) can read a transparent
per-dimension breakdown alongside the weighted total.

The model is frozen (``frozen=True``) and rejects extras (``extra="forbid"``)
so accidental drift in the dimension keys surfaces as a
:class:`pydantic.ValidationError` at construction time.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EvalScore(BaseModel):
    """Weighted similarity score between a live envelope and its golden fixture.

    Attributes:
        total: Weighted sum across all dimensions; in the inclusive range
            ``[0.0, 1.0]``.
        per_dim: Per-dimension normalised scores (each in ``[0.0, 1.0]``).
            Keyed by the six dimension names — ``status``, ``body_keys``,
            ``warnings``, ``repair_commands``, ``evidence_refs``,
            ``state_mutation_kinds``. The dict gives the operator a
            transparent breakdown the regression failure message can quote.
        pass_threshold: Floor for :attr:`total` below which the regression
            should fail. Defaults to ``0.85``; per-fixture overrides are
            permitted via the golden's ``eval_score_threshold`` field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: float
    per_dim: dict[str, float]
    pass_threshold: float = 0.85


__all__ = ["EvalScore"]
