"""LINT-034 (budget half): the budget gate detects an injected duration regression.

The gate diffs a JUnit report against the committed baseline. A test that
doubled past its baseline, a new test slower than twice the floor, and a
fast-tier test over its tier's wall-time ceiling are each findings; jitter
under the floor never is.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import pytest
from pydantic import ValidationError

from eawf.platform.lint.duration_budget import (
    REGRESSION_FLOOR_SECONDS,
    DurationBaseline,
    budget_findings,
    junit_durations,
    load_baseline,
    record_baseline,
)
from eawf.platform.lint.kind_taxonomy import TIER_RUNTIME_BUDGET_SECONDS, GateTier

_REPO = Path(__file__).resolve().parents[4]
_CEILING = TIER_RUNTIME_BUDGET_SECONDS[GateTier.WAVE]
_UNIT = "tests.unit.platform.lint.test_x::test_slow"
_GOLDEN = "tests.golden.render.test_y::test_frames"


def _junit(cases: dict[str, float]) -> str:
    rows = "".join(
        f'<testcase classname="{key.split("::")[0]}" name="{key.split("::")[1]}" time="{seconds}"/>'
        for key, seconds in cases.items()
    )
    return f'<testsuites><testsuite name="pytest">{rows}</testsuite></testsuites>'


def _baseline(durations: dict[str, float]) -> DurationBaseline:
    return DurationBaseline(revision="abc1234", durations=durations)


def test_lint_034_the_budget_gate_detects_an_injected_duration_regression() -> None:
    """Gate fire: a 1.0s baseline test measured at 2.5s."""
    measured = junit_durations(_junit({_UNIT: 2.5}))

    (finding,) = budget_findings(measured, _baseline({_UNIT: 1.0}))

    assert finding.test == _UNIT
    assert "against a 1.000s baseline" in finding.render()


def test_lint_034_exactly_twice_the_baseline_is_within_budget() -> None:
    assert budget_findings({_UNIT: 2.0}, _baseline({_UNIT: 1.0})) == []


def test_lint_034_jitter_on_a_fast_test_is_never_a_finding() -> None:
    """A 0.01s test that grew tenfold is judged against the floor, not its own time."""
    assert budget_findings({_UNIT: 0.1}, _baseline({_UNIT: 0.01})) == []
    assert budget_findings({_UNIT: REGRESSION_FLOOR_SECONDS * 2}, _baseline({})) == []


def test_lint_034_a_new_test_slower_than_twice_the_floor_is_a_finding() -> None:
    (finding,) = budget_findings({_UNIT: REGRESSION_FLOOR_SECONDS * 2 + 0.001}, _baseline({}))

    assert "against a 0.500s baseline" in finding.reason


def test_lint_034_a_fast_tier_test_exactly_at_its_ceiling_passes() -> None:
    assert budget_findings({_UNIT: float(_CEILING)}, _baseline({_UNIT: 40.0})) == []


def test_lint_034_a_fast_tier_test_one_millisecond_over_its_ceiling_fails() -> None:
    (finding,) = budget_findings({_UNIT: _CEILING + 0.001}, _baseline({_UNIT: 40.0}))

    assert f"over the {_CEILING}s fast-tier ceiling" in finding.reason


def test_lint_034_a_slower_tier_is_not_held_to_the_fast_ceiling() -> None:
    assert budget_findings({_GOLDEN: _CEILING + 5.0}, _baseline({_GOLDEN: 60.0})) == []


def test_lint_034_an_empty_run_has_no_findings() -> None:
    assert budget_findings({}, _baseline({})) == []


def test_lint_034_junit_durations_keeps_the_slowest_rerun() -> None:
    xml = _junit({_UNIT: 0.2}).replace(
        "</testsuite>",
        f'<testcase classname="{_UNIT.split("::")[0]}" name="test_slow" time="0.9"/></testsuite>',
    )

    assert junit_durations(xml) == {_UNIT: pytest.approx(0.9)}


def test_lint_034_junit_durations_rejects_a_report_that_is_not_xml() -> None:
    with pytest.raises(ParseError):
        junit_durations("not xml")


def test_lint_034_junit_durations_rejects_a_non_numeric_time() -> None:
    with pytest.raises(ValueError):
        junit_durations(_junit({_UNIT: 0.1}).replace('time="0.1"', 'time="fast"'))


def test_lint_034_record_keeps_only_tests_at_or_above_the_floor() -> None:
    baseline = record_baseline(
        {_UNIT: REGRESSION_FLOOR_SECONDS, "a::b": REGRESSION_FLOOR_SECONDS - 0.001},
        revision="abc1234",
    )

    assert baseline.durations == {_UNIT: REGRESSION_FLOOR_SECONDS}


@pytest.mark.parametrize(
    "payload",
    [
        {"revision": "", "durations": {}},
        {"revision": "abc", "durations": {"a::b": -1.0}},
        {"revision": "abc", "durations": {}, "extra": 1},
        {"durations": {}},
    ],
)
def test_lint_034_load_baseline_refuses_a_malformed_file(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_baseline(path)


def _tool() -> Any:
    spec = importlib.util.spec_from_file_location(
        "duration_budget_gate", _REPO / "tools" / "duration_budget_gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["duration_budget_gate"] = module
    spec.loader.exec_module(module)
    return module


def test_lint_034_the_gate_entry_point_fails_on_the_injected_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junit = tmp_path / "junit.xml"
    baseline = tmp_path / "baseline.json"
    junit.write_text(_junit({_UNIT: 1.0}), encoding="utf-8")

    assert _tool().main(["record", str(junit), str(baseline)]) == 0
    assert _tool().main(["check", str(junit), str(baseline)]) == 0
    junit.write_text(_junit({_UNIT: 2.5}), encoding="utf-8")
    assert _tool().main(["check", str(junit), str(baseline)]) == 1
    assert "1 of 1 tests over budget" in capsys.readouterr().out
