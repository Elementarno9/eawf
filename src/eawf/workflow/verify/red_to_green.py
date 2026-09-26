"""The red-to-green check an executor report's test runs face at wave close.

An executor records each run of the tests it named on its report
(:class:`~eawf.kernel.store.kinds.agent_report.ExecutorTestRun`). Wave close
groups those runs per test and asks
:func:`~eawf.platform.lint.kind_taxonomy.red_to_green_finding` whether each
test ran red before its first green run and ended green.

A finding is advisory, except on a defect repro test: the test-first
protocol requires a repro to prove red on the pre-fix basis before close,
so a repro test without the pair blocks. The check proves only that a
red-then-green pair was recorded, not that the test stayed honest between
the two runs.

Only tests gated at the wave tier are checked. A test whose kind runs at
iter or release tier (golden, TUI, conformance, e2e, perf) is not part of
the executor's inner loop, and a golden changes through a reviewed
regeneration diff rather than a red run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from eawf.kernel.store.kinds.agent_report import ExecutorReportBody
from eawf.platform.lint.kind_taxonomy import (
    GateTier,
    RedToGreenFinding,
    RunOutcome,
    TaskTestRun,
    gate_tier_for_test_path,
    red_to_green_finding,
    tier_rank,
)

logger = logging.getLogger(__name__)

#: The name segment the test-first protocol gives a defect repro test.
REPRO_MARK = "_repro_"


@dataclass(frozen=True)
class RedToGreenCloseFinding:
    """A red-to-green finding raised at wave close.

    Attributes:
        finding: The per-test finding from the taxonomy check.
        blocking: ``True`` when the test is a defect repro, whose red run
            the protocol requires before close.
    """

    finding: RedToGreenFinding
    blocking: bool

    def render(self) -> str:
        """Return the one-line finding, tagged with its effect on close."""
        effect = "blocks close" if self.blocking else "advisory"
        return f"{self.finding.render()} ({effect})"


def _checked_at_wave_tier(test_id: str) -> bool:
    """Return whether *test_id*'s file is gated at the wave tier.

    A file outside every kind directory has no declared tier; it is run in
    the targeted inner loop, so it is checked like a wave-tier test.
    """
    tier = gate_tier_for_test_path(test_id.split("::", 1)[0])
    return tier is None or tier_rank(tier) <= tier_rank(GateTier.WAVE)


def red_to_green_close_findings(
    report: ExecutorReportBody,
) -> tuple[RedToGreenCloseFinding, ...]:
    """Return the red-to-green findings for *report*'s recorded test runs.

    Args:
        report: The executor report wave close reads.

    Returns:
        One finding per wave-tier test whose runs lack a red-then-green
        pair, in the order each test first appears. Empty when the report
        records no test runs, which is how every report written before the
        field existed reads.

    Raises:
        TypeError: *report* is not an :class:`ExecutorReportBody`.
    """
    if not isinstance(report, ExecutorReportBody):
        raise TypeError(f"expected an ExecutorReportBody, got {type(report).__name__}")
    runs_by_test: dict[str, list[TaskTestRun]] = {}
    for run in report.test_runs:
        runs_by_test.setdefault(run.test_id, []).append(
            TaskTestRun(test_id=run.test_id, outcome=RunOutcome(run.outcome), revision=run.revision)
        )
    findings: list[RedToGreenCloseFinding] = []
    for test_id, runs in runs_by_test.items():
        if not _checked_at_wave_tier(test_id):
            logger.debug(f"red_to_green_close_findings skip test={test_id!r} tier=above-wave")
            continue
        finding = red_to_green_finding(runs)
        if finding is not None:
            findings.append(RedToGreenCloseFinding(finding=finding, blocking=REPRO_MARK in test_id))
    return tuple(findings)


__all__ = [
    "REPRO_MARK",
    "RedToGreenCloseFinding",
    "red_to_green_close_findings",
]
