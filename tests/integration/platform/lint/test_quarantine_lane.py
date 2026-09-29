"""LINT-034 (quarantine half): the quarantine lane reports without blocking.

A scratch suite runs under the repository's own collection hooks from
``tests/conftest.py``, with the registry pointed at a scratch file: a
quarantined failing test leaves the blocking run green, and the lane that
runs it reports the failure and exits clean. The registry refuses an
entry with no backlog row or no fix date, so a quarantine cannot be
entered for free.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.platform.lint.flake_quarantine import (
    LaneReport,
    QuarantineRegistry,
    load_quarantine,
    partition,
)

_REPO = Path(__file__).resolve().parents[4]
_FLAKY = "test_lane.py::test_flaky"

_CONFTEST = """\
from pathlib import Path

import tests.conftest as suite

suite._QUARANTINE_REGISTRY = Path(__file__).parent / "quarantine.json"

from tests.conftest import pytest_addoption, pytest_configure  # noqa: E402
"""

_TESTS = """\
import pytest


def test_steady():
    assert True


def test_flaky():
    assert False, "flaked"


@pytest.mark.parametrize("case", [1, 2])
def test_cases(case):
    assert case
"""


def _entry(test_id: str = _FLAKY, **overrides: object) -> dict[str, object]:
    return {
        "test_id": test_id,
        "backlog_ref": "EAWF-0170",
        "fix_by": "2026-10-15",
        "reason": "races the watcher thread",
    } | overrides


def _suite(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    (tmp_path / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (tmp_path / "test_lane.py").write_text(_TESTS, encoding="utf-8")
    (tmp_path / "quarantine.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return tmp_path


def _pytest(suite: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"PYTHONPATH": str(_REPO)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(suite), *args],
        cwd=suite,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_lint_034_a_failing_test_outside_quarantine_reds_the_blocking_run(tmp_path: Path) -> None:
    """The control: without the entry the flake blocks, so the lane is what unblocks it."""
    result = _pytest(_suite(tmp_path, []))

    assert result.returncode == 1, result.stdout


def test_lint_034_a_quarantined_test_leaves_the_blocking_run(tmp_path: Path) -> None:
    result = _pytest(_suite(tmp_path, [_entry()]))

    assert result.returncode == 0, result.stdout
    assert "1 deselected" in result.stdout


def test_lint_034_the_quarantine_lane_reports_without_blocking(tmp_path: Path) -> None:
    """Gate fire: the quarantined test fails in its lane, which reports it and exits 0."""
    result = _pytest(_suite(tmp_path, [_entry()]), "--quarantine-lane")

    assert result.returncode == 0, result.stdout
    assert f"quarantine FAILED: {_FLAKY}" in result.stdout
    assert "1 of 1 quarantined run(s) failed (flake rate 100%)" in result.stdout
    assert "3 deselected" in result.stdout


def test_lint_034_a_parametrized_entry_covers_every_case(tmp_path: Path) -> None:
    result = _pytest(
        _suite(tmp_path, [_entry("test_lane.py::test_cases"), _entry()]), "--quarantine-lane"
    )

    assert result.returncode == 0, result.stdout
    assert "quarantine passed: test_lane.py::test_cases[1]" in result.stdout
    assert "1 of 3 quarantined run(s) failed" in result.stdout


def test_lint_034_an_entry_without_a_backlog_row_fails_collection(tmp_path: Path) -> None:
    entry = _entry()
    del entry["backlog_ref"]

    result = _pytest(_suite(tmp_path, [entry]))

    assert result.returncode != 0
    assert "backlog_ref" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "entry",
    [
        _entry(backlog_ref="B152"),
        _entry(fix_by="soon"),
        _entry(test_id="test_lane.py"),
        _entry(reason=""),
        _entry(owner="someone"),
    ],
)
def test_lint_034_the_registry_refuses_an_entry_it_cannot_hold_to_account(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_quarantine(path)


def test_lint_034_the_registry_refuses_a_test_quarantined_twice() -> None:
    with pytest.raises(ValidationError, match="quarantined twice"):
        QuarantineRegistry.model_validate({"entries": [_entry(), _entry()]})


def test_lint_034_partition_keeps_collection_order() -> None:
    registry = QuarantineRegistry.model_validate({"entries": [_entry()]})

    blocking, quarantined = partition(["a.py::t", _FLAKY, "b.py::t"], registry)

    assert blocking == ["a.py::t", "b.py::t"]
    assert quarantined == [_FLAKY]


def test_lint_034_an_empty_lane_reports_a_zero_flake_rate() -> None:
    report = LaneReport(outcomes={})

    assert report.flake_rate == 0.0
    assert report.render() == [
        "quarantine lane: 0 of 0 quarantined run(s) failed (flake rate 0%); "
        "the lane reports and never blocks"
    ]


def test_lint_034_the_committed_registry_validates() -> None:
    registry = load_quarantine(_REPO / "tests" / "quarantine.json")

    assert all(isinstance(entry.fix_by, date) for entry in registry.entries)
