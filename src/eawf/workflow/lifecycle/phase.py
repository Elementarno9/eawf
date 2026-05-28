"""Pure-functional phase lifecycle transitions.

Open / close / plan / activate / archive / reopen for :class:`Phase`. Every
helper mutates the supplied :class:`State` in place. See
:mod:`eawf.workflow.lifecycle.transitions` for the shared design rules and the
re-export surface that keeps ``eawf.workflow.lifecycle.transitions`` import paths
working after the per-entity split.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    DecisionStatus,
    IterStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Phase, State
from eawf.workflow.lifecycle._errors import LifecycleError

if TYPE_CHECKING:
    from eawf.workflow.verify.models import CloseReadiness, CriterionView

logger = logging.getLogger(__name__)

#: Phrases that, when present in an ACTIVE decision scoped to the phase,
#: ratify closing a phase that landed only a single wave (the
#: "single-commit-per-phase" exception of the P19-W03 gate).
_SCOPE_COLLAPSE_PHRASES: tuple[str, ...] = (
    "single-wave",
    "single wave",
    "scope collapse",
    "scope collapsed",
    "collapse scope",
)

#: Wave statuses that are already terminal — :func:`archive_phase` leaves
#: these untouched when cascading to ABANDONED.
_TERMINAL_WAVE_STATUSES: frozenset[WaveStatus] = frozenset(
    {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}
)

#: Iter statuses that are already terminal — :func:`archive_phase` leaves
#: these untouched when cascading to ABANDONED.
_TERMINAL_ITER_STATUSES: frozenset[IterStatus] = frozenset(
    {IterStatus.CLOSED, IterStatus.ABANDONED}
)

_PHASE_CLOSE_ALLOWED_AUDIT_VERDICTS: frozenset[AuditVerdict] = frozenset(
    {AuditVerdict.PASS, AuditVerdict.MINOR}
)
_RELEASE_PREFLIGHT_CHECK_IDS: frozenset[str] = frozenset(
    {"release-preflight", "release_readiness", "release-readiness"}
)
_LEGACY_STUB_CHECK_IDS: frozenset[str] = frozenset({"stub"})


def open_phase(
    state: State,
    *,
    phase_id: str,
    title: str,
    scope_id: str | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
) -> Phase:
    """Insert a new phase into ``state.phases`` with status ``active``.

    Args:
        state: State to mutate in place.
        phase_id: Canonical phase id (e.g. ``P03``).
        title: Bounded ≤72-char phase title.
        scope_id: Optional scope id override; defaults to the project code.
        description: Optional bounded ≤500-char long-form description;
            persisted on :attr:`Phase.description` for downstream renderers.

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
        description=description,
        status=PhaseStatus.ACTIVE,
        iter_ids=[],
        outcome_ids=[],
        opened_at=datetime.now(UTC),
        closed_at=None,
        audit_id=None,
        intent=intent,
    )
    state.phases[phase_id] = phase
    state.current.phase_id = phase_id
    state.current.iter_id = None
    state.current.active_wave_ids = []
    logger.info(f"open_phase id={phase_id} title={title!r}")
    return phase


def has_scope_collapse_decision(state: State, *, phase_id: str) -> bool:
    """Return whether an ACTIVE decision ratifies a single-wave phase close.

    A decision counts when it is :data:`DecisionStatus.ACTIVE`, is scoped to
    *phase_id* (either ``scope_id == phase_id`` or *phase_id* appears in the
    title), and its title/rationale mentions one of
    :data:`_SCOPE_COLLAPSE_PHRASES`. This is the authoritative source for the
    single-wave-without-decision gate; the CLI pre-flight reuses it.
    """
    for decision in (state.decisions or {}).values():
        if decision.status != DecisionStatus.ACTIVE:
            continue
        if decision.scope_id != phase_id and phase_id not in decision.title:
            continue
        haystack = f"{decision.title}\n{decision.rationale}".lower()
        if any(phrase in haystack for phrase in _SCOPE_COLLAPSE_PHRASES):
            return True
    return False


def _phase_close_view(
    criterion_id: str,
    *,
    passed: bool,
) -> CriterionView:
    """Build one phase-close readiness criterion view."""
    from eawf.workflow.verify.models import CriterionView, GateResult

    status = "pass" if passed else "blocked"
    return CriterionView(
        id=criterion_id,
        source="floor",
        status=status,  # type: ignore[arg-type]
        gate_results=[
            GateResult(
                gate_id=criterion_id,
                status="pass" if passed else "blocked",
            )
        ],
    )


def _phase_close_iter_ids(state: State, *, phase_id: str) -> set[str]:
    """Return ids of iters attached to *phase_id*."""
    return {iid for iid, it in state.iters.items() if it.phase_id == phase_id}


def _phase_close_audit_warning(
    state: State,
    *,
    phase: Phase,
    audit_id: str | None,
) -> str | None:
    """Return the close-audit blocker text, or ``None`` when it clears."""
    if audit_id is None:
        return "close audit required"
    audit = (state.audits or {}).get(audit_id)
    if audit is None:
        return f"close audit {audit_id!r} not found"
    if audit.status != AuditStatus.COMPLETE:
        return f"close audit {audit_id!r} must be complete"
    if audit.kind != AuditKind.SHIP_GATE:
        return f"close audit {audit_id!r} must be ship-gate"
    if audit.scope_id not in {phase.id, phase.scope_id}:
        return f"close audit {audit_id!r} must be scoped to phase {phase.id!r}"
    if audit.verdict not in _PHASE_CLOSE_ALLOWED_AUDIT_VERDICTS:
        return f"close audit {audit_id!r} verdict must be pass or minor"
    if not _audit_has_real_close_evidence(state, audit=audit):
        return f"close audit {audit_id!r} must include real audit evidence"
    return None


def _audit_row_get(row: object, key: str) -> object:
    """Read one audit check field from dict-like or model-like rows."""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _audit_check_passed(row: object, *, accepted_ids: frozenset[str]) -> bool:
    """Return whether one audit check row is a passing accepted gate."""
    raw_id = (
        _audit_row_get(row, "id")
        or _audit_row_get(row, "name")
        or _audit_row_get(row, "check_id")
        or _audit_row_get(row, "gate_id")
    )
    if not isinstance(raw_id, str) or raw_id not in accepted_ids:
        return False
    if _audit_row_get(row, "passed") is True:
        return True
    raw_status = _audit_row_get(row, "status") or _audit_row_get(row, "conclusion")
    return isinstance(raw_status, str) and raw_status.lower() in {"pass", "passed", "ok", "success"}


def _audit_row_has_result(row: object) -> bool:
    """Return whether one audit check row carries an explicit result."""
    raw_id = (
        _audit_row_get(row, "id")
        or _audit_row_get(row, "name")
        or _audit_row_get(row, "check_id")
        or _audit_row_get(row, "gate_id")
    )
    if not isinstance(raw_id, str) or not raw_id:
        return False
    raw_details = _audit_row_get(row, "details")
    if raw_id in _LEGACY_STUB_CHECK_IDS and raw_details == "Phase 2 stub":
        return False
    if isinstance(_audit_row_get(row, "passed"), bool):
        return True
    raw_status = _audit_row_get(row, "status") or _audit_row_get(row, "conclusion")
    return isinstance(raw_status, str) and raw_status.lower() in {
        "fail",
        "failed",
        "pass",
        "passed",
        "ok",
        "success",
    }


def _audit_has_real_close_evidence(state: State, *, audit: object) -> bool:
    """Return whether *audit* carries durable close evidence.

    A real close audit is anchored either by a report artifact recorded in
    state, or by at least one non-legacy check result. The old Phase-2
    ``stub`` audit row is intentionally rejected so phase close cannot be
    satisfied by a placeholder verdict alone.
    """
    report_artifact_id = getattr(audit, "report_artifact_id", None)
    if isinstance(report_artifact_id, str) and report_artifact_id:
        return report_artifact_id in state.artifacts
    check_results = getattr(audit, "check_results", [])
    return any(_audit_row_has_result(row) for row in check_results)


def _phase_close_release_preflight_warning(
    state: State,
    *,
    phase: Phase,
    audit_id: str | None,
) -> str | None:
    """Return release-preflight blocker text, or ``None`` when it clears."""
    if audit_id is None:
        return "release preflight required"
    audit = (state.audits or {}).get(audit_id)
    if audit is None:
        return f"release preflight audit {audit_id!r} not found"
    passed_release_check = any(
        _audit_check_passed(row, accepted_ids=_RELEASE_PREFLIGHT_CHECK_IDS)
        for row in audit.check_results
    )
    if passed_release_check:
        return None
    return (
        f"release preflight required: audit {audit_id!r} for phase {phase.id!r} must include "
        "a passing release-preflight check"
    )


def _extend_phase_close_structure(
    state: State,
    *,
    phase: Phase,
    criteria: list[CriterionView],
    warnings: list[str],
) -> None:
    """Append structural phase-close criteria + warnings."""
    phase_id = phase.id
    phase_status_ready = phase.status in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}
    criteria.append(_phase_close_view("phase-status", passed=phase_status_ready))
    if not phase_status_ready:
        warnings.append(f"phase {phase_id!r} has status {phase.status.value!r}; cannot close")

    iter_ids_in_phase = _phase_close_iter_ids(state, phase_id=phase_id)
    open_iters = sorted(
        (
            iid
            for iid, it in state.iters.items()
            if it.phase_id == phase_id and it.status in {IterStatus.PLANNED, IterStatus.ACTIVE}
        ),
        key=natural_key,
    )
    criteria.append(_phase_close_view("open-iters", passed=not open_iters))
    if open_iters:
        warnings.append(f"open iters: {', '.join(open_iters)}")

    open_waves = sorted(
        (
            wid
            for wid, wave in state.waves.items()
            if wave.iter_id in iter_ids_in_phase
            and wave.status in {WaveStatus.PENDING, WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
        ),
        key=natural_key,
    )
    criteria.append(_phase_close_view("open-waves", passed=not open_waves))
    if open_waves:
        warnings.append(f"open waves: {', '.join(open_waves)}")

    closed_wave_count = sum(
        1
        for wave in state.waves.values()
        if wave.iter_id in iter_ids_in_phase and wave.status == WaveStatus.CLOSED
    )
    criteria.append(_phase_close_view("closed-waves", passed=closed_wave_count > 0))
    if closed_wave_count == 0:
        warnings.append(
            f"phase {phase_id!r} has no closed waves; close_phase requires at least one closed wave"
        )

    iters_without_audit = sorted(
        (
            iid
            for iid, it in state.iters.items()
            if it.phase_id == phase_id and it.status == IterStatus.CLOSED and not it.audit_id
        ),
        key=natural_key,
    )
    criteria.append(_phase_close_view("iter-audits", passed=not iters_without_audit))
    if iters_without_audit:
        warnings.append(f"closed iters missing audit: {iters_without_audit}")

    scope_collapse_ready = closed_wave_count != 1 or has_scope_collapse_decision(
        state, phase_id=phase_id
    )
    criteria.append(_phase_close_view("scope-collapse", passed=scope_collapse_ready))
    if not scope_collapse_ready:
        warnings.append(
            f"phase {phase_id!r} has a single closed wave; close_phase requires an active "
            "phase decision documenting scope collapse"
        )


def phase_close_readiness(
    state: State,
    *,
    phase_id: str,
    audit_id: str | None = None,
    require_audit: bool = False,
    include_structure: bool = True,
    require_release_preflight: bool = False,
) -> CloseReadiness:
    """Return phase-level close readiness derived from state.

    The projection deliberately reuses :class:`CloseReadiness` so phase
    close, ``/ship``, and operator renderers share the same status shape
    that wave close already uses. The helper is read-only and does not
    persist evidence; the audit row named by *audit_id* is the evidence
    anchor.

    Raises:
        LifecycleError: when *phase_id* is unknown.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    from eawf.workflow.verify.models import CloseReadiness

    criteria: list[CriterionView] = []
    warnings: list[str] = []

    if include_structure:
        _extend_phase_close_structure(
            state,
            phase=phase,
            criteria=criteria,
            warnings=warnings,
        )

    if require_audit or audit_id is not None:
        audit_warning = _phase_close_audit_warning(state, phase=phase, audit_id=audit_id)
        criteria.append(_phase_close_view("close-audit", passed=audit_warning is None))
        if audit_warning is not None:
            warnings.append(audit_warning)

    if require_release_preflight:
        # _validate_phase_closable extended: phase close enforces release preflight.
        release_warning = _phase_close_release_preflight_warning(
            state, phase=phase, audit_id=audit_id
        )
        criteria.append(_phase_close_view("release-preflight", passed=release_warning is None))
        if release_warning is not None:
            warnings.append(release_warning)

    ready = all(view.status in ("pass", "waived") for view in criteria)
    return CloseReadiness(
        ready=ready,
        criteria=criteria,
        warnings=warnings,
        waived_gate_ids=[],
    )


def phase_close_readiness_blockers(readiness: CloseReadiness) -> list[str]:
    """Render blocker strings from a phase-close readiness view."""
    return list(readiness.warnings)


def _validate_phase_closable(
    state: State,
    *,
    phase_id: str,
    audit_id: str,
    require_release_preflight: bool = False,
) -> Phase:
    """Run the close-phase gates and return the closable phase.

    Raises:
        LifecycleError: when *phase_id* is unknown, the phase is not in a
            closable status, child iters are still open, no wave is closed,
            a CLOSED child iter is missing its audit, or a single-wave phase
            lacks its scope-collapse decision.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    readiness = phase_close_readiness(
        state,
        phase_id=phase_id,
        audit_id=audit_id,
        require_audit=True,
        require_release_preflight=require_release_preflight,
    )
    if not readiness.ready:
        details = "; ".join(phase_close_readiness_blockers(readiness))
        raise LifecycleError(f"phase {phase_id!r} not ready to close: {details}")
    return phase


def close_phase(
    state: State,
    *,
    phase_id: str,
    audit_id: str,
    checkpoint: str | None = None,
    require_release_preflight: bool = False,
) -> Phase:
    """Close an active phase.

    Rejects when child iters are still open, when the phase has zero waves in
    :data:`WaveStatus.CLOSED`, when any CLOSED child iter lacks its
    ``audit_id``, or when the phase landed exactly one closed wave without an
    ACTIVE decision ratifying the scope collapse.

    The ≥1-closed-wave gate (P19-W03) catches the
    "single-commit-per-phase" anti-pattern where a runtime ships the
    entire phase as one commit without closing any waves first. The
    closed-iter-audit and single-wave-decision gates are enforced here (not
    only in the CLI pre-flight) so they hold atomically under the write lock
    on both the daemon-proxy and in-process paths.

    The ``checkpoint`` argument is recorded in the lifecycle event but does
    not currently mutate the phase record — that field will land in Phase 3
    when the audit-link table is introduced.

    Raises:
        LifecycleError: when *phase_id* is unknown, the phase is not in a
            closable status, child iters are still open, no wave is closed,
            a CLOSED child iter is missing its audit, or a single-wave phase
            lacks its scope-collapse decision.
    """
    phase = _validate_phase_closable(
        state,
        phase_id=phase_id,
        audit_id=audit_id,
        require_release_preflight=require_release_preflight,
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
    description: str | None = None,
    intent: IntentBrief | None = None,
) -> Phase:
    """Insert a new phase into ``state.phases`` with status ``planned``.

    Phases created via :func:`plan_phase` sit on the PLANNED queue until
    :func:`activate_phase` flips them to ACTIVE. ``depends_on`` declares
    phase-level prerequisites; cycles are rejected up-front.

    Args:
        state: State to mutate in place.
        phase_id: Canonical phase id (e.g. ``P03``).
        title: Bounded ≤72-char phase title.
        scope_id: Optional scope id override; defaults to the project code.
        depends_on: Optional list of prerequisite phase ids.
        source_brief_ids: Optional list of brief ids motivating this phase.
        description: Optional bounded ≤500-char long-form description;
            persisted on :attr:`Phase.description` for downstream renderers.

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
        description=description,
        status=PhaseStatus.PLANNED,
        iter_ids=[],
        outcome_ids=[],
        depends_on=deps_list,
        source_brief_ids=list(source_brief_ids or []),
        opened_at=datetime.now(UTC),
        closed_at=None,
        audit_id=None,
        intent=intent,
    )
    state.phases[phase_id] = phase
    logger.info(
        f"plan_phase id={phase_id} title={title!r} depends_on={deps_list} "
        f"source_briefs={list(source_brief_ids or [])}"
    )
    return phase


def activate_phase(state: State, *, phase_id: str) -> Phase:
    """Flip a planned phase to active. Sets ``current.phase_id``.

    Hard gate (V11 in P19 brief; tightened in P19-W11):

    - The phase must exist and be in PLANNED status.
    - Every phase in ``depends_on`` must be CLOSED.
    - The phase must already have at least one wave planned under it.

    The branch-currency and clean-working-tree gates live in the CLI
    handler (:func:`eawf.surfaces.cli.commands.lifecycle.phase_activate_cmd`)
    because they need git access; together with the no-waves gate above
    they form the complete P19-W11 V11 hard gate.

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
        raise LifecycleError(
            f"phase {phase_id!r} blocked on un-closed dep phases: {sorted(unmet, key=natural_key)}"
        )
    iter_ids = phase.iter_ids
    wave_count = sum(1 for w in state.waves.values() if w.iter_id in set(iter_ids))
    if wave_count == 0:
        logger.info(f"activate_phase phase={phase_id!r} no_waves=True")
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

    Cascades to the phase's children so archiving never leaves zombie
    non-terminal records behind: every wave under the phase's iters that
    is not already terminal (``closed`` / ``failed`` / ``abandoned``) is
    moved to :data:`WaveStatus.ABANDONED`, and every non-terminal iter
    (``planned`` / ``active``) is moved to :data:`IterStatus.ABANDONED`.
    Closure timestamps are stamped so the closure-timestamp invariant
    holds for the now-terminal children.

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
    now = datetime.now(UTC)
    iter_ids_in_phase = {iid for iid, it in state.iters.items() if it.phase_id == phase_id}
    abandoned_waves = 0
    for wave in state.waves.values():
        if wave.iter_id in iter_ids_in_phase and wave.status not in _TERMINAL_WAVE_STATUSES:
            wave.status = WaveStatus.ABANDONED
            wave.closed_at = now
            abandoned_waves += 1
            if wave.id in state.current.active_wave_ids:
                state.current.active_wave_ids.remove(wave.id)
    abandoned_iters = 0
    for iid in iter_ids_in_phase:
        it = state.iters[iid]
        if it.status not in _TERMINAL_ITER_STATUSES:
            it.status = IterStatus.ABANDONED
            it.closed_at = now
            abandoned_iters += 1
    phase.status = PhaseStatus.ARCHIVED
    phase.closed_at = now
    if state.current.phase_id == phase_id:
        state.current.phase_id = None
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(
        f"archive_phase id={phase_id} abandoned_waves={abandoned_waves} "
        f"abandoned_iters={abandoned_iters}"
    )
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
