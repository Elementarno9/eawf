"""The red-to-green check a native Task's repro gates face at completion.

The test-first protocol requires a defect repro test to prove red on the
pre-fix basis. A test is a repro only when its function name has the form
:data:`~eawf.platform.lint.kind_taxonomy.REPRO_TEST_NAME` declares; the
executor prompt renders that form from the same constant, so the rule an
executor reads and the rule completion applies cannot drift apart.

The pair is read off the proof receipts the daemon filed for the Task: a
gate that runs a defect repro test is required-tier, and the Task completes
only once that gate's filed proofs hold a failing run before the passing
one, as :func:`~eawf.platform.lint.kind_taxonomy.red_to_green_finding`
judges it. The receipts are the existing gate machinery, so the rule adds
no field to any record. The check proves only that a red-then-green pair
was recorded, not that the test stayed honest between the two runs.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import GateReceiptResult
from eawf.platform.lint.kind_taxonomy import (
    REPRO_TEST_NAME,
    RedToGreenFinding,
    RunOutcome,
    TaskTestRun,
    red_to_green_finding,
)

logger = logging.getLogger(__name__)


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
    "is_required_tier",
    "unpaired_required_gates",
]
