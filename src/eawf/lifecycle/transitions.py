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
    """Close an active phase. Rejects when child iters are still open.

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
    phase.status = PhaseStatus.CLOSED
    phase.closed_at = datetime.now(UTC)
    phase.audit_id = audit_id
    if state.current.phase_id == phase_id:
        state.current.phase_id = None
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(f"close_phase id={phase_id} audit={audit_id} checkpoint={checkpoint!r}")
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


# ---- Wave -------------------------------------------------------------------


def plan_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    title: str,
    file_scopes: list[str],
    deps: list[str] | None = None,
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
        claim_session_id=None,
        worktree_id=None,
        commit=None,
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


def claim_wave(state: State, *, wave_id: str, session_id: str) -> Wave:
    """Move a pending wave to ``claimed`` and bind it to *session_id*.

    Re-claiming an already-claimed wave with the *same* session is a no-op
    (idempotent). Re-claiming with a *different* session is rejected so the
    sibling-lock + status check delivers exactly-once semantics.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status == WaveStatus.CLAIMED and wave.claim_session_id == session_id:
        logger.debug(f"claim_wave idempotent id={wave_id} session={session_id}")
        return wave
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(f"wave {wave_id!r} cannot be claimed (status={wave.status.value!r})")
    wave.status = WaveStatus.CLAIMED
    wave.claim_session_id = session_id
    if wave_id not in state.current.active_wave_ids:
        state.current.active_wave_ids.append(wave_id)
    logger.info(f"claim_wave id={wave_id} session={session_id}")
    return wave


def close_wave(
    state: State,
    *,
    wave_id: str,
    commit: str,
    outcome: str,
) -> Wave:
    """Close a claimed/in-progress wave with a commit + outcome string."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise LifecycleError(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot close"
        )
    wave.status = WaveStatus.CLOSED
    wave.commit = commit
    wave.outcome = outcome
    wave.closed_at = datetime.now(UTC)
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    logger.info(f"close_wave id={wave_id} commit={commit}")
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
