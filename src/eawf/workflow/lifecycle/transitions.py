"""Pure-functional open/close transitions for Phase/Iter/Wave.

Every helper mutates the supplied :class:`State` in place and returns either
the affected entity or a small NamedTuple of relevant fields. The CLI
handlers call these inside a held sibling lock; tests call them directly to
keep transitions fast.

Design rules:

- Transitions only enforce **structural** guards (parent open/closed, status
  matches expected before-state). Schema-level invariants (URN regex, enum
  values) live on the Pydantic models. Cross-entity invariants (e.g.
  ``current.phase_id`` must be open) run via :func:`validate_state` on the
  candidate state after the mutation.
- Every transition raises :class:`LifecycleError` on rejection — the CLI
  layer translates that into the right exit code (mostly ``INVALID_INPUT``
  but ``VALIDATION_FAILED`` for closure guards).

The transitions are physically organised per entity to keep each module
under the Q25 LOC cap; this module re-exports the full surface so the
``eawf.workflow.lifecycle.transitions`` import path keeps working unchanged:

- :class:`LifecycleError` — :mod:`eawf.workflow.lifecycle._errors`
- project/track helpers — :mod:`eawf.workflow.lifecycle.project`
- phase helpers — :mod:`eawf.workflow.lifecycle.phase`
- iter helpers — :mod:`eawf.workflow.lifecycle.iter_`
- wave helpers — :mod:`eawf.workflow.lifecycle.wave`
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from eawf.kernel.spec.roadmap_plan import RoadmapPlan, RoadmapPlanWave
from eawf.kernel.state.models import CriteriaFloorWaiver, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.iter_ import (
    activate_iter,
    close_iter,
    edit_iter_plan,
    open_iter,
    plan_iter,
    set_iter_candidate_tag,
)
from eawf.workflow.lifecycle.phase import (
    activate_phase,
    archive_phase,
    close_phase,
    edit_phase_plan,
    has_scope_collapse_decision,
    open_phase,
    phase_close_readiness,
    phase_close_readiness_blockers,
    plan_phase,
    reopen_phase,
)
from eawf.workflow.lifecycle.project import (
    add_track,
    switch_track,
)
from eawf.workflow.lifecycle.wave import (
    claim_wave,
    close_wave,
    edit_wave_plan,
    fail_wave,
    plan_wave,
    release_wave,
    remove_wave_plan,
    set_wave_deps,
    start_wave,
)


class PlannedRoadmap(NamedTuple):
    """Ids inserted by :func:`plan_roadmap`."""

    phase_id: str
    iter_ids: list[str]
    wave_ids: list[str]


def plan_roadmap(state: State, *, plan: RoadmapPlan) -> PlannedRoadmap:
    """Stage a whole roadmap plan into ``state``.

    The strict :class:`RoadmapPlan` loader owns schema validation. This
    transition owns state mutation: phase first, iters second, waves last.
    Waves are planned in dependency order so the plan file need not sort a
    child after every prerequisite.

    Args:
        state: State to mutate in place.
        plan: Already-validated roadmap plan.

    Returns:
        Inserted phase, iter, and wave ids.

    Raises:
        LifecycleError: when a lifecycle guard rejects an insert.
    """
    phase = plan.phase
    plan_phase(
        state,
        phase_id=phase.id,
        title=phase.title,
        depends_on=list(phase.depends_on),
        source_brief_ids=list(phase.source_brief_ids),
        description=phase.description,
    )
    iter_ids: list[str] = []
    for iter_plan in plan.iters:
        plan_iter(
            state,
            iter_id=iter_plan.id,
            phase_id=phase.id,
            title=iter_plan.title,
            description=iter_plan.description,
        )
        iter_ids.append(iter_plan.id)

    wave_ids: list[str] = []
    for iter_id, wave_plan in _roadmap_waves_in_dep_order(plan):
        # Propose-staged waves carry the plan document's free-form criteria
        # strings; the typed criteria land via spec sync before activation,
        # so the propose stage waives the plan-time typed-criteria floor
        # with a visible record instead of blocking the whole propose.
        plan_wave(
            state,
            wave_id=wave_plan.id,
            iter_id=iter_id,
            title=wave_plan.title,
            file_scopes=list(wave_plan.file_scopes),
            deps=list(wave_plan.deps),
            success_criteria=list(wave_plan.success_criteria),
            agent_role=wave_plan.agent_role,
            effort_bucket=wave_plan.effort_bucket,
            description=wave_plan.description,
            intent=wave_plan.intent,
            criteria_floor_waiver=_propose_stage_floor_waiver(wave_plan),
        )
        wave_ids.append(wave_plan.id)
    return PlannedRoadmap(phase_id=phase.id, iter_ids=iter_ids, wave_ids=wave_ids)


def _propose_stage_floor_waiver(wave_plan: RoadmapPlanWave) -> CriteriaFloorWaiver | None:
    """Return the propose-stage criteria-floor waiver, or ``None``.

    Only a wave whose plan document supplies free-form (legacy) criteria
    strings needs the waiver; a wave staged without criteria passes the
    floor on its own and stays waiver-free so the spec-sync authoring
    path keeps full enforcement.
    """
    if not wave_plan.success_criteria:
        return None
    return CriteriaFloorWaiver(
        reason="staged by roadmap propose; typed criteria land via spec sync",
        waived_at=datetime.now(UTC),
    )


def _roadmap_waves_in_dep_order(plan: RoadmapPlan) -> list[tuple[str, RoadmapPlanWave]]:
    """Return plan waves topologically sorted with file order as tie-breaker."""
    by_wave: dict[str, tuple[int, str, RoadmapPlanWave]] = {}
    order = 0
    for iter_plan in plan.iters:
        for wave_plan in iter_plan.waves:
            by_wave[wave_plan.id] = (order, iter_plan.id, wave_plan)
            order += 1
    remaining = set(by_wave)
    planned: list[tuple[str, RoadmapPlanWave]] = []
    while remaining:
        ready = [
            wave_id
            for wave_id in remaining
            if all(dep not in remaining for dep in by_wave[wave_id][2].deps)
        ]
        if not ready:
            raise LifecycleError("roadmap plan wave deps contain a cycle")
        for wave_id in sorted(ready, key=lambda item: by_wave[item][0]):
            _order, iter_id, wave_plan = by_wave[wave_id]
            planned.append((iter_id, wave_plan))
            remaining.remove(wave_id)
    return planned


__all__ = [
    "LifecycleError",
    "PlannedRoadmap",
    "activate_iter",
    "activate_phase",
    "add_track",
    "archive_phase",
    "claim_wave",
    "close_iter",
    "close_phase",
    "close_wave",
    "edit_iter_plan",
    "edit_phase_plan",
    "edit_wave_plan",
    "fail_wave",
    "has_scope_collapse_decision",
    "open_iter",
    "open_phase",
    "phase_close_readiness",
    "phase_close_readiness_blockers",
    "plan_iter",
    "plan_phase",
    "plan_roadmap",
    "plan_wave",
    "release_wave",
    "remove_wave_plan",
    "reopen_phase",
    "set_iter_candidate_tag",
    "set_wave_deps",
    "start_wave",
    "switch_track",
]
