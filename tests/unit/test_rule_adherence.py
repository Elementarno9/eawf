"""Rule-adherence scorer tests for recorded wave evals."""

from __future__ import annotations

import pytest

from eawf.observability.eval.rule_adherence import RULE_IDS, RecordedWaveCase
from eawf.observability.eval.score import (
    score_recorded_wave_corpus,
    score_rule_adherence,
)


def _case(
    *,
    transcript: str = "",
    commands: tuple[str, ...] = ("rtk git status --short",),
    changed_files: tuple[str, ...] = ("src/eawf/observability/eval/score.py",),
    verify_count: int = 1,
) -> RecordedWaveCase:
    return RecordedWaveCase(
        case_id="case",
        transcript=transcript,
        commands=commands,
        changed_files=changed_files,
        verify_count=verify_count,
    )


def test_score_rule_adherence_clean_case_passes_all() -> None:
    report = score_rule_adherence(_case())

    assert report.advisory_only is True
    assert report.checker_count == 6
    assert report.passed is True
    assert report.score == pytest.approx(1.0)
    assert report.failed_rules == ()
    assert {finding.rule_id for finding in report.findings} == set(RULE_IDS)
    assert {finding.severity for finding in report.findings} == {"advisory"}


@pytest.mark.parametrize(
    ("case", "failed_rule"),
    (
        (
            _case(commands=("uv run pytest tests/unit/test_eval_score.py",)),
            "commands_use_rtk",
        ),
        (
            _case(commands=("rtk pytest tests/unit/test_eval_score.py",)),
            "python_invocations_use_uv_run",
        ),
        (
            _case(transcript=f"wrote trace to {'/' + 'tmp/eawf/session.log'}"),
            "no_local_path_leaks",
        ),
        (
            _case(changed_files=(".ea/state.json",)),
            "no_direct_state_json_edits",
        ),
        (
            _case(transcript=("class LooseModel(BaseModel):\n    name: str\n")),
            "pydantic_models_forbid_extra",
        ),
        (
            _case(commands=("rtk git reset --hard HEAD",)),
            "no_destructive_git_reverts",
        ),
    ),
)
def test_score_rule_adherence_flags_each_checker(
    case: RecordedWaveCase,
    failed_rule: str,
) -> None:
    report = score_rule_adherence(case)

    assert report.advisory_only is True
    assert report.failed_rules == (failed_rule,)
    assert report.score == pytest.approx(5 / 6)
    assert all(finding.severity == "advisory" for finding in report.findings)


def test_score_rule_adherence_redacts_local_path_evidence() -> None:
    report = score_rule_adherence(_case(transcript=f"local log {'/' + 'tmp/eawf/session.log'}"))

    finding = next(item for item in report.findings if item.rule_id == "no_local_path_leaks")
    assert finding.passed is False
    assert finding.evidence == ("<redacted-local-path>",)


def test_score_recorded_wave_corpus_aggregates_counts() -> None:
    clean = _case(verify_count=2)
    bare_python = _case(
        commands=("rtk python -m pytest tests/unit/test_eval_score.py",),
        verify_count=3,
    )

    baseline = score_recorded_wave_corpus((clean, bare_python))

    assert baseline.experiment_id == "Exp-1"
    assert baseline.advisory_only is True
    assert baseline.checker_count == 6
    assert baseline.corpus_count == 2
    assert baseline.verify_count == 5
    assert baseline.pass_rate == pytest.approx(11 / 12)
    assert baseline.rule_pass_rates["python_invocations_use_uv_run"] == pytest.approx(0.5)
