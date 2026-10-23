"""An executor report records its test runs and names its repro tests by one form."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.store.kinds.agent_report import (
    ExecutorReportBody,
    ExecutorTestRun,
)
from eawf.platform.lint.kind_taxonomy import REPRO_TEST_NAME
from eawf.surfaces.render.agents import _EXECUTOR_BODY
from eawf.workflow.agents.specs.models import SubagentSpec

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
