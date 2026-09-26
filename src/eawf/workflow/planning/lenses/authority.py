"""Lenses 5-8: ownership, criterion fidelity, role authority, integration/migration.

These run after :mod:`~eawf.workflow.planning.lenses.structure`'s lenses
1-4, in the fixed order the package's ``__init__`` composes them in. Each
lens here is, like its predecessors, a pure function of a
:class:`~eawf.kernel.state.epoch2.plan_revision.PlanBody`: no lock, no
file, no document.

What each lens catches, given what :class:`PlanBody` and
:class:`PlannedTask` actually hold today:

- ownership: two Tasks whose ``write_claims`` overlap with no
  ``depends_on`` edge, direct or transitive, ordering one before the
  other. The check runs across the whole plan rather than per-Batch, so
  two Tasks in different Batches that both touch the same path are
  caught the same way two Tasks in one Batch are.
- criterion fidelity: a criterion whose response clause cannot resolve an
  oracle tier -- a ``JUDGED`` clause with no ``jury_reason``, a
  ``forall`` claim proved somewhere other than the hypothesis locus, or a
  ``gate_ref`` naming a gate kind nothing recognises.
- role authority: a Task whose declared ``run_purpose`` mutates a
  repository (:data:`~eawf.kernel.state.epoch2.run.MUTATING_PURPOSES`)
  but names no ``write_claims``. A task-scoped Run is the only
  :class:`~eawf.kernel.state.epoch2.run.RunScope` variant permitted to
  write, and it refuses construction with a mutating purpose and an empty
  write set; this lens raises the same refusal at plan time, before a
  worker ever tries to compile a Run for it. A Task with no declared
  ``run_purpose`` makes no claim this lens can check, so it earns no
  finding either way.
- integration/migration: a Task whose ``depends_on`` crosses from one
  Batch's repository into another's. Every Batch names exactly one
  repository, so contracting each Task down to that one repository turns
  the Task dependency graph into a repository graph; a cycle in that
  graph means two repositories each need the other to land first, which
  no rollback sequence can satisfy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from eawf.kernel.spec.common import assign_oracle_tier
from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES
from eawf.kernel.state.epoch2.urns import RepositoryUrn, TaskUrn
from eawf.workflow.planning.lenses.structure import PlanFinding, PlanFindingCode, PlanLens


def _depends_transitively(
    start: TaskUrn, target: TaskUrn, edges: dict[TaskUrn, frozenset[TaskUrn]]
) -> bool:
    """Return whether *start* is ordered after *target* via ``depends_on``.

    A direct or transitive ``depends_on`` edge from *start* to *target*
    means *start* only runs once *target*'s work has integrated, so the
    two may safely share a write claim. Breadth-first rather than a
    cached closure: a plan's Task count is small and this runs once per
    candidate pair.

    Args:
        start: The Task whose dependency chain is walked.
        target: The Task being searched for.
        edges: Every Task's own ``depends_on`` set, keyed by its URN.

    Returns:
        ``True`` when *target* is reachable from *start*.
    """
    seen: set[TaskUrn] = set()
    queue = list(edges.get(start, ()))
    while queue:
        node = queue.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        queue.extend(edges.get(node, ()))
    return False


def _ownership_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag two Tasks that claim an overlapping write path with no ordering.

    Args:
        body: The plan content.

    Returns:
        One finding per unordered overlapping pair; empty when every
        overlap is ordered or no two Tasks share a claim.
    """
    edges = {task.urn: frozenset(task.depends_on) for task in body.tasks}
    findings: list[PlanFinding] = []
    tasks = body.tasks
    for index, first in enumerate(tasks):
        first_claims = frozenset(first.write_claims)
        if not first_claims:
            continue
        for second in tasks[index + 1 :]:
            overlap = first_claims & frozenset(second.write_claims)
            if not overlap:
                continue
            if _depends_transitively(first.urn, second.urn, edges) or _depends_transitively(
                second.urn, first.urn, edges
            ):
                continue
            pair = tuple(sorted((str(first.urn), str(second.urn))))
            claimed = tuple(sorted(overlap))
            findings.append(
                PlanFinding(
                    lens=PlanLens.OWNERSHIP,
                    severity="blocking",
                    code=PlanFindingCode.WRITE_CLAIM_OVERLAP_UNORDERED,
                    entity_refs=(*pair, *claimed),
                    message=(
                        f"{pair[0]} and {pair[1]} both claim {', '.join(claimed)} with no "
                        "depends_on edge ordering them"
                    ),
                    remediation=(
                        "Add a depends_on edge ordering the two Tasks, or narrow one "
                        "Task's write_claims so the paths no longer overlap."
                    ),
                )
            )
    return tuple(findings)


def _criterion_fidelity_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag a criterion whose response clause cannot resolve an oracle tier.

    Reuses :func:`~eawf.kernel.spec.common.assign_oracle_tier` rather than
    re-deriving its rules, so the lens and the tier compute can never
    disagree about which clause is malformed.

    Args:
        body: The plan content.

    Returns:
        One finding per criterion whose response clause fails tier
        assignment; empty when every response clause resolves (a
        criterion with no response clause is not this lens's concern).
    """
    findings: list[PlanFinding] = []
    for task in body.tasks:
        for criterion in task.criteria:
            response = criterion.response
            if response is None:
                continue
            try:
                assign_oracle_tier(response)
            except ValueError as exc:
                findings.append(
                    PlanFinding(
                        lens=PlanLens.CRITERION_FIDELITY,
                        severity="blocking",
                        code=PlanFindingCode.CRITERION_FIDELITY_UNRESOLVED,
                        entity_refs=(str(task.urn), criterion.id),
                        message=(
                            f"criterion {criterion.id!r} on {task.urn} fails oracle-tier "
                            f"assignment: {exc}"
                        ),
                        remediation=(
                            "Give the response clause a jury_reason, a recognised gate "
                            "kind, or a matching quantifier and locus."
                        ),
                    )
                )
    return tuple(findings)


def _role_authority_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag a mutating Task that names no write claim for its scope to bound.

    Args:
        body: The plan content.

    Returns:
        Zero findings, or one blocking finding naming every Task whose
        declared ``run_purpose`` mutates a repository but whose
        ``write_claims`` is empty. A Task with no declared ``run_purpose``
        is not checked: the plan has made no claim about it yet.
    """
    offenders = tuple(
        str(task.urn)
        for task in body.tasks
        if task.run_purpose in MUTATING_PURPOSES and not task.write_claims
    )
    if not offenders:
        return ()
    return (
        PlanFinding(
            lens=PlanLens.ROLE_AUTHORITY,
            severity="blocking",
            code=PlanFindingCode.TASK_WRITE_CLAIM_MISSING,
            entity_refs=offenders,
            message=(
                "these Tasks declare a mutating run_purpose but no write_claims, and "
                f"TaskScope refuses a mutating scope with an empty write_set: "
                f"{', '.join(offenders)}"
            ),
            remediation=(
                "Give every mutating Task at least one write_claims path, or drop the "
                "mutating run_purpose until the plan knows what it touches."
            ),
        ),
    )


def _find_repository_cycle(
    graph: dict[RepositoryUrn, frozenset[RepositoryUrn]],
) -> tuple[RepositoryUrn, ...] | None:
    """Return one cycle among *graph*'s edges, via Kahn's algorithm.

    Mirrors :func:`eawf.workflow.planning.lenses.structure._find_cycle`'s
    shape over repository nodes instead of Task nodes.

    Args:
        graph: Every repository's own predecessor set -- the repositories
            that must integrate before it -- keyed by repository.

    Returns:
        The cyclic repositories' URNs, in stable string order, or
        ``None`` when the graph resolves.
    """
    nodes = tuple(graph.keys())
    in_degree = {node: len(graph[node]) for node in nodes}
    dependents: dict[RepositoryUrn, list[RepositoryUrn]] = {node: [] for node in nodes}
    for node, deps in graph.items():
        for dep in deps:
            dependents[dep].append(node)

    ready = sorted((node for node in nodes if in_degree[node] == 0), key=str)
    resolved: set[RepositoryUrn] = set()
    while ready:
        node = ready.pop(0)
        resolved.add(node)
        for dependent in sorted(dependents[node], key=str):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)

    if len(resolved) == len(nodes):
        return None
    return tuple(sorted((node for node in nodes if node not in resolved), key=str))


def _integration_migration_lens(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Flag repositories a rollback could never sequence.

    Each Batch names exactly one repository, so a Task's cross-Batch
    dependency edge always names a pair of repositories too; a cycle in
    the repository graph those edges induce means neither can safely
    land first.

    Args:
        body: The plan content.

    Returns:
        Zero findings, or one blocking finding naming the repositories in
        the cycle.
    """
    repo_of_batch = {batch.urn: batch.repository_ref for batch in body.batches}
    repo_of_task = {task.urn: repo_of_batch[task.batch_ref] for task in body.tasks}
    graph: dict[RepositoryUrn, set[RepositoryUrn]] = {
        repo: set() for repo in repo_of_batch.values()
    }
    for task in body.tasks:
        downstream = repo_of_task[task.urn]
        for dep in task.depends_on:
            upstream = repo_of_task.get(dep)
            if upstream is not None and upstream != downstream:
                graph[downstream].add(upstream)
    cycle = _find_repository_cycle({repo: frozenset(deps) for repo, deps in graph.items()})
    if cycle is None:
        return ()
    rendered = tuple(str(ref) for ref in cycle)
    return (
        PlanFinding(
            lens=PlanLens.INTEGRATION_MIGRATION,
            severity="blocking",
            code=PlanFindingCode.INTEGRATION_REPOSITORY_CYCLE_DETECTED,
            entity_refs=rendered,
            message=(
                "these repositories depend on each other through cross-Batch Task "
                f"edges and admit no rollback sequence: {', '.join(rendered)}"
            ),
            remediation=(
                "Break the cross-repository dependency, or move the dependent work "
                "into a Batch on the same repository."
            ),
        ),
    )


#: Lenses 5-8, in the order they run after structure.py's lenses 1-4.
AUTHORITY_LENS_ORDER: Final[tuple[PlanLens, ...]] = (
    PlanLens.OWNERSHIP,
    PlanLens.CRITERION_FIDELITY,
    PlanLens.ROLE_AUTHORITY,
    PlanLens.INTEGRATION_MIGRATION,
)

_AUTHORITY_LENS_FUNCS: Final[dict[PlanLens, Callable[[PlanBody], tuple[PlanFinding, ...]]]] = {
    PlanLens.OWNERSHIP: _ownership_lens,
    PlanLens.CRITERION_FIDELITY: _criterion_fidelity_lens,
    PlanLens.ROLE_AUTHORITY: _role_authority_lens,
    PlanLens.INTEGRATION_MIGRATION: _integration_migration_lens,
}


def run_authority_lenses(body: PlanBody) -> tuple[PlanFinding, ...]:
    """Run lenses 5-8, in :data:`AUTHORITY_LENS_ORDER`, over *body*.

    Args:
        body: The plan content to validate.

    Returns:
        Every finding any of lenses 5-8 produced, in lens order.
    """
    findings: list[PlanFinding] = []
    for lens in AUTHORITY_LENS_ORDER:
        findings.extend(_AUTHORITY_LENS_FUNCS[lens](body))
    return tuple(findings)


__all__ = [
    "AUTHORITY_LENS_ORDER",
    "run_authority_lenses",
]
