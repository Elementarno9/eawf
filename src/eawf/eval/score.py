"""Weighted scoring loop for the skill-eval harness (B042).

Layers a semantic score on top of the shape-only guard introduced in
P12-W05 (B033). Given a live :class:`~eawf.render.envelope.OutputEnvelope`
and the matching golden-fixture dict, :func:`score_envelope` returns an
:class:`~eawf.eval.models.EvalScore` with six dimension scores and the
weighted total.

Weight table (sums to 1.00):

==========================  =====
Dimension                   Weight
==========================  =====
``status``                  0.25
``body_keys``               0.25
``warnings``                0.15
``repair_commands``         0.15
``evidence_refs``           0.10
``state_mutation_kinds``    0.10
==========================  =====

Boundary semantics: when the golden lacks a dimension's key entirely
(``evidence_refs_present`` / ``state_mutation_kinds``) the dimension
scores ``1.0`` — no signal, no penalty. This keeps the suite green
pending later footer additions (P14+).
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.eval.models import EvalScore
from eawf.render.envelope import OutputEnvelope

logger = logging.getLogger(__name__)


# Dimension weights — keep in sync with the docstring table above.
_WEIGHTS: dict[str, float] = {
    "status": 0.25,
    "body_keys": 0.25,
    "warnings": 0.15,
    "repair_commands": 0.15,
    "evidence_refs": 0.10,
    "state_mutation_kinds": 0.10,
}

# Tolerance (inclusive) for the warnings / repair_commands count deltas.
_COUNT_TOLERANCE: int = 1


def _kinds(mutations: list[str]) -> frozenset[str]:
    """Extract the kind-set from a JSONPath-ish ``state_mutations`` list.

    Each mutation in v0.2 is a string like ``$.iterations.<id>``. We treat
    the first dotted segment (after the leading ``$.``) as the *kind*.
    Empty strings collapse to ``""``.
    """
    out: set[str] = set()
    for m in mutations:
        if not m:
            out.add("")
            continue
        # Strip optional leading ``$`` and ``.`` before reading the first
        # path segment.
        stripped = m.lstrip("$").lstrip(".")
        if not stripped:
            out.add("")
            continue
        head = stripped.split(".", 1)[0]
        out.add(head)
    return frozenset(out)


def _score_status(env: OutputEnvelope, golden: dict[str, Any]) -> float:
    expected = golden.get("status")
    return 1.0 if expected is not None and env.header.status == expected else 0.0


def _score_body_keys(env: OutputEnvelope, golden: dict[str, Any]) -> float:
    expected = golden.get("body_keys")
    if expected is None:
        return 1.0
    live_keys = sorted(env.body.keys()) if isinstance(env.body, dict) else []
    return 1.0 if live_keys == list(expected) else 0.0


def _score_count_within_tolerance(live: int, golden: int) -> float:
    return 1.0 if abs(live - golden) <= _COUNT_TOLERANCE else 0.0


def _score_warnings(env: OutputEnvelope, golden: dict[str, Any]) -> float:
    if "warnings_count" not in golden:
        return 1.0
    live = len(env.footer.warnings) if env.footer.warnings else 0
    return _score_count_within_tolerance(live, int(golden["warnings_count"]))


def _score_repair_commands(env: OutputEnvelope, golden: dict[str, Any]) -> float:
    if "repair_commands_count" not in golden:
        return 1.0
    live = len(env.footer.repair_commands) if env.footer.repair_commands else 0
    return _score_count_within_tolerance(live, int(golden["repair_commands_count"]))


def _score_evidence_refs(env: OutputEnvelope, golden: dict[str, Any]) -> float:
    if "evidence_refs_present" not in golden:
        return 1.0
    expected_present = bool(golden["evidence_refs_present"])
    live_present = bool(env.footer.evidence_refs)
    return 1.0 if expected_present == live_present else 0.0


def _score_state_mutation_kinds(env: OutputEnvelope, golden: dict[str, Any]) -> float:
    if "state_mutation_kinds" not in golden:
        return 1.0
    expected_kinds = frozenset(str(k) for k in golden["state_mutation_kinds"])
    live_kinds = _kinds(list(env.footer.state_mutations))
    return 1.0 if live_kinds == expected_kinds else 0.0


def score_envelope(env: OutputEnvelope, golden: dict[str, Any]) -> EvalScore:
    """Compute weighted similarity between a live skill envelope and its golden fixture.

    Returns an :class:`EvalScore` with per-dimension breakdown. Boundary
    semantics: when the golden lacks an ``evidence_refs_present`` /
    ``state_mutation_kinds`` field the matching dimension scores 1.0
    (no signal, no penalty). The same rule applies if a footer field is
    absent from the live envelope (currently impossible because
    :class:`~eawf.render.envelope.EnvelopeFooter` defaults both to empty
    lists, but the scorer is forward-compatible with future v0.3+
    footer reshuffles).

    Args:
        env: Live envelope produced by
            :func:`~eawf.skills.engine.run_skill`.
        golden: Parsed golden-fixture dict — typically the JSON-decoded
            contents of ``tests/eval/golden/<slug>.json``.

    Returns:
        :class:`EvalScore` whose ``total`` is the weighted sum of the
        six per-dimension scores and whose ``pass_threshold`` reflects
        the fixture's ``eval_score_threshold`` (default ``0.85``).
    """
    per_dim: dict[str, float] = {
        "status": _score_status(env, golden),
        "body_keys": _score_body_keys(env, golden),
        "warnings": _score_warnings(env, golden),
        "repair_commands": _score_repair_commands(env, golden),
        "evidence_refs": _score_evidence_refs(env, golden),
        "state_mutation_kinds": _score_state_mutation_kinds(env, golden),
    }

    total = sum(_WEIGHTS[k] * per_dim[k] for k in _WEIGHTS)
    # Clamp away tiny floating-point drift so a perfect match reports
    # exactly 1.0 (or whatever the weight sum is).
    if total > 1.0:
        total = 1.0
    threshold = float(golden.get("eval_score_threshold", 0.85))
    logger.debug(f"score_envelope: total={total:.4f} per_dim={per_dim}")
    return EvalScore(total=total, per_dim=per_dim, pass_threshold=threshold)


__all__ = ["score_envelope"]
