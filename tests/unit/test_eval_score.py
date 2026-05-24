"""Unit tests for :func:`eawf.observability.eval.score.score_envelope` (B042, P13-W03).

Covers the per-dimension scoring matrix:

- Perfect match → ``total == 1.0``.
- Single-dimension mismatch → ``total`` drops by exactly that dimension's
  weight.
- Warnings / repair_commands deltas respect the ±1 tolerance.
- ``evidence_refs`` presence mismatch zeroes its dimension.
- ``state_mutation_kinds`` set drift zeroes its dimension.
- Missing-from-golden fields score 1.0 (no-signal, no-penalty).
- :class:`EvalScore` rejects extras.

The synthetic envelopes are constructed via
:meth:`OutputEnvelope.model_validate` so the tests follow the same
typed-shape contract the production code expects.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.observability.eval import EvalScore, score_envelope
from eawf.surfaces.render.envelope import OutputEnvelope


def _envelope(
    *,
    status: str = "ok",
    body: dict[str, Any] | str | None = None,
    warnings_count: int = 0,
    repair_commands_count: int = 0,
    evidence_refs: list[str] | None = None,
    state_mutations: list[str] | None = None,
) -> OutputEnvelope:
    """Build a typed :class:`OutputEnvelope` with knobs for each scoring dim.

    The header uses canonical URN-shaped placeholders and the
    ``"/research"`` skill literal. The footer is hand-stitched so we
    can flip individual scoring inputs without touching the rest.
    """
    base_body: dict[str, Any] | str = {"alpha": 1, "beta": 2} if body is None else body
    warnings = [{"code": f"warn{i}", "detail": f"detail-{i}"} for i in range(warnings_count)]
    repair_commands = [f"repair-cmd-{i}" for i in range(repair_commands_count)]
    payload: dict[str, Any] = {
        "header": {
            "skill": "/research",
            "scope_id": "urn:eawf:v1:state:QR/P00",
            "session": "urn:eawf:v1:store:QR/sessions/SES-001",
            "started_at": "2026-05-09T00:00:00Z",
            "finished_at": "2026-05-09T00:00:01Z",
            "status": status,
            "instrument_probe": {},
        },
        "body": base_body,
        "footer": {
            "warnings": warnings,
            "evidence_refs": list(evidence_refs or []),
            "state_mutations": list(state_mutations or []),
            "repair_commands": repair_commands if repair_commands else None,
        },
    }
    return OutputEnvelope.model_validate(payload)


def _golden(**overrides: Any) -> dict[str, Any]:
    """Canonical golden matching the default :func:`_envelope` output."""
    base: dict[str, Any] = {
        "skill": "/research",
        "status": "ok",
        "body_keys": ["alpha", "beta"],
        "warnings_count": 0,
        "repair_commands_count": 0,
        "evidence_refs_present": False,
        "state_mutation_kinds": [],
        "eval_score_threshold": 0.85,
    }
    base.update(overrides)
    return base


# ---- happy path -------------------------------------------------------------


def test_score_envelope_perfect_match() -> None:
    """Every dimension at 1.0 → ``total == 1.0`` and threshold honoured."""
    score = score_envelope(_envelope(), _golden())
    assert score.total == pytest.approx(1.0)
    assert score.pass_threshold == pytest.approx(0.85)
    # All six dimensions scored 1.0.
    assert set(score.per_dim.keys()) == {
        "status",
        "body_keys",
        "warnings",
        "repair_commands",
        "evidence_refs",
        "state_mutation_kinds",
    }
    for value in score.per_dim.values():
        assert value == pytest.approx(1.0)


# ---- status / body_keys mismatches ------------------------------------------


def test_score_envelope_status_mismatch() -> None:
    """Wrong status → total drops by exactly the status weight (0.25)."""
    env = _envelope(status="failed")
    # _envelope with status=failed needs repair_commands set; bump knob.
    env = _envelope(
        status="failed",
        repair_commands_count=1,
    )
    golden = _golden(repair_commands_count=1)
    score = score_envelope(env, golden)
    assert score.per_dim["status"] == pytest.approx(0.0)
    assert score.total == pytest.approx(0.75)
    assert score.total <= 0.75


def test_score_envelope_body_keys_mismatch() -> None:
    """Body-key set drift → body_keys dim 0.0; total drops by 0.25."""
    env = _envelope(body={"alpha": 1, "gamma": 3})
    score = score_envelope(env, _golden())
    assert score.per_dim["body_keys"] == pytest.approx(0.0)
    assert score.total == pytest.approx(0.75)


def test_score_envelope_body_keys_string_body() -> None:
    """Non-dict body collapses to an empty key list; mismatch when golden lists keys."""
    env = _envelope(body="raw markdown")
    score = score_envelope(env, _golden())
    assert score.per_dim["body_keys"] == pytest.approx(0.0)


# ---- count tolerance --------------------------------------------------------


def test_score_envelope_warnings_within_tolerance() -> None:
    """``abs(live - golden) <= 1`` → dim score 1.0."""
    env = _envelope(warnings_count=1)
    score = score_envelope(env, _golden(warnings_count=0))
    assert score.per_dim["warnings"] == pytest.approx(1.0)
    assert score.total == pytest.approx(1.0)


def test_score_envelope_warnings_outside_tolerance() -> None:
    """``abs(live - golden) > 1`` → dim score 0.0; total drops by 0.15."""
    env = _envelope(warnings_count=5)
    score = score_envelope(env, _golden(warnings_count=0))
    assert score.per_dim["warnings"] == pytest.approx(0.0)
    assert score.total == pytest.approx(0.85)


def test_score_envelope_repair_count_outside_tolerance() -> None:
    """Repair-command count delta beyond ±1 zeroes the dim."""
    env = _envelope(
        status="failed",
        repair_commands_count=5,
    )
    score = score_envelope(env, _golden(status="failed", repair_commands_count=0))
    assert score.per_dim["repair_commands"] == pytest.approx(0.0)
    assert score.total == pytest.approx(0.85)


def test_score_envelope_repair_count_within_tolerance() -> None:
    """Repair-command count delta ≤1 keeps the dim at 1.0."""
    env = _envelope(
        status="failed",
        repair_commands_count=1,
    )
    score = score_envelope(env, _golden(status="failed", repair_commands_count=0))
    assert score.per_dim["repair_commands"] == pytest.approx(1.0)


# ---- evidence_refs presence -------------------------------------------------


def test_score_envelope_evidence_refs_presence_mismatch() -> None:
    """Golden expects refs but live has none → dim 0.0; total drops by 0.10."""
    env = _envelope(evidence_refs=[])
    score = score_envelope(env, _golden(evidence_refs_present=True))
    assert score.per_dim["evidence_refs"] == pytest.approx(0.0)
    assert score.total == pytest.approx(0.90)


def test_score_envelope_evidence_refs_both_present() -> None:
    """Both non-empty → dim 1.0."""
    env = _envelope(evidence_refs=["urn:eawf:v1:state:QR/H01-01"])
    score = score_envelope(env, _golden(evidence_refs_present=True))
    assert score.per_dim["evidence_refs"] == pytest.approx(1.0)


# ---- state_mutation kinds ---------------------------------------------------


def test_score_envelope_state_mutation_kinds_match() -> None:
    """Kind sets match → dim 1.0."""
    env = _envelope(state_mutations=["$.iterations.iter-001"])
    score = score_envelope(env, _golden(state_mutation_kinds=["iterations"]))
    assert score.per_dim["state_mutation_kinds"] == pytest.approx(1.0)


def test_score_envelope_state_mutation_kinds_subset_fails() -> None:
    """Kind set drift → dim 0.0; total drops by 0.10."""
    env = _envelope(state_mutations=["$.iterations.iter-001"])
    score = score_envelope(env, _golden(state_mutation_kinds=["audits"]))
    assert score.per_dim["state_mutation_kinds"] == pytest.approx(0.0)
    assert score.total == pytest.approx(0.90)


def test_score_envelope_state_mutation_kinds_unanchored() -> None:
    """Mutations without a leading ``$`` still extract the head kind."""
    env = _envelope(state_mutations=["iterations.iter-001"])
    score = score_envelope(env, _golden(state_mutation_kinds=["iterations"]))
    assert score.per_dim["state_mutation_kinds"] == pytest.approx(1.0)


# ---- missing-field tolerance ------------------------------------------------


def test_score_envelope_missing_golden_field_scores_one() -> None:
    """Golden missing ``evidence_refs_present`` → dim defaults to 1.0."""
    golden = _golden()
    golden.pop("evidence_refs_present")
    score = score_envelope(_envelope(), golden)
    assert score.per_dim["evidence_refs"] == pytest.approx(1.0)
    assert score.total == pytest.approx(1.0)


def test_score_envelope_missing_state_mutations_field_scores_one() -> None:
    """Golden missing ``state_mutation_kinds`` → dim defaults to 1.0."""
    golden = _golden()
    golden.pop("state_mutation_kinds")
    score = score_envelope(_envelope(), golden)
    assert score.per_dim["state_mutation_kinds"] == pytest.approx(1.0)


def test_score_envelope_default_threshold_when_missing() -> None:
    """Golden missing ``eval_score_threshold`` → ``pass_threshold`` defaults to 0.85."""
    golden = _golden()
    golden.pop("eval_score_threshold")
    score = score_envelope(_envelope(), golden)
    assert score.pass_threshold == pytest.approx(0.85)


def test_score_envelope_threshold_override() -> None:
    """Per-fixture override propagates to ``EvalScore.pass_threshold``."""
    score = score_envelope(_envelope(), _golden(eval_score_threshold=0.95))
    assert score.pass_threshold == pytest.approx(0.95)


# ---- EvalScore model contract -----------------------------------------------


def test_eval_score_extra_field_rejected() -> None:
    """``EvalScore`` rejects unknown fields per ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        EvalScore(
            total=1.0,
            per_dim={"status": 1.0},
            pass_threshold=0.85,
            extra="boom",  # type: ignore[call-arg]
        )


def test_eval_score_is_frozen() -> None:
    """``EvalScore`` is immutable; assignment raises ``ValidationError``."""
    score = score_envelope(_envelope(), _golden())
    with pytest.raises(ValidationError):
        score.total = 0.0  # type: ignore[misc]
