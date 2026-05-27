"""Recorded-corpus regression tests for rule-adherence experiments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from eawf.observability.eval.experiments import (
    AB_MIN_DELTA_PP,
    MechanismCandidate,
    dump_rule_adherence_experiments,
    meets_ab_threshold,
    run_recorded_rule_adherence_experiments,
)

_EVAL_DIR = Path(__file__).resolve().parents[1]
_RECORDED_DIR = _EVAL_DIR / "waves" / "recorded"
_GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "rule_adherence_experiments.json"


def _load_golden() -> dict[str, object]:
    return cast(dict[str, object], json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")))


def test_rule_adherence_experiments_run_exp1_exp2_exp3() -> None:
    report = run_recorded_rule_adherence_experiments(_RECORDED_DIR)

    assert report.experiment_ids == ("Exp-1", "Exp-2", "Exp-3")
    assert report.corpus_count == 6
    assert report.verify_count == 7
    assert len(report.violation_mining.per_rule_baselines) == 6
    assert report.tiered_ab.comparison.min_delta_pp == pytest.approx(AB_MIN_DELTA_PP)
    assert report.tiered_ab.comparison.meets_threshold is True
    assert report.mechanism_bakeoff.winner_id == "runtime_guardrails"


def test_rule_adherence_experiments_match_golden() -> None:
    report = run_recorded_rule_adherence_experiments(_RECORDED_DIR)

    assert report.model_dump(mode="json") == _load_golden()
    assert dump_rule_adherence_experiments(report) == _GOLDEN_PATH.read_text(encoding="utf-8")


def test_ab_threshold_uses_two_percentage_points() -> None:
    assert meets_ab_threshold(0.80, 0.82) is True
    assert meets_ab_threshold(0.80, 0.819) is False


def test_ab_threshold_rejects_negative_min_delta() -> None:
    with pytest.raises(ValueError, match="min_delta_rate must be >= 0"):
        meets_ab_threshold(0.80, 0.79, min_delta_rate=-0.01)


def test_mechanism_candidate_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        MechanismCandidate.model_validate(
            {
                "mechanism_id": "x",
                "label": "X",
                "covered_rules": ["commands_use_rtk"],
                "unexpected": True,
            }
        )
