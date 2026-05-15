"""Pure-functional open/close transitions for Phase/Iter/Wave.

Every helper in this module mutates the supplied :class:`State` in place and
returns either the affected entity or a small NamedTuple of relevant fields.
The CLI handlers call these inside a held sibling lock; tests call them
directly to keep transitions fast.

Design rules:

- Transitions only enforce **structural** guards (parent open/closed, status
  matches expected before-state). Schema-level invariants (URN regex, enum
  values) live on the Pydantic models. Cross-entity invariants (e.g.
  ``current.phase_id`` must be open) run via :func:`validate_state` on the
  candidate state after the mutation.
- Every transition raises :class:`LifecycleError` on rejection — the CLI
  layer translates that into the right exit code (mostly ``INVALID_INPUT``
  but ``VALIDATION_FAILED`` for closure guards).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.state.enums import (
    AgentSessionRole,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    SubprojectStatus,
    WaveStatus,
)
from eawf.state.models import (
    Iter,
    Phase,
    State,
    Subproject,
    Wave,
)

logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    """Raised by lifecycle transitions when a guard rejects the change.

    The CLI layer catches this and remaps to the appropriate exit code.
    """


# ---- Project / Subproject ---------------------------------------------------


def add_subproject(
    state: State,
    *,
    code: str,
    kind: str,
    title: str,
    domains: list[str] | None = None,
) -> Subproject:
    """Add a new subproject. Raises if ``code`` already exists."""
    if state.project is None:
        raise LifecycleError("cannot add subproject: state has no project")
    if state.subprojects is None:
        state.subprojects = {}
    if code in state.subprojects:
        raise LifecycleError(f"subproject {code!r} already exists")
    sub = Subproject(
        id=code,
        code=code,
        slug=code.lower(),
        title=title,
        kind=kind,
        domains=list(domains or []),
        status=SubprojectStatus.ACTIVE,
        owner=None,
        goal_ids=[],
    )
    state.subprojects[code] = sub
    logger.info(f"add_subproject code={code} title={title!r}")
    return sub


def switch_subproject(state: State, *, code: str) -> None:
    """Set ``current.subproject_id`` to *code*. Raises if unknown."""
    if state.subprojects is None or code not in state.subprojects:
        raise LifecycleError(f"unknown subproject {code!r}")
    state.current.subproject_id = code
    logger.info(f"switch_subproject code={code}")


# ---- Phase ------------------------------------------------------------------


def open_phase(
    state: State,
    *,
    phase_id: str,
    title: str,
    scope_id: str | None = None,
) -> Phase:
    """Insert a new phase into ``state.phases`` with status ``active``.

    Raises:
        LifecycleError: if *phase_id* already exists.
    """
    if phase_id in state.phases:
        raise LifecycleError(f"phase {phase_id!r} already exists")
    project_code = state.project.code if state.project is not None else None
    effective_scope = scope_id or project_code or "unknown"
    phase = Phase(
        id=phase_id,
        scope_id=effective_scope,
        subproject_id=state.current.subproject_id,
        title=title,
        status=PhaseStatus.ACTIVE,
        iter_ids=[],
        outcome_ids=[],
        opened_at=datetime.now(UTC),
        closed_at=None,
        audit_id=None,
    )
    state.phases[phase_id] = phase
    state.current.phase_id = phase_id
    state.current.iter_id = None
    state.current.active_wave_ids = []
    logger.info(f"open_phase id={phase_id} title={title!r}")
    return phase


def close_phase(
    state: State,
    *,
    phase_id: str,
    audit_id: str,
    checkpoint: str | None = None,
) -> Phase:
    """Close an active phase. Rejects when child iters are still open
    or when the phase has zero waves in :data:`WaveStatus.CLOSED`.

    The ≥1-closed-wave gate (P19-W03) catches the
    "single-commit-per-phase" anti-pattern where a runtime ships the
    entire phase as one commit without closing any waves first.

    The ``checkpoint`` argument is recorded in the lifecycle event but does
    not currently mutate the phase record — that field will land in Phase 3
    when the audit-link table is introduced.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise LifecycleError(f"phase {phase_id!r} has status {phase.status.value!r}; cannot close")
    open_children = [
        iid
        for iid, it in state.iters.items()
        if it.phase_id == phase_id and it.status in {IterStatus.PLANNED, IterStatus.ACTIVE}
    ]
    if open_children:
        raise LifecycleError(f"phase {phase_id!r} has open iters: {sorted(open_children)}")
    iter_ids_in_phase = {iid for iid, it in state.iters.items() if it.phase_id == phase_id}
    closed_wave_count = sum(
        1
        for w in state.waves.values()
        if w.iter_id in iter_ids_in_phase and w.status == WaveStatus.CLOSED
    )
    if closed_wave_count == 0:
        raise LifecycleError(
            f"phase {phase_id!r} has no closed waves; close_phase requires at least one closed wave"
        )
    phase.status = PhaseStatus.CLOSED
    phase.closed_at = datetime.now(UTC)
    phase.audit_id = audit_id
    if state.current.phase_id == phase_id:
        state.current.phase_id = None
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(f"close_phase id={phase_id} audit={audit_id} checkpoint={checkpoint!r}")
    return phase


def plan_phase(
    state: State,
    *,
    phase_id: str,
    title: str,
    scope_id: str | None = None,
    depends_on: list[str] | None = None,
    source_brief_ids: list[str] | None = None,
) -> Phase:
    """Insert a new phase into ``state.phases`` with status ``planned``.

    Phases created via :func:`plan_phase` sit on the PLANNED queue until
    :func:`activate_phase` flips them to ACTIVE. ``depends_on`` declares
    phase-level prerequisites; cycles are rejected up-front.

    Raises:
        LifecycleError: if *phase_id* already exists, any declared
            ``depends_on`` references a missing phase, or the resulting
            phase DAG would contain a cycle.
    """
    if phase_id in state.phases:
        raise LifecycleError(f"phase {phase_id!r} already exists")
    deps_list = list(depends_on or [])
    if phase_id in deps_list:
        raise LifecycleError(f"phase {phase_id!r} cannot depend on itself")
    for dep in deps_list:
        if dep not in state.phases:
            raise LifecycleError(f"unknown phase dep: {dep!r}")
    if _would_create_phase_cycle(state, new_id=phase_id, new_deps=deps_list):
        raise LifecycleError(
            f"adding phase {phase_id!r} with depends_on={deps_list} would create a cycle"
        )
    project_code = state.project.code if state.project is not None else None
    effective_scope = scope_id or project_code or "unknown"
    phase = Phase(
        id=phase_id,
        scope_id=effective_scope,
        subproject_id=state.current.subproject_id,
        title=title,
        status=PhaseStatus.PLANNED,
        iter_ids=[],
        outcome_ids=[],
        depends_on=deps_list,
        source_brief_ids=list(source_brief_ids or []),
        opened_at=datetime.now(UTC),
        closed_at=None,
        audit_id=None,
    )
    state.phases[phase_id] = phase
    logger.info(
        f"plan_phase id={phase_id} title={title!r} depends_on={deps_list} "
        f"source_briefs={list(source_brief_ids or [])}"
    )
    return phase


def activate_phase(state: State, *, phase_id: str) -> Phase:
    """Flip a planned phase to active. Sets ``current.phase_id``.

    Hard gate (V11 in P19 brief): the phase must already have at least
    one wave planned under it. Branch currency and clean-working-tree
    checks live in the CLI handler since they need git access.

    Raises:
        LifecycleError: when *phase_id* is unknown, not in PLANNED state,
            has no waves planned, or any phase in ``depends_on`` is not
            yet CLOSED.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status != PhaseStatus.PLANNED:
        raise LifecycleError(
            f"phase {phase_id!r} has status {phase.status.value!r}; "
            "only planned phases can activate"
        )
    unmet = [pid for pid in phase.depends_on if state.phases[pid].status != PhaseStatus.CLOSED]
    if unmet:
        raise LifecycleError(f"phase {phase_id!r} blocked on un-closed dep phases: {sorted(unmet)}")
    iter_ids = phase.iter_ids
    wave_count = sum(1 for w in state.waves.values() if w.iter_id in set(iter_ids))
    if wave_count == 0:
        raise LifecycleError(
            f"phase {phase_id!r} has no planned waves; activate_phase requires at least one wave"
        )
    phase.status = PhaseStatus.ACTIVE
    state.current.phase_id = phase_id
    state.current.iter_id = None
    state.current.active_wave_ids = []
    logger.info(f"activate_phase id={phase_id} waves={wave_count}")
    return phase


def archive_phase(state: State, *, phase_id: str) -> Phase:
    """Move a planned phase to archived. Used by ``eawf roadmap drop``.

    Raises:
        LifecycleError: when *phase_id* is unknown or not in PLANNED state.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status != PhaseStatus.PLANNED:
        raise LifecycleError(
            f"phase {phase_id!r} has status {phase.status.value!r}; "
            "only planned phases can be archived"
        )
    phase.status = PhaseStatus.ARCHIVED
    phase.closed_at = datetime.now(UTC)
    logger.info(f"archive_phase id={phase_id}")
    return phase


def _would_create_phase_cycle(
    state: State,
    *,
    new_id: str,
    new_deps: list[str],
) -> bool:
    """Return True iff inserting *new_id* with phase-level *new_deps* yields a cycle."""
    deps_by_node: dict[str, set[str]] = {pid: set(p.depends_on) for pid, p in state.phases.items()}
    deps_by_node[new_id] = set(new_deps)
    in_degree: dict[str, int] = {node: len(parents) for node, parents in deps_by_node.items()}
    children: dict[str, list[str]] = {node: [] for node in deps_by_node}
    for node, parents in deps_by_node.items():
        for parent in parents:
            if parent in children:
                children[parent].append(node)
    ready = [node for node, count in in_degree.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    return visited != len(deps_by_node)


def reopen_phase(state: State, *, phase_id: str) -> Phase:
    """Reopen a closed phase. Flips status closed→active, clears closed_at.

    Audit linkage (``audit_id``) is preserved so the original close evidence
    stays reconstructible; the next ``close_phase`` overwrites it with a new
    audit. ``state.current.phase_id`` is set to *phase_id* iff no other phase
    is currently active.

    Raises:
        LifecycleError: when *phase_id* is unknown or not in the closed state.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status != PhaseStatus.CLOSED:
        raise LifecycleError(
            f"phase {phase_id!r} has status {phase.status.value!r}; only closed phases can reopen"
        )
    phase.status = PhaseStatus.ACTIVE
    phase.closed_at = None
    if state.current.phase_id is None:
        state.current.phase_id = phase_id
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(f"reopen_phase id={phase_id}")
    return phase


# ---- Iter -------------------------------------------------------------------


def open_iter(
    state: State,
    *,
    iter_id: str,
    phase_id: str,
    title: str,
) -> Iter:
    """Insert a new iter under *phase_id* with status ``active``.

    Raises:
        LifecycleError: if the phase is missing or not open, or if *iter_id*
            already exists.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise LifecycleError(f"phase {phase_id!r} is not open (status={phase.status.value!r})")
    if iter_id in state.iters:
        raise LifecycleError(f"iter {iter_id!r} already exists")
    it = Iter(
        id=iter_id,
        phase_id=phase_id,
        title=title,
        status=IterStatus.ACTIVE,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )
    state.iters[iter_id] = it
    if iter_id not in phase.iter_ids:
        phase.iter_ids.append(iter_id)
    state.current.phase_id = phase_id
    state.current.iter_id = iter_id
    state.current.active_wave_ids = []
    logger.info(f"open_iter id={iter_id} phase={phase_id} title={title!r}")
    return it


def close_iter(state: State, *, iter_id: str, audit_id: str) -> Iter:
    """Close an active iter. Rejects when child waves are still open."""
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    if it.status not in {IterStatus.PLANNED, IterStatus.ACTIVE}:
        raise LifecycleError(f"iter {iter_id!r} has status {it.status.value!r}; cannot close")
    open_waves = [
        wid
        for wid, w in state.waves.items()
        if w.iter_id == iter_id
        and w.status in {WaveStatus.PENDING, WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
    ]
    if open_waves:
        raise LifecycleError(f"iter {iter_id!r} has open waves: {sorted(open_waves)}")
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    it.audit_id = audit_id
    if state.current.iter_id == iter_id:
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(f"close_iter id={iter_id} audit={audit_id}")
    return it


def plan_iter(
    state: State,
    *,
    iter_id: str,
    phase_id: str,
    title: str,
) -> Iter:
    """Insert a new iter under *phase_id* with status ``planned``.

    Companion of :func:`plan_phase`. The iter sits in PLANNED until
    :func:`activate_iter` (or :func:`activate_phase` when the parent
    activates and the iter is the sole open child).

    Raises:
        LifecycleError: when the phase is missing, the phase is not
            PLANNED or ACTIVE, or *iter_id* already exists.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise LifecycleError(f"phase {phase_id!r} is not open (status={phase.status.value!r})")
    if iter_id in state.iters:
        raise LifecycleError(f"iter {iter_id!r} already exists")
    it = Iter(
        id=iter_id,
        phase_id=phase_id,
        title=title,
        status=IterStatus.PLANNED,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )
    state.iters[iter_id] = it
    if iter_id not in phase.iter_ids:
        phase.iter_ids.append(iter_id)
    logger.info(f"plan_iter id={iter_id} phase={phase_id} title={title!r}")
    return it


def activate_iter(state: State, *, iter_id: str) -> Iter:
    """Flip a planned iter to active.

    Updates ``current.iter_id``. The parent phase must already be ACTIVE
    (or PLANNED but in the middle of a coordinated activate sequence —
    callers responsible for ordering).

    Raises:
        LifecycleError: when *iter_id* is unknown or not in PLANNED state.
    """
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    if it.status != IterStatus.PLANNED:
        raise LifecycleError(
            f"iter {iter_id!r} has status {it.status.value!r}; only planned iters can activate"
        )
    it.status = IterStatus.ACTIVE
    state.current.phase_id = it.phase_id
    state.current.iter_id = iter_id
    state.current.active_wave_ids = []
    logger.info(f"activate_iter id={iter_id} phase={it.phase_id}")
    return it


# ---- Wave -------------------------------------------------------------------


def plan_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    title: str,
    file_scopes: list[str],
    deps: list[str] | None = None,
    success_criteria: list[str] | None = None,
    agent_role: AgentSessionRole | None = None,
    effort_bucket: EffortBucket | None = None,
) -> Wave:
    """Insert a new wave with status ``pending``.

    Raises:
        LifecycleError: if iter is missing/closed, wave id duplicates, any
            declared dep references a missing wave id, the dep set names
            the wave itself, or the resulting graph would contain a cycle.

    Side-effects on the reverse-index: for every ``dep`` in *deps*, the
    dep wave's ``blocks`` list is mutated in-place to include *wave_id*
    (idempotent — already-present ids are not duplicated).
    """
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    if it.status not in {IterStatus.PLANNED, IterStatus.ACTIVE}:
        raise LifecycleError(f"iter {iter_id!r} is not open (status={it.status.value!r})")
    if wave_id in state.waves:
        raise LifecycleError(f"wave {wave_id!r} already exists")
    deps_list = list(deps or [])
    if wave_id in deps_list:
        raise LifecycleError(f"wave {wave_id!r} cannot depend on itself")
    for dep in deps_list:
        if dep not in state.waves:
            raise LifecycleError(f"unknown dep wave {dep!r}")
    # Cycle check: simulate the post-insert DAG over deps and topo-sort.
    # The new wave depends on every entry of ``deps_list``; existing
    # waves keep their current ``deps``. If toposort fails any node
    # remains unprocessed -> cycle. Self-dep is already rejected above.
    if _would_create_cycle(state, new_id=wave_id, new_deps=deps_list):
        raise LifecycleError(f"adding wave {wave_id!r} with deps={deps_list} would create a cycle")
    wave = Wave(
        id=wave_id,
        iter_id=iter_id,
        title=title,
        status=WaveStatus.PENDING,
        deps=deps_list,
        blocks=[],
        file_scopes=list(file_scopes),
        success_criteria=list(success_criteria or []),
        agent_role=agent_role,
        effort_bucket=effort_bucket,
        claim_session_id=None,
        worktree_id=None,
        outcome=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )
    state.waves[wave_id] = wave
    if wave_id not in it.wave_ids:
        it.wave_ids.append(wave_id)
    # Maintain the reverse "blocks" index: every dep gains the new wave
    # in its blocks list (idempotent).
    for dep in deps_list:
        dep_wave = state.waves[dep]
        if wave_id not in dep_wave.blocks:
            dep_wave.blocks.append(wave_id)
    logger.info(f"plan_wave id={wave_id} iter={iter_id} files={file_scopes} deps={deps_list}")
    return wave


def _would_create_cycle(
    state: State,
    *,
    new_id: str,
    new_deps: list[str],
) -> bool:
    """Return True iff inserting *new_id* with *new_deps* yields a cycle.

    Kahn-style topo sort over the union {existing waves' deps,
    new_id -> new_deps}. If any node remains unprocessed after the
    sweep there is at least one cycle reachable from / containing it.
    """
    # adjacency: child_id -> set of parent dep ids (edges parent -> child)
    deps_by_node: dict[str, set[str]] = {wid: set(w.deps) for wid, w in state.waves.items()}
    deps_by_node[new_id] = set(new_deps)
    # in_degree: incoming-edge count per node (an edge dep -> wave for each dep)
    in_degree: dict[str, int] = {node: len(parents) for node, parents in deps_by_node.items()}
    # children index: parent_id -> list of waves that list it as a dep
    children: dict[str, list[str]] = {node: [] for node in deps_by_node}
    for node, parents in deps_by_node.items():
        for parent in parents:
            # Skip dangling dep ids that were already rejected by the
            # caller's "unknown dep" guard — guard against KeyError if a
            # call site sneaks past that check.
            if parent in children:
                children[parent].append(node)
    ready = [node for node, count in in_degree.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    return visited != len(deps_by_node)


def edit_wave_plan(
    state: State,
    *,
    wave_id: str,
    title: str | None = None,
    file_scopes: list[str] | None = None,
    success_criteria: list[str] | None = None,
    agent_role: AgentSessionRole | None = None,
    effort_bucket: EffortBucket | None = None,
) -> Wave:
    """Mutate a PENDING wave's plan-time fields. Rejects non-PENDING waves.

    Editable surface: title, file_scopes, success_criteria, agent_role,
    effort_bucket. Dep mutations go through :func:`set_wave_deps` (it
    maintains the reverse ``blocks`` index and re-runs the cycle check).

    The PENDING gate is also the load-bearing invariant for the
    ACTIVE-phase revise path (P19-W12): the CLI gate accepts an ACTIVE
    parent phase, and this guard ensures only PENDING waves under it
    actually mutate while CLOSED/CLAIMED/IN_PROGRESS waves stay frozen.

    Raises:
        LifecycleError: when *wave_id* is unknown or not PENDING.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(
            f"wave {wave_id!r} is not pending (status={wave.status.value!r}); cannot edit plan"
        )
    if title is not None:
        wave.title = title
    if file_scopes is not None:
        wave.file_scopes = list(file_scopes)
    if success_criteria is not None:
        wave.success_criteria = list(success_criteria)
    if agent_role is not None:
        wave.agent_role = agent_role
    if effort_bucket is not None:
        wave.effort_bucket = effort_bucket
    logger.info(
        f"edit_wave_plan id={wave_id} title={title!r} file_scopes={file_scopes} "
        f"agent_role={agent_role} effort_bucket={effort_bucket}"
    )
    return wave


def remove_wave_plan(state: State, *, wave_id: str) -> None:
    """Delete a PENDING wave from state. Also strips reverse-index entries.

    Like :func:`edit_wave_plan`, the PENDING guard here is what makes
    ``eawf roadmap revise --remove-wave`` safe under an ACTIVE parent
    phase (P19-W12): CLOSED/CLAIMED/IN_PROGRESS waves never get
    removed regardless of the parent phase's status.

    Raises:
        LifecycleError: when *wave_id* is unknown or not PENDING.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(
            f"wave {wave_id!r} is not pending (status={wave.status.value!r}); cannot remove"
        )
    if wave.blocks:
        raise LifecycleError(
            f"wave {wave_id!r} blocks other waves {sorted(wave.blocks)}; "
            "remove those first or break the dep"
        )
    for dep_id in wave.deps:
        dep_wave = state.waves.get(dep_id)
        if dep_wave is not None and wave_id in dep_wave.blocks:
            dep_wave.blocks.remove(wave_id)
    parent_iter = state.iters.get(wave.iter_id)
    if parent_iter is not None and wave_id in parent_iter.wave_ids:
        parent_iter.wave_ids.remove(wave_id)
    del state.waves[wave_id]
    logger.info(f"remove_wave_plan id={wave_id}")


def set_wave_deps(state: State, *, wave_id: str, deps: list[str]) -> Wave:
    """Replace a PENDING wave's deps. Maintains the reverse ``blocks`` index.

    Like the other ``*_wave_plan`` helpers, the PENDING guard is what
    keeps the ACTIVE-phase revise path (P19-W12) safe — only PENDING
    waves under an ACTIVE parent can have their dep set rewritten.

    Raises:
        LifecycleError: when *wave_id* is unknown, not PENDING, any dep
            id is unknown, self-dep, or the new graph would cycle.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(
            f"wave {wave_id!r} is not pending (status={wave.status.value!r}); cannot set deps"
        )
    new_deps = list(deps)
    if wave_id in new_deps:
        raise LifecycleError(f"wave {wave_id!r} cannot depend on itself")
    for dep in new_deps:
        if dep not in state.waves:
            raise LifecycleError(f"unknown dep wave {dep!r}")
    old_deps = list(wave.deps)
    wave.deps = new_deps
    try:
        if _would_create_cycle(state, new_id=wave_id, new_deps=new_deps):
            raise LifecycleError(f"set_wave_deps on {wave_id!r} with deps={new_deps} would cycle")
    except LifecycleError:
        wave.deps = old_deps
        raise
    for dep in old_deps:
        dep_wave = state.waves.get(dep)
        if dep_wave is not None and wave_id in dep_wave.blocks:
            dep_wave.blocks.remove(wave_id)
    for dep in new_deps:
        dep_wave = state.waves[dep]
        if wave_id not in dep_wave.blocks:
            dep_wave.blocks.append(wave_id)
    logger.info(f"set_wave_deps id={wave_id} deps={new_deps}")
    return wave


def claim_wave(
    state: State,
    *,
    wave_id: str,
    session_id: str,
    out_of_order: bool = False,
) -> Wave:
    """Move a pending wave to ``claimed`` and bind it to *session_id*.

    Re-claiming an already-claimed wave with the *same* session is a no-op
    (idempotent). Re-claiming with a *different* session is rejected so the
    sibling-lock + status check delivers exactly-once semantics.

    P19-W02 dep + monotonic gates:

    - Reject the claim when any wave in ``wave.deps`` is not in
      :data:`WaveStatus.CLOSED`. Dependencies must land before a
      downstream wave can start.
    - Reject the claim when a sibling wave with a numerically lower
      ``W##`` is still PENDING under the same iter AND its own deps
      are already satisfied — this enforces the monotonic claim
      order that prevents parallel runtimes from skipping ahead.
    - ``out_of_order=True`` (CLI ``--out-of-order``) is the
      operator-blessed escape hatch for parallel-worktree dispatch
      where multiple waves of the same dep-frontier are intentionally
      claimed at once.

    Raises:
        LifecycleError: when *wave_id* is unknown, wave is not
            PENDING, dep waves are not CLOSED, or a lower-numbered
            sibling-ready wave is still PENDING (without
            ``out_of_order``).
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status == WaveStatus.CLAIMED and wave.claim_session_id == session_id:
        logger.debug(f"claim_wave idempotent id={wave_id} session={session_id}")
        return wave
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(f"wave {wave_id!r} cannot be claimed (status={wave.status.value!r})")
    unmet_deps = [
        dep_id
        for dep_id in wave.deps
        if state.waves.get(dep_id) is None or state.waves[dep_id].status != WaveStatus.CLOSED
    ]
    if unmet_deps:
        raise LifecycleError(
            f"wave {wave_id!r} blocked on un-closed dep waves: {sorted(unmet_deps)}"
        )
    if not out_of_order:
        skipped = _lower_w_sibling_pending(state, wave)
        if skipped:
            raise LifecycleError(
                f"wave {wave_id!r} would skip lower-numbered ready siblings: "
                f"{sorted(skipped)}; pass --out-of-order to claim regardless"
            )
    wave.status = WaveStatus.CLAIMED
    wave.claim_session_id = session_id
    if wave_id not in state.current.active_wave_ids:
        state.current.active_wave_ids.append(wave_id)
    logger.info(f"claim_wave id={wave_id} session={session_id} out_of_order={out_of_order}")
    return wave


def _lower_w_sibling_pending(state: State, wave: Wave) -> list[str]:
    """Return PENDING sibling wave ids with a lower ``W##`` whose deps are CLOSED.

    A sibling shares ``wave.iter_id``. ``W##`` ordering uses the
    trailing two digits of the wave id. Returns an empty list when no
    such "ready and unclaimed" lower-numbered wave exists.
    """
    suffix = wave.id.split("-")[-1]
    if not (suffix.startswith("W") and suffix[1:].isdigit()):
        return []
    my_index = int(suffix[1:])
    skipped: list[str] = []
    for other_id, other in state.waves.items():
        if other.iter_id != wave.iter_id:
            continue
        other_suffix = other_id.split("-")[-1]
        if not (other_suffix.startswith("W") and other_suffix[1:].isdigit()):
            continue
        if int(other_suffix[1:]) >= my_index:
            continue
        if other.status != WaveStatus.PENDING:
            continue
        deps_met = all(
            state.waves.get(d) is not None and state.waves[d].status == WaveStatus.CLOSED
            for d in other.deps
        )
        if deps_met:
            skipped.append(other_id)
    return skipped


def close_wave(
    state: State,
    *,
    wave_id: str,
    outcome: str,
) -> Wave:
    """Close a claimed/in-progress wave with an outcome string.

    The wave's commit SHA is derived at read time via
    :func:`eawf.lifecycle.wave_sha.derive_wave_sha` from the
    ``[P##-W##]`` commit-subject prefix; it is no longer persisted on
    the wave record (P19-W04 removed ``Wave.commit``).
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise LifecycleError(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot close"
        )
    wave.status = WaveStatus.CLOSED
    wave.outcome = outcome
    wave.closed_at = datetime.now(UTC)
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    logger.info(f"close_wave id={wave_id} outcome={outcome!r}")
    return wave


def fail_wave(state: State, *, wave_id: str, reason: str) -> Wave:
    """Mark a claimed/in-progress wave as ``failed`` with *reason*."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status in {
        WaveStatus.CLOSED,
        WaveStatus.FAILED,
        WaveStatus.ABANDONED,
    }:
        raise LifecycleError(f"wave {wave_id!r} already terminal (status={wave.status.value!r})")
    wave.status = WaveStatus.FAILED
    wave.outcome = reason
    wave.closed_at = datetime.now(UTC)
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    logger.info(f"fail_wave id={wave_id} reason={reason!r}")
    return wave
