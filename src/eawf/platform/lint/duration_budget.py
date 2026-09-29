"""The wall-time budget gate: a test got slower, or a fast test is too slow.

Both suites are green, so the defect this gate exists for is the whale: a
test that quietly grew from milliseconds to seconds and now dominates the
run nobody waits for on purpose. A green suite reports nothing about it.
The gate diffs each measured duration against a committed baseline and
reports two things:

- a **regression** -- a test now slower than twice its baseline, where
  the baseline of a test recorded below :data:`REGRESSION_FLOOR_SECONDS`
  (or not recorded at all) is read as the floor, so jitter on a
  millisecond test is never a finding but a new one-second test is;
- a **ceiling breach** -- a test of a fast-tier kind whose single run
  exceeds its tier's whole wall-time budget, which no baseline can excuse.

The baseline keeps only the tests at or above the floor, because every
test below it is judged against the floor anyway. Durations are read from
the JUnit report pytest writes, keyed ``<classname>::<name>`` so the key
a baseline records and the key a later run measures are the same string.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.platform.lint.kind_taxonomy import (
    TIER_RUNTIME_BUDGET_SECONDS,
    GateTier,
    gate_tier_for_test_path,
)

logger = logging.getLogger(__name__)

#: How many times its baseline a test may take before it is a regression.
REGRESSION_RATIO: Final[float] = 2.0

#: The baseline a test below this duration is judged against, in seconds.
REGRESSION_FLOOR_SECONDS: Final[float] = 0.5


class DurationBaseline(BaseModel):
    """The committed per-test durations a later run is diffed against.

    Attributes:
        revision: The commit the recorded run measured.
        durations: Seconds per test key, for every test at or above the
            regression floor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: Annotated[str, Field(min_length=1)]
    durations: dict[str, Annotated[float, Field(ge=0.0)]]


@dataclass(frozen=True, slots=True)
class BudgetFinding:
    """One test the budget gate reports.

    Attributes:
        test: The test key, ``<classname>::<name>``.
        reason: Which budget it broke and by how much.
    """

    test: str
    reason: str

    def render(self) -> str:
        """Return a one-line ``test: reason`` diagnostic."""
        return f"{self.test}: {self.reason}"


def junit_durations(xml_text: str) -> dict[str, float]:
    """Return the duration of every test case a JUnit report records.

    Args:
        xml_text: A JUnit XML report as pytest's ``--junitxml`` writes it.

    Returns:
        Seconds per ``<classname>::<name>`` key. A key repeated by a rerun
        keeps its slowest run.

    Raises:
        xml.etree.ElementTree.ParseError: *xml_text* is not XML.
        ValueError: A test case's ``time`` is not a number.
    """
    durations: dict[str, float] = {}
    for case in ET.fromstring(xml_text).iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        durations[key] = max(durations.get(key, 0.0), float(case.get("time", "0")))
    return durations


def record_baseline(durations: Mapping[str, float], *, revision: str) -> DurationBaseline:
    """Return the baseline a measured run records.

    Args:
        durations: Seconds per test key from one measured run.
        revision: The commit that run measured.

    Returns:
        A baseline keeping every test at or above the regression floor.

    Raises:
        pydantic.ValidationError: *revision* is blank or a duration is
            negative.
    """
    return DurationBaseline(
        revision=revision,
        durations={
            key: round(seconds, 3)
            for key, seconds in sorted(durations.items())
            if seconds >= REGRESSION_FLOOR_SECONDS
        },
    )


def load_baseline(path: Path) -> DurationBaseline:
    """Return the committed baseline at *path*, validated.

    Raises:
        FileNotFoundError: *path* does not exist.
        pydantic.ValidationError: The file is not a baseline.
    """
    return DurationBaseline.model_validate_json(path.read_text(encoding="utf-8"))


def _test_path(key: str) -> str:
    """Return a path under the test's kind directory, from its JUnit classname.

    Only the leading ``tests/<kind>/`` segments decide the tier, so a
    classname naming a test class resolves correctly even though its last
    segment is a class rather than a module.
    """
    classname = key.split("::", 1)[0]
    return f"{classname.replace('.', '/')}.py"


def budget_findings(
    durations: Mapping[str, float], baseline: DurationBaseline
) -> list[BudgetFinding]:
    """Return every regression and ceiling breach in one measured run.

    Args:
        durations: Seconds per test key from the run under judgment.
        baseline: The committed baseline it is diffed against.

    Returns:
        Findings in key order; a test that is both a regression and a
        ceiling breach is reported as the breach.
    """
    ceiling = TIER_RUNTIME_BUDGET_SECONDS[GateTier.WAVE]
    findings: list[BudgetFinding] = []
    for key, seconds in sorted(durations.items()):
        if gate_tier_for_test_path(_test_path(key)) is GateTier.WAVE and seconds > ceiling:
            findings.append(
                BudgetFinding(key, f"took {seconds:.3f}s, over the {ceiling}s fast-tier ceiling")
            )
            continue
        allowed = max(baseline.durations.get(key, 0.0), REGRESSION_FLOOR_SECONDS)
        if seconds > allowed * REGRESSION_RATIO:
            findings.append(
                BudgetFinding(
                    key,
                    f"took {seconds:.3f}s against a {allowed:.3f}s baseline "
                    f"(over {REGRESSION_RATIO:g}x)",
                )
            )
    logger.info(f"budget_findings tests={len(durations)} findings={len(findings)}")
    return findings


__all__ = [
    "REGRESSION_FLOOR_SECONDS",
    "REGRESSION_RATIO",
    "BudgetFinding",
    "DurationBaseline",
    "budget_findings",
    "junit_durations",
    "load_baseline",
    "record_baseline",
]
