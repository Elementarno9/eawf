"""Wave close runs the red-to-green check over an executor report's test runs."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentReportVerdict, Confidence
from eawf.kernel.store.kinds.agent_report import (
    ExecutorReportBody,
    ExecutorTestRun,
    PlannerReportBody,
)
from eawf.platform.lint.kind_taxonomy import REPRO_TEST_NAME
from eawf.surfaces.render.agents import _EXECUTOR_BODY
from eawf.workflow.agents.specs.models import SubagentSpec
from eawf.workflow.verify.dispatch_close import (
    verify_close_readiness,
)
from eawf.workflow.verify.red_to_green import red_to_green_close_findings

_WAVE_ID = "P01-I01-W01"
_UNIT_TEST = "tests/unit/test_x.py::test_x"
_REPRO_TEST = "tests/unit/test_x.py::test_x_repro_42"


def _run(test_id: str, outcome: str, revision: str = "abcdef1") -> dict[str, str]:
    return {"test_id": test_id, "outcome": outcome, "revision": revision}


def _report(*runs: dict[str, str], **overrides: Any) -> ExecutorReportBody:
    payload: dict[str, Any] = {
        "role": "executor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "done",
        "wave_id": _WAVE_ID,
        "outcome": "done",
        "commit_sha": "abcdef1",
        "test_runs": list(runs),
    }
    payload.update(overrides)
    return ExecutorReportBody.model_validate(payload)


# ---- ExecutorTestRun schema ------------------------------------------------


def test_executor_report_body_parses_without_test_runs() -> None:
    body = _report()
    body_without_field = ExecutorReportBody.model_validate(
        {k: v for k, v in body.model_dump(mode="json").items() if k != "test_runs"}
    )
    assert body_without_field.test_runs == []


def test_executor_test_run_rejects_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate(_run(_UNIT_TEST, "yellow"))


def test_executor_test_run_rejects_empty_test_id() -> None:
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate(_run("", "red"))


def test_executor_test_run_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate({**_run(_UNIT_TEST, "red"), "duration": 1})


def test_executor_test_run_accepts_max_length_test_id() -> None:
    assert ExecutorTestRun.model_validate(_run("t" * 500, "red")).test_id == "t" * 500
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate(_run("t" * 501, "red"))


# ---- red_to_green_close_findings ------------------------------------------


def test_red_to_green_close_findings_empty_runs_raise_nothing() -> None:
    assert red_to_green_close_findings(_report()) == ()


def test_red_to_green_close_findings_red_then_green_passes() -> None:
    report = _report(_run(_UNIT_TEST, "red", "worktree"), _run(_UNIT_TEST, "green"))
    assert red_to_green_close_findings(report) == ()


def test_red_to_green_close_findings_single_green_is_advisory() -> None:
    (finding,) = red_to_green_close_findings(_report(_run(_UNIT_TEST, "green")))
    assert not finding.blocking
    assert finding.finding.test_id == _UNIT_TEST
    assert "has no earlier red run" in finding.render()
    assert finding.render().endswith("(advisory)")


def test_red_to_green_close_findings_repro_without_red_blocks() -> None:
    (finding,) = red_to_green_close_findings(_report(_run(_REPRO_TEST, "green")))
    assert finding.blocking
    assert finding.render().endswith("(blocks close)")


def test_red_to_green_close_findings_groups_runs_per_test() -> None:
    report = _report(
        _run(_UNIT_TEST, "red", "worktree"),
        _run(_REPRO_TEST, "green"),
        _run(_UNIT_TEST, "green"),
    )
    findings = red_to_green_close_findings(report)
    assert [f.finding.test_id for f in findings] == [_REPRO_TEST]


def test_red_to_green_close_findings_skips_tests_above_wave_tier() -> None:
    report = _report(
        _run("tests/golden/test_render.py::test_render", "green"),
        _run("tests/e2e/test_flow.py::test_flow", "green"),
    )
    assert red_to_green_close_findings(report) == ()


def test_red_to_green_close_findings_checks_tests_outside_kind_dirs() -> None:
    (finding,) = red_to_green_close_findings(_report(_run("tests/lint/test_a.py::test_a", "green")))
    assert finding.finding.test_id == "tests/lint/test_a.py::test_a"


def test_red_to_green_close_findings_flags_final_red_run() -> None:
    report = _report(
        _run(_UNIT_TEST, "red", "worktree"),
        _run(_UNIT_TEST, "green"),
        _run(_UNIT_TEST, "red"),
    )
    (finding,) = red_to_green_close_findings(report)
    assert "is red" in finding.finding.reason


def test_red_to_green_close_findings_rejects_non_executor_body() -> None:
    planner = PlannerReportBody(
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="plan",
        objective="plan",
    )
    with pytest.raises(TypeError, match="ExecutorReportBody"):
        red_to_green_close_findings(planner)  # type: ignore[arg-type]


# ---- verify_close_readiness wiring ----------------------------------------


def test_verify_close_readiness_flags_green_without_red_as_advisory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="eawf.workflow.verify.dispatch_close"):
        result = verify_close_readiness(_WAVE_ID, _report(_run(_UNIT_TEST, "green")))
    assert result.passed
    assert len(result.advisories) == 1
    assert _UNIT_TEST in result.advisories[0]
    assert "advisory=" in caplog.text


def test_verify_close_readiness_blocks_repro_without_red() -> None:
    result = verify_close_readiness(_WAVE_ID, _report(_run(_REPRO_TEST, "green")))
    assert not result.passed
    assert any(_REPRO_TEST in reason for reason in result.reasons)
    assert result.advisories == ()


def test_verify_close_readiness_clean_pair_has_no_advisory() -> None:
    report = _report(_run(_REPRO_TEST, "red", "worktree"), _run(_REPRO_TEST, "green"))
    result = verify_close_readiness(_WAVE_ID, report)
    assert result.passed
    assert result.advisories == ()


# ---- executor prompt contract ----------------------------------------------


def test_render_headless_executor_asks_for_test_runs() -> None:
    spec = SubagentSpec.model_validate(
        {
            "wave_id": _WAVE_ID,
            "iter_id": "P01-I01",
            "title": "Solo wave",
            "scope_id": "QR",
            "agent_role": "executor",
            "file_scopes": ["src/"],
        }
    )
    out = spec.render(headless=True)
    assert '"test_runs": [{"test_id"' in out
    assert "never ran red first" in out
    assert f"`{REPRO_TEST_NAME.form}`" in out


# ---- the repro naming convention -------------------------------------------


def test_executor_body_renders_the_repro_form_from_the_constant() -> None:
    assert f"`{REPRO_TEST_NAME.form}`" in _EXECUTOR_BODY
    assert "`_repro_` test" not in _EXECUTOR_BODY


@pytest.mark.parametrize(
    "test_id",
    [
        _REPRO_TEST,
        "tests/unit/test_x.py::test_repro_7",
        "tests/unit/test_x.py::test_x_repro_7[case-1]",
        "tests/unit/test_x.py::TestX::test_x_repro_7",
    ],
)
def test_repro_form_matches_a_repro_function_name(test_id: str) -> None:
    assert REPRO_TEST_NAME.matches(test_id)


@pytest.mark.parametrize(
    "test_id",
    [
        "tests/unit/test_a_repro_1.py::test_b",
        "tests/unit/test_x.py::TestA_repro_1::test_b",
        "tests/unit/test_x.py::test_nonrepro_case",
        "tests/unit/test_x.py::test_x_repro_",
        "tests/unit/test_x.py::test_x_reproduce_42",
        "",
    ],
)
def test_repro_form_ignores_ids_that_merely_contain_the_marker(test_id: str) -> None:
    assert not REPRO_TEST_NAME.matches(test_id)


def test_marker_outside_the_function_name_stays_advisory() -> None:
    """A ``_repro_`` module path no longer turns every test in it into a blocker."""
    test_id = "tests/unit/test_a_repro_1.py::test_b"
    (finding,) = red_to_green_close_findings(_report(_run(test_id, "green")))
    assert finding.blocking is False


def test_verify_close_readiness_passes_marker_outside_function_name() -> None:
    result = verify_close_readiness(
        _WAVE_ID, _report(_run("tests/unit/test_a_repro_1.py::test_b", "green"))
    )
    assert result.passed
    assert len(result.advisories) == 1
