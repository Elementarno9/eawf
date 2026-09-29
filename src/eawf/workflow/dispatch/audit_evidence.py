"""What a durable close auditor's criterion row may cite as its evidence.

A row is the review receipt for one criterion, and a receipt is only as
good as what it cites. A gate-running criterion is discharged by the
GateReceipt its gate left on the frozen close; a claim about a rendered
artifact is discharged by nothing else, because a source scan, a
stylesheet grep or the implementer's report cannot tell what a reader
sees. A statistic quoted in a row names the population it was drawn from,
so a reader can re-derive the denominator before trusting the rate.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from eawf.kernel.store.kinds.agent_report import CriterionVerdict

if TYPE_CHECKING:
    from eawf.workflow.dispatch.verdict import DurableAuditCriterion

#: A rate quoted in an evidence note. A percentage is the shape a statistic
#: drawn from a history store arrives in; a bare count is not a rate and
#: carries its own denominator problem only once it is divided.
_QUOTED_RATE: re.Pattern[str] = re.compile(r"\d+(?:\.\d+)?\s*%")


def require_row_evidence(row: CriterionVerdict, criterion: DurableAuditCriterion) -> None:
    """Require a row's evidence to be the kind its criterion can be discharged by.

    Args:
        row: One auditor row, already matched to *criterion*.
        criterion: The frozen criterion the row answers.

    Raises:
        ValueError: When the row cites nothing, a gate-running criterion
            cites none of its mapped GateReceipts (a rendering claim's
            other evidence is a source scan or a report, which cannot
            discharge it), or a reference quotes a rate without naming
            its population.
    """
    if not row.evidence_refs:
        raise ValueError(
            f"durable audit criterion requires evidence_refs: {criterion.criterion_id!r}"
        )
    for ref in row.evidence_refs:
        if ref.population is None and ref.note and _QUOTED_RATE.search(ref.note):
            raise ValueError(
                f"criterion {criterion.criterion_id!r} evidence quotes a rate with no named "
                f"population: set `population` to the selector and filter that produced it "
                f"for {ref.ref!r}"
            )
    if not criterion.deterministic:
        return
    mapped = set(criterion.gate_receipt_urns)
    if not mapped:
        raise ValueError(
            f"deterministic criterion has no GateReceipt binding: {criterion.criterion_id!r}"
        )
    if any(ref.kind == "store_record" and ref.ref in mapped for ref in row.evidence_refs):
        return
    if criterion.rendered_run:
        raise ValueError(
            f"rendered_run criterion {criterion.criterion_id!r} cites a source scan or "
            "report as its only evidence: cite the rendered run's mapped GateReceipt URN"
        )
    raise ValueError(
        f"deterministic criterion must cite a mapped GateReceipt URN: {criterion.criterion_id!r}"
    )
