"""Lenses 1-4: schema/reference, source atomization, outcome coverage, DAG.

A lens is a pure function of a :class:`PlanBody`: no lock, no file, no
document. That is what lets the same four functions decide a unit test
and a live submit, and it is why a lens returns findings rather than
raising -- a caller collects every lens's verdict before deciding what a
blocking one means.

Findings are collected in :func:`run_plan_lenses`, in the fixed order
the four lenses are declared here. The order matters only for which
finding a caller surfaces first when several fire at once; every lens
still runs and every finding it produces is returned.

What each lens catches, given what :class:`PlanBody` actually holds
today:

- schema/reference: a Batch, Task, or citation addressed outside the
  Milestone's own workspace/project scope. Pydantic already enforces a
  URN's *kind*; it enforces nothing about which workspace or project the
  URN names, so a plan naming work in a foreign scope would otherwise
  validate and then fail unpredictably wherever that scope is read.
- source atomization: a declared :class:`SourceAtom` that maps to no
  criterion and carries no typed drop, a mapping or a drop that names an
  atom or criterion id the plan never declares.
- outcome coverage: a Batch the plan creates that no Task in the same
  plan targets, so it reaches no criterion and nothing it promises can
  ever be shown to be done.
- DAG: a Task whose ``depends_on`` names a Task the plan does not
  create, or a dependency cycle among the plan's own Tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from eawf.kernel.state.epoch2.plan_revision import PlanBody, PlannedTask
from eawf.kernel.state.epoch2.urns import TaskUrn


class PlanLens(StrEnum):
    """The deterministic lenses this package runs, in declaration order.

    Lenses 5-8 (ownership, criterion fidelity, role authority,
    integration/migration) run out of
    :mod:`~eawf.workflow.planning.lenses.authority`; this module keeps
    only lenses 1-4. Lenses 9-12 (policy/constitution, duplicate
    mechanism, idle-contract, semantic diff) run out of
    :mod:`~eawf.workflow.planning.lenses.governance`.
    """

    SCHEMA_REFERENCE = "schema_reference"
    SOURCE_ATOMIZATION = "source_atomization"
    OUTCOME_COVERAGE = "outcome_coverage"
    DAG = "dag"
    OWNERSHIP = "ownership"
    CRITERION_FIDELITY = "criterion_fidelity"
    ROLE_AUTHORITY = "role_authority"
    INTEGRATION_MIGRATION = "integration_migration"
    POLICY_CONSTITUTION = "policy_constitution"
    DUPLICATE_MECHANISM = "duplicate_mechanism"
    IDLE_CONTRACT = "idle_contract"
    SEMANTIC_DIFF = "semantic_diff"


class PlanFindingCode(StrEnum):
    """The stable codes a lens finding carries.

    One code per distinct condition a lens below actually detects; a
    code with nothing that emits it would be a contract no producer
    honours.
    """

    CROSS_SCOPE_MISMATCH = "plan_cross_scope_mismatch"
    SOURCE_ATOM_UNMAPPED = "plan_source_atom_unmapped"
    SOURCE_ATOM_REFERENCE_UNRESOLVED = "plan_source_atom_reference_unresolved"
    OUTCOME_BATCH_UNREACHED = "plan_outcome_batch_unreached"
    DAG_DEPENDENCY_UNRESOLVED = "plan_dag_dependency_unresolved"
    DAG_CYCLE_DETECTED = "plan_dag_cycle_detected"
    WRITE_CLAIM_OVERLAP_UNORDERED = "plan_write_claim_overlap_unordered"
    CRITERION_FIDELITY_UNRESOLVED = "plan_criterion_fidelity_unresolved"
    TASK_WRITE_CLAIM_MISSING = "plan_task_write_claim_missing"
    INTEGRATION_REPOSITORY_CYCLE_DETECTED = "plan_integration_repository_cycle_detected"
    UNAUTHORIZED_CRITERION_WAIVER = "plan_unauthorized_criterion_waiver"
    DUPLICATE_CRITERION_ID = "plan_duplicate_criterion_id"
    IDLE_CONTRACT_PRODUCER_MISSING = "plan_idle_contract_producer_missing"
    SEMANTIC_DIFF_HIDDEN_TASK_DELETION = "plan_semantic_diff_hidden_task_deletion"


#: How the runner treats a finding. Only ``"blocking"`` refuses a
#: revision today; the other three grades are reserved for lenses that
#: have not shipped a producer yet.
PlanFindingSeverity = Literal["blocking", "error", "warning", "advisory"]


@dataclass(frozen=True, slots=True)
class PlanFinding:
    """One deterministic lens's verdict on one plan body.

    Attributes:
        lens: Which lens produced this finding.
        severity: How the runner treats it.
        code: The stable code a client branches on.
        entity_refs: The plan-local entities the finding names, rendered
            as their canonical URN or id strings.
        message: The operator-facing explanation.
        remediation: One sentence saying what to do about it.
    """

    lens: PlanLens
    severity: PlanFindingSeverity
    code: PlanFindingCode
    entity_refs: tuple[str, ...]
    message: str
    remediation: str


#: The order :func:`run_plan_lenses` runs its lenses in. Fixed rather
#: than derived from a dict, so the sequence survives whatever order a
#: future lens gets registered in.
FIXED_LENS_ORDER: Final[tuple[PlanLens, ...]] = (
    PlanLens.SCHEMA_REFERENCE,
    PlanLens.SOURCE_ATOMIZATION,
    PlanLens.OUTCOME_COVERAGE,
    PlanLens.DAG,
)


def _schema_reference_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag every entity URN *body* addresses outside the Milestone's own scope.

    A URN's kind is validated at the loader; the workspace and project it
    names are not, so nothing else in the model catches a Batch, Task, or
    citation planned into a foreign slot.

    Args:
        body: The plan content.

    Returns:
        Zero findings, or one blocking finding naming every offending URN.
    """
    home = (body.milestone_urn.workspace_key, body.milestone_urn.project_key)
    offenders = tuple(
        str(ref)
        for ref in (
            *(batch.urn for batch in body.batches),
            *(task.urn for task in body.tasks),
            *(citation.finding_ref for citation in body.citations),
        )
        if (ref.workspace_key, ref.project_key) != home
    )
    if not offenders:
        return ()
    return (
        PlanFinding(
            lens=PlanLens.SCHEMA_REFERENCE,
            severity="blocking",
            code=PlanFindingCode.CROSS_SCOPE_MISMATCH,
            entity_refs=offenders,
            message=(
                "these entities are addressed outside the Milestone's own "
                f"workspace/project scope: {', '.join(offenders)}"
            ),
            remediation="Plan every Batch, Task and citation inside the Milestone's own scope.",
        ),
    )


def _source_atomization_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Require every declared source atom to map to a criterion or a typed drop.

    Args:
        body: The plan content.

    Returns:
        One finding per dangling reference or unmapped atom; empty when
        every atom is covered.
    """
    criterion_ids = frozenset(criterion.id for task in body.tasks for criterion in task.criteria)
    declared_atom_ids = frozenset(atom.atom_id for atom in body.source_atoms)
    dropped_ids = frozenset(dropped.atom_ref for dropped in body.dropped_atoms)
    findings: list[PlanFinding] = []

    dangling_drops = tuple(
        dropped.atom_ref
        for dropped in body.dropped_atoms
        if dropped.atom_ref not in declared_atom_ids
    )
    if dangling_drops:
        findings.append(
            PlanFinding(
                lens=PlanLens.SOURCE_ATOMIZATION,
                severity="blocking",
                code=PlanFindingCode.SOURCE_ATOM_REFERENCE_UNRESOLVED,
                entity_refs=dangling_drops,
                message=(
                    "dropped_atoms names an atom this plan's source_atoms never "
                    f"declares: {', '.join(dangling_drops)}"
                ),
                remediation="Drop only an atom_id the plan's source_atoms declares.",
            )
        )

    for atom in body.source_atoms:
        dangling_criteria = tuple(
            ref for ref in atom.mapped_criterion_ids if ref not in criterion_ids
        )
        if dangling_criteria:
            findings.append(
                PlanFinding(
                    lens=PlanLens.SOURCE_ATOMIZATION,
                    severity="blocking",
                    code=PlanFindingCode.SOURCE_ATOM_REFERENCE_UNRESOLVED,
                    entity_refs=(atom.atom_id, *dangling_criteria),
                    message=(
                        f"source atom {atom.atom_id!r} maps to a criterion id this plan "
                        f"never declares: {', '.join(dangling_criteria)}"
                    ),
                    remediation=(
                        "Map the atom only to a criterion id one of this plan's Tasks declares."
                    ),
                )
            )
            continue
        if not atom.mapped_criterion_ids and atom.atom_id not in dropped_ids:
            findings.append(
                PlanFinding(
                    lens=PlanLens.SOURCE_ATOMIZATION,
                    severity="blocking",
                    code=PlanFindingCode.SOURCE_ATOM_UNMAPPED,
                    entity_refs=(atom.atom_id,),
                    message=(
                        f"source atom {atom.atom_id!r} maps to no criterion and carries "
                        "no typed drop"
                    ),
                    remediation=(
                        "Map the atom to a criterion, or record it in dropped_atoms with a reason."
                    ),
                )
            )
    return tuple(findings)


def _outcome_coverage_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Require every planned Batch to be reached by at least one Task.

    Args:
        body: The plan content.

    Returns:
        Zero findings, or one blocking finding naming every Batch no
        Task in this plan targets.
    """
    owning_batches = frozenset(task.batch_ref for task in body.tasks)
    unreached = tuple(str(batch.urn) for batch in body.batches if batch.urn not in owning_batches)
    if not unreached:
        return ()
    return (
        PlanFinding(
            lens=PlanLens.OUTCOME_COVERAGE,
            severity="blocking",
            code=PlanFindingCode.OUTCOME_BATCH_UNREACHED,
            entity_refs=unreached,
            message=f"these Batches own no Task and reach no criterion: {', '.join(unreached)}",
            remediation="Plan at least one Task into every Batch, or drop the Batch from the plan.",
        ),
    )


def _find_cycle(tasks: tuple[PlannedTask, ...]) -> tuple[TaskUrn, ...] | None:
    """Return one cycle among *tasks*' resolved dependency edges, via Kahn's algorithm.

    Every ``depends_on`` entry is assumed already resolved to a Task in
    *tasks*; the caller checks that separately, because a dangling edge
    and a cycle are different findings.

    Args:
        tasks: Every Task the plan creates.

    Returns:
        The cyclic Tasks' URNs, in stable string order, or ``None`` when
        the dependency graph is acyclic.
    """
    task_urns = tuple(task.urn for task in tasks)
    depends_on = {task.urn: frozenset(task.depends_on) for task in tasks}
    in_degree = {urn: len(depends_on[urn]) for urn in task_urns}
    dependents: dict[TaskUrn, list[TaskUrn]] = {urn: [] for urn in task_urns}
    for urn, deps in depends_on.items():
        for dep in deps:
            dependents[dep].append(urn)

    ready = sorted((urn for urn in task_urns if in_degree[urn] == 0), key=str)
    resolved: set[TaskUrn] = set()
    while ready:
        node = ready.pop(0)
        resolved.add(node)
        for dependent in sorted(dependents[node], key=str):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)

    if len(resolved) == len(task_urns):
        return None
    return tuple(sorted((urn for urn in task_urns if urn not in resolved), key=str))


def _dag_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Require the plan's Task dependency graph to resolve and stay acyclic.

    Args:
        body: The plan content.

    Returns:
        Zero findings, one blocking finding for a dangling dependency, or
        one blocking finding naming a cycle. A dangling edge is checked
        first and short-circuits the cycle search, which assumes every
        edge already resolves.
    """
    declared = frozenset(task.urn for task in body.tasks)
    dangling = tuple(
        (str(task.urn), str(dep))
        for task in body.tasks
        for dep in task.depends_on
        if dep not in declared
    )
    if dangling:
        entity_refs = tuple(sorted({ref for pair in dangling for ref in pair}))
        return (
            PlanFinding(
                lens=PlanLens.DAG,
                severity="blocking",
                code=PlanFindingCode.DAG_DEPENDENCY_UNRESOLVED,
                entity_refs=entity_refs,
                message=(
                    "these Tasks depend on a Task this plan does not create: "
                    + ", ".join(f"{task} -> {dep}" for task, dep in dangling)
                ),
                remediation=(
                    "Depend only on a Task this plan creates, or split the dependency "
                    "into its own plan."
                ),
            ),
        )
    cycle = _find_cycle(body.tasks)
    if cycle is None:
        return ()
    rendered = tuple(str(ref) for ref in cycle)
    return (
        PlanFinding(
            lens=PlanLens.DAG,
            severity="blocking",
            code=PlanFindingCode.DAG_CYCLE_DETECTED,
            entity_refs=rendered,
            message=f"these Tasks depend on each other in a cycle: {', '.join(rendered)}",
            remediation="Break the cycle by removing or reordering one dependency edge.",
        ),
    )


def run_plan_lenses(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Run every registered lens over *body*, in :data:`FIXED_LENS_ORDER`.

    Args:
        body: The plan content to validate.

    Returns:
        Every finding any lens produced, in lens order and then
        declaration order within a lens. An empty tuple means no lens
        objects to the plan.
    """
    findings: list[PlanFinding] = []
    for lens in FIXED_LENS_ORDER:
        findings.extend(_LENS_FUNCS[lens](body))
    return tuple(findings)


def blocking_findings(findings: tuple[PlanFinding, ...]) -> tuple[PlanFinding, ...]:
    """Return the subset of *findings* that refuses a revision.

    Args:
        findings: Every finding a lens run produced.

    Returns:
        Only the ``"blocking"`` severity findings, in their given order.
    """
    return tuple(finding for finding in findings if finding.severity == "blocking")


_LENS_FUNCS: Final[dict[PlanLens, Callable[[PlanBody], tuple[PlanFinding, ...]]]] = {
    PlanLens.SCHEMA_REFERENCE: _schema_reference_lens,
    PlanLens.SOURCE_ATOMIZATION: _source_atomization_lens,
    PlanLens.OUTCOME_COVERAGE: _outcome_coverage_lens,
    PlanLens.DAG: _dag_lens,
}


__all__ = [
    "FIXED_LENS_ORDER",
    "PlanFinding",
    "PlanFindingCode",
    "PlanFindingSeverity",
    "PlanLens",
    "blocking_findings",
    "run_plan_lenses",
]
