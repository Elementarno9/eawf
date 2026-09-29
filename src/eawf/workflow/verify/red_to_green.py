"""The red-to-green check an executor report's test runs face at wave close.

An executor records each run of the tests it named on its report
(:class:`~eawf.kernel.store.kinds.agent_report.ExecutorTestRun`). Wave close
groups those runs per test and asks
:func:`~eawf.platform.lint.kind_taxonomy.red_to_green_finding` whether each
test ran red before its first green run and ended green.

A finding is advisory, except on a defect repro test: the test-first
protocol requires a repro to prove red on the pre-fix basis before close,
so a repro test without the pair blocks. A test is a repro only when its
function name has the form
:data:`~eawf.platform.lint.kind_taxonomy.REPRO_TEST_NAME` declares; the
executor prompt renders that form from the same constant, so the rule an
executor reads and the rule close applies cannot drift apart. The check
proves only that a red-then-green pair was recorded, not that the test
stayed honest between the two runs.

Only tests gated at the wave tier are checked. A test whose kind runs at
iter or release tier (golden, TUI, conformance, e2e, perf) is not part of
the executor's inner loop, and a golden changes through a reviewed
regeneration diff rather than a red run.

A native Task faces the same pair at completion, read off the proof
receipts the daemon filed for it rather than off a report: a gate that
runs a defect repro test is required-tier, and the Task completes only
once that gate's filed proofs hold a failing run before the passing one.
The receipts are the existing gate machinery, so the rule adds no field
to any record.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.store.kinds.agent_report import ExecutorReportBody
from eawf.platform.lint.kind_taxonomy import (
    REPRO_TEST_NAME,
    GateTier,
    RedToGreenFinding,
    RunOutcome,
    TaskTestRun,
    gate_tier_for_test_path,
    red_to_green_finding,
    tier_rank,
)

logger = logging.getLogger(__name__)


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
            findings.append(
                RedToGreenCloseFinding(finding=finding, blocking=REPRO_TEST_NAME.matches(test_id))
            )
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class GateRun:
    """One filed run of one gate, in the order the ledger holds it.

    Attributes:
        gate_id: The gate that ran.
        result: What the run recorded.
    """

    gate_id: str
    result: GateReceiptResult


#: The receipt results that are a red or a green run of the test itself.
#: A blocked, errored, timed-out or cancelled run never reached a verdict,
#: so it is neither half of the pair.
_RUN_OUTCOMES: dict[GateReceiptResult, RunOutcome] = {
    GateReceiptResult.FAIL: RunOutcome.RED,
    GateReceiptResult.PASS: RunOutcome.GREEN,
}


def is_required_tier(gate: GateSpec) -> bool:
    """Return whether *gate* runs a defect repro test.

    Args:
        gate: A gate one of a Task's criteria is proved by.

    Returns:
        ``True`` when an element of the gate's ``argv`` names a test
        function in the repro form, as a node id or a bare name.
    """
    argv = gate.args.get("argv")
    return isinstance(argv, list) and any(
        isinstance(arg, str) and REPRO_TEST_NAME.matches(arg) for arg in argv
    )


def unpaired_required_gates(
    gates: Sequence[GateSpec], runs: Sequence[GateRun]
) -> tuple[RedToGreenFinding, ...]:
    """Return a finding per required-tier gate whose runs lack a red-then-green pair.

    Args:
        gates: The gates the Task's criteria are proved by.
        runs: Every run filed for the Task, oldest first.

    Returns:
        One finding per required-tier gate, in *gates* order, whose runs
        hold no failing run before the first passing one or end red.
        Empty when no gate is required-tier.
    """
    findings: list[RedToGreenFinding] = []
    for gate in gates:
        if not is_required_tier(gate):
            continue
        gate_runs = [
            TaskTestRun(test_id=gate.id, outcome=_RUN_OUTCOMES[run.result], revision=f"run {index}")
            for index, run in enumerate(runs, start=1)
            if run.gate_id == gate.id and run.result in _RUN_OUTCOMES
        ]
        finding = red_to_green_finding(gate_runs)
        if finding is not None:
            findings.append(
                RedToGreenFinding(test_id=gate.id, reason=finding.reason)
                if not finding.test_id
                else finding
            )
    return tuple(findings)


__all__ = [
    "GateRun",
    "RedToGreenCloseFinding",
    "is_required_tier",
    "red_to_green_close_findings",
    "unpaired_required_gates",
]
