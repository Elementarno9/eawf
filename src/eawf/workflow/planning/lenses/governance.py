"""Lenses 9-12: policy/constitution, duplicate mechanism, idle-contract, semantic diff.

These run after :mod:`~eawf.workflow.planning.lenses.authority`'s lenses
5-8, in the fixed order the package's ``__init__`` composes them in. The
first three are, like every lens before them, a pure function of a
:class:`~eawf.kernel.state.epoch2.plan_revision.PlanBody`: no lock, no
file, no document. The fourth -- semantic diff -- is a pure function of
*two* plan bodies, because a diff needs something to diff against; a plan
with no parent has nothing to compare and earns no finding from it.

What each lens catches, given what :class:`PlanBody` and
:class:`~eawf.kernel.spec.common.CriterionSpec` actually hold today:

- policy/constitution: a ``required`` criterion that also carries a
  ``waiver_reason``. A mandatory criterion's gate is not the one a plan
  is allowed to skip; a non-mandatory criterion may waive freely, which
  is why an unrequired criterion with a waiver earns no finding here.
  Checking a cited Decision's own ACTIVE/SUPERSEDED status is out of
  scope: :attr:`CriterionSpec.accepted_risk_decision_ref` is deliberately
  an opaque bounded string rather than an addressable typed reference
  (see its docstring), so a pure function of the plan body alone has
  nothing resolvable to check that status against.
- duplicate mechanism: a criterion id declared more than once within one
  Task's own ``criteria`` list. One id naming two different criterion
  rows on the same Task is the same defect the design brief calls "two
  canonical owners of one persisted truth"; two different Tasks each
  naming their own criterion the same id are not that defect, because
  the approval gate's own identity for a criterion is the Task/id pair,
  not the id alone -- a reused id across Tasks names two different
  mechanisms, not one mechanism with two owners.
- idle-contract: a :class:`~eawf.kernel.state.epoch2.plan_revision.SurfaceProducerRef`
  whose ``producer_ref`` is unset or names a Task this plan does not
  itself create. Consumer-side idle-contract enforcement (a surface with
  a producer but nothing that ever reads it) is not yet checked: nothing
  in :class:`PlanBody` ties a criterion back to the surface it exercises,
  so there is no sound reference to check that against today.
- semantic diff: a parent-revision Task, addressed by its stable URN,
  that this revision's own Tasks no longer create and that
  ``dropped_tasks`` does not name. The fuller diff the design brief
  describes -- moves, edits, terminal rewrites, migration impact -- needs
  a notion of Task identity beyond "same URN, present or absent" that
  :class:`PlanBody` does not carry yet; this lens catches the one
  category it can prove today: a deletion the repair never enumerated.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.lenses.structure import PlanFinding, PlanFindingCode, PlanLens


def _policy_constitution_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag a mandatory criterion that also carries a waiver.

    Args:
        body: The plan content.

    Returns:
        One finding per required criterion carrying a ``waiver_reason``;
        empty when no required criterion does.
    """
    findings: list[PlanFinding] = []
    for task in body.tasks:
        for criterion in task.criteria:
            if not (criterion.required and criterion.waiver_reason is not None):
                continue
            findings.append(
                PlanFinding(
                    lens=PlanLens.POLICY_CONSTITUTION,
                    severity="blocking",
                    code=PlanFindingCode.UNAUTHORIZED_CRITERION_WAIVER,
                    entity_refs=(str(task.urn), criterion.id),
                    message=(
                        f"criterion {criterion.id!r} on {task.urn} is required and carries "
                        "a waiver_reason, and a mandatory criterion's gate may not be waived"
                    ),
                    remediation=(
                        "Drop the waiver_reason, or grade the criterion optional "
                        "(required=False) if its gate is not actually mandatory."
                    ),
                )
            )
    return tuple(findings)


def _duplicate_mechanism_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag a criterion id two or more of one Task's own criterion rows share.

    Criterion ids are scoped per Task, matching the approval gate's own
    task-scoped identity (``f"{task.urn}/{criterion.id}"`` in
    :func:`~eawf.workflow.planning.revision.ungrounded_approval_criteria`):
    two different Tasks each naming their own ``CR-01`` name two different
    mechanisms, not one id with two owners, so only a repeat *within* one
    Task's own ``criteria`` list is the defect this lens catches.

    Args:
        body: The plan content.

    Returns:
        One finding per Task with a repeated criterion id, ordered by
        Task URN then criterion id so two plans differing only in task or
        criterion list order report identically; empty when every Task's
        own criterion ids are unique within it.
    """
    findings: list[PlanFinding] = []
    for task in sorted(body.tasks, key=lambda task: str(task.urn)):
        counts: dict[str, int] = {}
        for criterion in task.criteria:
            counts[criterion.id] = counts.get(criterion.id, 0) + 1
        for criterion_id in sorted(counts):
            count = counts[criterion_id]
            if count < 2:
                continue
            findings.append(
                PlanFinding(
                    lens=PlanLens.DUPLICATE_MECHANISM,
                    severity="blocking",
                    code=PlanFindingCode.DUPLICATE_CRITERION_ID,
                    entity_refs=(criterion_id, str(task.urn)),
                    message=(
                        f"criterion id {criterion_id!r} is declared {count} times on "
                        f"{task.urn}, which gives one identity two canonical rows "
                        "inside the same Task"
                    ),
                    remediation=(
                        "Give each criterion its own id within the Task; a shared id "
                        "names two truths as one."
                    ),
                )
            )
    return tuple(findings)


def _idle_contract_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag a new surface this plan names with no Task assigned to produce it.

    Args:
        body: The plan content.

    Returns:
        Zero findings, or one blocking finding naming every declared
        surface whose producer is unset or resolves to no Task this plan
        creates.
    """
    declared_tasks = frozenset(task.urn for task in body.tasks)
    offenders = tuple(
        sorted(
            surface.surface_id
            for surface in body.new_surfaces
            if surface.producer_ref is None or surface.producer_ref not in declared_tasks
        )
    )
    if not offenders:
        return ()
    return (
        PlanFinding(
            lens=PlanLens.IDLE_CONTRACT,
            severity="blocking",
            code=PlanFindingCode.IDLE_CONTRACT_PRODUCER_MISSING,
            entity_refs=offenders,
            message=(
                "these new surfaces name no Task this plan creates as their producer: "
                f"{', '.join(offenders)}"
            ),
            remediation=(
                "Name a producer_ref this plan itself creates for every declared surface, "
                "or drop the surface until a Task is planned to build it."
            ),
        ),
    )


def _semantic_diff_lens(body: PlanBody, parent: PlanBody | None) -> tuple[PlanFinding, ...]:
    """Flag a parent Task this revision drops without a typed drop.

    Args:
        body: The plan content.
        parent: The revision *body* repairs, or ``None`` for a plan with
            no parent to diff against.

    Returns:
        Zero findings when *parent* is ``None`` or every parent Task
        survives or is enumerated in ``dropped_tasks``; otherwise one
        blocking finding naming every hidden deletion.
    """
    if parent is None:
        return ()
    declared = frozenset(task.urn for task in body.tasks)
    dropped = frozenset(dropped.task_ref for dropped in body.dropped_tasks)
    hidden = tuple(
        sorted(
            str(task.urn)
            for task in parent.tasks
            if task.urn not in declared and task.urn not in dropped
        )
    )
    if not hidden:
        return ()
    return (
        PlanFinding(
            lens=PlanLens.SEMANTIC_DIFF,
            severity="blocking",
            code=PlanFindingCode.SEMANTIC_DIFF_HIDDEN_TASK_DELETION,
            entity_refs=hidden,
            message=(
                "these parent-revision Tasks are absent from this revision with no typed "
                f"drop naming why: {', '.join(hidden)}"
            ),
            remediation=(
                "Record every dropped parent Task in dropped_tasks with a reason, or keep it."
            ),
        ),
    )


#: Lenses 9-11, in the order they run after authority.py's lenses 5-8.
#: Lens 12 (semantic diff) is not in this tuple: it alone takes a second
#: plan body, so :func:`run_governance_lenses` runs it separately rather
#: than through the uniform single-body dispatch the other eleven lenses
#: share.
GOVERNANCE_LENS_ORDER: Final[tuple[PlanLens, ...]] = (
    PlanLens.POLICY_CONSTITUTION,
    PlanLens.DUPLICATE_MECHANISM,
    PlanLens.IDLE_CONTRACT,
    PlanLens.SEMANTIC_DIFF,
)

_GOVERNANCE_LENS_FUNCS: Final[dict[PlanLens, Callable[[PlanBody], tuple[PlanFinding, ...]]]] = {
    PlanLens.POLICY_CONSTITUTION: _policy_constitution_lens,
    PlanLens.DUPLICATE_MECHANISM: _duplicate_mechanism_lens,
    PlanLens.IDLE_CONTRACT: _idle_contract_lens,
}


def run_governance_lenses(
    body: PlanBody, *, parent: PlanBody | None = None
) -> tuple[PlanFinding, ...]:
    """Run lenses 9-12, in :data:`GOVERNANCE_LENS_ORDER`, over *body*.

    Args:
        body: The plan content to validate.
        parent: The revision *body* repairs, for the semantic-diff lens;
            ``None`` for a plan with no parent.

    Returns:
        Every finding any of lenses 9-12 produced, in lens order.
    """
    findings: list[PlanFinding] = []
    for lens in (
        PlanLens.POLICY_CONSTITUTION,
        PlanLens.DUPLICATE_MECHANISM,
        PlanLens.IDLE_CONTRACT,
    ):
        findings.extend(_GOVERNANCE_LENS_FUNCS[lens](body))
    findings.extend(_semantic_diff_lens(body, parent))
    return tuple(findings)


__all__ = [
    "GOVERNANCE_LENS_ORDER",
    "run_governance_lenses",
]
