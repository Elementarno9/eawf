"""Deterministic plan lenses: pure, fixed-order, blocking-or-not.

:mod:`~eawf.workflow.planning.lenses.structure` holds lenses 1-4
(schema/reference, source atomization, outcome coverage, DAG);
:mod:`~eawf.workflow.planning.lenses.authority` holds lenses 5-8
(ownership, criterion fidelity, role authority, integration/migration);
:mod:`~eawf.workflow.planning.lenses.governance` holds lenses 9-12
(policy/constitution, duplicate mechanism, idle-contract, semantic
diff). This module composes the three into one fixed order rather than
having any submodule import another. This package's public surface --
:class:`PlanFinding`, :class:`PlanLens`, and :func:`run_plan_lenses` --
is what a caller needs regardless of how many lenses currently register.
"""

from __future__ import annotations

from typing import Final

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.lenses import structure
from eawf.workflow.planning.lenses.authority import AUTHORITY_LENS_ORDER, run_authority_lenses
from eawf.workflow.planning.lenses.governance import GOVERNANCE_LENS_ORDER, run_governance_lenses
from eawf.workflow.planning.lenses.structure import (
    PlanFinding,
    PlanFindingCode,
    PlanFindingSeverity,
    PlanLens,
)

#: Every lens this package runs, lenses 1-4 then lenses 5-8 then lenses 9-12.
FIXED_LENS_ORDER: Final[tuple[PlanLens, ...]] = (
    structure.FIXED_LENS_ORDER + AUTHORITY_LENS_ORDER + GOVERNANCE_LENS_ORDER
)


def run_plan_lenses(body: PlanBody, *, parent: PlanBody | None = None) -> tuple[PlanFinding, ...]:
    """Run every registered lens over *body*, in :data:`FIXED_LENS_ORDER`.

    Args:
        body: The plan content to validate.
        parent: The revision *body* repairs, when it repairs one. Read
            only by the semantic-diff lens; every other lens ignores it.

    Returns:
        Every finding any lens produced, lenses 1-4's own findings first
        (in their declared order), then lenses 5-8's, then lenses 9-12's.
        An empty tuple means no lens objects to the plan.
    """
    return (
        structure.run_plan_lenses(body)
        + run_authority_lenses(body)
        + run_governance_lenses(body, parent=parent)
    )


def blocking_findings(findings: tuple[PlanFinding, ...]) -> tuple[PlanFinding, ...]:
    """Return the subset of *findings* that refuses a revision.

    Args:
        findings: Every finding a lens run produced.

    Returns:
        Only the ``"blocking"`` severity findings, in their given order.
    """
    return structure.blocking_findings(findings)


__all__ = [
    "FIXED_LENS_ORDER",
    "PlanFinding",
    "PlanFindingCode",
    "PlanFindingSeverity",
    "PlanLens",
    "blocking_findings",
    "run_plan_lenses",
]
