"""Recorded wave corpus checks for rule-adherence Exp-1 baseline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from eawf.observability.eval.rule_adherence import RULE_IDS, load_recorded_wave_cases
from eawf.observability.eval.score import score_recorded_wave_corpus

_RECORDED_DIR = Path(__file__).resolve().parent / "waves" / "recorded"


def _load_baseline() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((_RECORDED_DIR / "baseline.json").read_text(encoding="utf-8")),
    )


def test_recorded_wave_corpus_matches_exp1_baseline() -> None:
    cases = load_recorded_wave_cases(_RECORDED_DIR)
    baseline = _load_baseline()

    assert len(cases) == baseline["corpus_count"]
    assert sum(case.verify_count for case in cases) == baseline["verify_count"]
    assert baseline["checker_count"] == len(RULE_IDS)
    assert score_recorded_wave_corpus(cases).model_dump(mode="json") == baseline


def test_recorded_wave_expected_failures_match_checker_output() -> None:
    cases = load_recorded_wave_cases(_RECORDED_DIR)

    for case in cases:
        report = score_recorded_wave_corpus((case,)).cases[0]
        assert report.failed_rules == case.expected_failed_rules
