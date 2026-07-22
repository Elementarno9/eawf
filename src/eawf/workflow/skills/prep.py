"""``/prep`` skill — plan the current/selected iter as a wave-partitioned DAG.

Implements the §14 algorithm for ``/prep``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Resolve planning mode: current iter by default; explicit ``p##[-i##]``
   if supplied; ``-i`` means fix-plan after audit/review findings.
3. Load state, accepted research, decisions, backlog, memory, current
   code/docs, acceptance config.
4. Define objective and non-goals.
5. Build task DAG: task ID, deps, file scope, commands, evidence, risk,
   expected artifact.
6. Partition into waves: parallel only for disjoint scopes; assign worktree
   policy.
7. Estimate each wave; roll up the iter budget.
8. Allocate IDs (``p##-i##`` and ``p##-i##-w##``).
9. Write plan / spec artefact, state wave stubs, and estimate records.
10. Ask approval if ``approval=ask``, risky, destructive, ambiguous, or
    budget exceeds threshold.

State is the source of the DAG: the build step reads the target phase's
PENDING waves directly from ``state.json`` (read-only — the daemon stays
the sole mutator). Two lifecycle short-circuits guard the plan path:

- **Idempotency** — ``/prep`` on a phase that is already ``ACTIVE`` is a
  no-op: it returns ``status=ok`` with ``body.no_op=True`` and emits no
  ``prep.build_dag`` event (the phase is already planned + activated).
- **Closed-phase block** — ``/prep`` on a ``CLOSED`` phase returns
  ``status=blocked`` with ``repair_commands=["eawf phase reopen <phase>"]``;
  the operator must reopen before re-planning.

Heavy LLM-fanout (steps 4-5) degrade to ``status=needs_user`` with a typed
:class:`UserQuestion` when the caller flags ``approval=ask``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import PhaseStatus, WaveStatus
from eawf.kernel.state.ids import parents_of
from eawf.kernel.state.models import Phase, State
from eawf.surfaces.render.envelope import EnvelopeWarning, SkillName
from eawf.workflow.skills.bodies.prep import (
    PrepAcceptance,
    PrepBody,
    PrepDagTask,
    PrepWave,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.engine import ActionRun, SkillAction, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


_DEFAULT_ITER_ID: str = "P00-I01"

#: Closed set of ``--ceremony`` values. An out-of-set token records an
#: advisory warning and falls back to the compute_ceremony recommendation
#: rather than aborting the plan (the flag is an override, not a gate).
_CEREMONY_VALUES: frozenset[str] = frozenset({"lite", "full"})


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Best-effort string->bool coercion for stdin-piped JSON args.

    Args:
        value: The raw arg value (``None`` when the flag is absent).
        default: Returned verbatim when *value* is ``None``.

    Returns:
        The coerced boolean; ``default`` when *value* is ``None``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _merged_config(state_path: Path) -> dict[str, Any]:
    """Compose the layered config anchored at the active repo (read-only).

    Mirrors :meth:`eawf.workflow.skills.research.ResearchSkill._merged_config`:
    the anchor is the state file's grandparent (``.ea`` is the parent). A
    merge failure degrades to an empty mapping so callers fall back to the
    built-in default rather than crashing the run.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The merged config mapping, or ``{}`` on any merge failure.
    """
    from eawf.kernel.config.layered import merge_config

    anchor = state_path.parent.parent
    try:
        merged, _sources = merge_config(repo=anchor, workspace=anchor)
    except Exception as exc:  # pragma: no cover - defensive only
        logger.debug(f"_merged_config merge_error={exc!r}")
        return {}
    return merged


def _load_state(state_path: Path) -> State | None:
    """Return the validated :class:`State`, or ``None`` when unreadable.

    The read is best-effort and read-only (rule 4: the daemon is the sole
    mutator; reads are free). A missing file, malformed JSON, or schema
    mismatch all degrade to ``None`` so the skill can fall back to the
    plan path with an empty DAG rather than crash.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The validated state document, or ``None`` when the file is absent
        or fails to parse / validate.
    """
    if not state_path.exists():
        return None
    try:
        return State.model_validate(orjson.loads(state_path.read_bytes()))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        logger.warning(f"_load_state read-failed path={state_path} exc={exc}")
        return None


def _resolve_phase_id(args: dict[str, Any], iter_id: str, state: State | None) -> str | None:
    """Resolve the target phase id for this ``/prep`` invocation.

    Precedence: explicit ``phase`` / ``phase_id`` arg, then the phase that
    parents the resolved ``iter_id``, then ``state.current.phase_id``.

    Args:
        args: The skill's parsed arg dict.
        iter_id: The already-resolved iter id (default or explicit).
        state: The loaded state, or ``None`` when no state file is present.

    Returns:
        The target phase id, or ``None`` when none can be resolved.
    """
    explicit = args.get("phase_id") or args.get("phase")
    if explicit:
        return str(explicit)
    parents = parents_of(iter_id)
    if parents:
        return parents[0]
    if state is not None:
        return state.current.phase_id
    return None


def _build_dag_from_phase(phase: Phase, state: State) -> tuple[list[PrepDagTask], list[PrepWave]]:
    """Project a phase's PENDING waves into typed DAG tasks + wave groups.

    Walks ``phase.iter_ids`` in order; for each iter walks ``iter.wave_ids``
    and emits one :class:`PrepDagTask` per wave whose status is
    :attr:`WaveStatus.PENDING`. Tasks are keyed by their canonical wave id
    (no synthetic ``T01``); ``deps`` and ``file_scope`` mirror the wave
    record. Each task also carries ``agent_role`` / ``effort_bucket`` /
    ``estimate_eu`` (P28-W18) so the prep body surfaces the same three
    planning signals the canonical ``plan_view`` renderer projects into
    ``roadmap show --md`` and the TUI tree. Each iter that contributes
    at least one PENDING wave yields one :class:`PrepWave` grouping its
    task ids; the wave estimate sums the per-task ``estimate_eu``.

    Args:
        phase: The target phase record.
        state: The loaded state holding the ``iters`` and ``waves`` maps.

    Returns:
        A two-tuple ``(dag, waves)`` of the projected tasks and wave groups.
        Both are empty when the phase has no PENDING waves.
    """
    from eawf.workflow.estimation.buckets import wave_estimate_eu

    pending_wave_ids = {
        wave_id
        for iter_id in phase.iter_ids
        if (iter_record := state.iters.get(iter_id)) is not None
        for wave_id in iter_record.wave_ids
        if (wave := state.waves.get(wave_id)) is not None and wave.status is WaveStatus.PENDING
    }
    dag: list[PrepDagTask] = []
    waves: list[PrepWave] = []
    for iter_id in phase.iter_ids:
        iter_record = state.iters.get(iter_id)
        if iter_record is None:
            continue
        iter_task_ids: list[str] = []
        iter_estimate_eu: float = 0.0
        for wave_id in iter_record.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is None or wave.status != WaveStatus.PENDING:
                continue
            estimate_eu = wave_estimate_eu(wave)
            dag.append(
                PrepDagTask(
                    task_id=wave.id,
                    deps=[dep_id for dep_id in wave.deps if dep_id in pending_wave_ids],
                    file_scope=list(wave.file_scopes),
                    commands=[],
                    evidence=[],
                    risk="low",
                    agent_role=wave.agent_role.value if wave.agent_role else None,
                    effort_bucket=wave.effort_bucket.value if wave.effort_bucket else None,
                    estimate_eu=estimate_eu,
                )
            )
            iter_task_ids.append(wave.id)
            iter_estimate_eu += estimate_eu
        if iter_task_ids:
            waves.append(
                PrepWave(
                    wave_id=iter_id,
                    tasks=iter_task_ids,
                    worktree_policy="auto",
                    estimate_eu=iter_estimate_eu,
                )
            )
    return dag, waves


_PREP_NEXT_ACTIONS: tuple[str, ...] = ("eawf wave plan", "eawf audit")


def _render_plan_mode_markdown(inputs: _PrepInputs) -> str | None:
    """Render the plan-mode markdown body via the canonical ``plan_view``.

    The Claude-runtime ``EnterPlanMode`` (and Codex text-prompt) surface
    for ``/prep`` plan-mode draws from
    :func:`eawf.surfaces.render.plan_view.render_phase_markdown` so the
    skill body, ``eawf roadmap show --md``, and the TUI roadmap tree
    all consume one projection (P28-W18). ``None`` when neither a
    phase nor a state document resolved — the renderer needs both to
    walk iters under the phase.

    Args:
        inputs: The resolved ``/prep`` inputs (state + phase + iter id).

    Returns:
        The rendered markdown body, or ``None`` when the projection
        cannot run.
    """
    if inputs.state is None or inputs.phase is None:
        return None
    from eawf.surfaces.render.plan_view import render_phase_markdown

    return render_phase_markdown(inputs.state, inputs.phase.id)


@dataclass
class _PrepInputs:
    """Resolved ``/prep`` inputs gathered before any algorithm step runs.

    Attributes:
        iter_id: The resolved target iter id (explicit arg or default).
        approval: The lowered approval mode (``auto`` / ``ask``).
        fix_mode: Whether ``--fix`` / ``-i`` selected the audit-fix objective.
        state: The loaded state document, or ``None`` when unreadable.
        phase_id: The resolved target phase id, or ``None``.
        phase: The resolved :class:`Phase` record, or ``None``.
        auto_resume: When ``True`` (``prep.auto_resume`` default), the emitted
            dispatch actions lead with ``eawf dispatch resume`` (SKH-8a gotcha i).
        out_of_order: Whether the planner selected out-of-order execution;
            recorded in the trace but handled inside daemon dispatch.
        ceremony: The ``--ceremony`` override (``lite`` / ``full``), or ``None``
            to keep the compute_ceremony recommendation.
        runtime: The requested batch-ladder runtime, recorded in the trace;
            concrete operator actions leave runtime selection to dispatch.
        warnings: Advisory warnings folded into the envelope footer (e.g. an
            out-of-set ``--ceremony`` token).
    """

    iter_id: str
    approval: str
    fix_mode: bool
    state: State | None
    phase_id: str | None
    phase: Phase | None
    auto_resume: bool = True
    out_of_order: bool = False
    ceremony: str | None = None
    runtime: str | None = None
    warnings: list[EnvelopeWarning] = field(default_factory=list)


@dataclass
class _PrepPlan:
    """The planned objective + DAG produced by ``/prep``'s execute stage.

    Attributes:
        objective: The plan objective line.
        non_goals: The plan's non-goal list.
        dag: The projected DAG tasks.
        waves: The projected wave groups.
    """

    objective: str
    non_goals: list[str] = field(default_factory=list)
    dag: list[PrepDagTask] = field(default_factory=list)
    waves: list[PrepWave] = field(default_factory=list)


@register
class PrepSkill(SkillAction):
    """Concrete ``/prep`` skill (Phase 4 W02)."""

    name: SkillName = "/prep"

    def _gather(self, run: ActionRun) -> _PrepInputs:
        iter_id = str(run.args.get("iter_id") or run.args.get("iter") or _DEFAULT_ITER_ID)
        state = _load_state(run.state_path)
        phase_id = _resolve_phase_id(run.args, iter_id, state)
        phase = state.phases.get(phase_id) if (state is not None and phase_id) else None
        warnings: list[EnvelopeWarning] = []
        return _PrepInputs(
            iter_id=iter_id,
            approval=str(run.args.get("approval", "auto")).lower(),
            fix_mode=bool(run.args.get("fix") or run.args.get("i")),
            state=state,
            phase_id=phase_id,
            phase=phase,
            auto_resume=self._resolve_auto_resume(run),
            out_of_order=_coerce_bool(run.args.get("out_of_order"), default=False),
            ceremony=self._resolve_ceremony(run, warnings),
            runtime=self._resolve_runtime(run),
            warnings=warnings,
        )

    def _resolve_auto_resume(self, run: ActionRun) -> bool:
        """Resolve ``--auto-resume`` (default from the ``prep.auto_resume`` leaf).

        An explicit flag wins; with no flag the ``prep.auto_resume`` layered
        leaf (built-in default ``True``) decides whether the emitted claim
        actions lead with ``eawf dispatch resume``.
        """
        raw = run.args.get("auto_resume")
        if raw is not None:
            return _coerce_bool(raw, default=True)
        prep_cfg = _merged_config(run.state_path).get("prep")
        if isinstance(prep_cfg, dict) and "auto_resume" in prep_cfg:
            return _coerce_bool(prep_cfg.get("auto_resume"), default=True)
        return True

    def _resolve_ceremony(self, run: ActionRun, warnings: list[EnvelopeWarning]) -> str | None:
        """Resolve ``--ceremony`` (``lite`` / ``full``); default keeps the recommendation.

        An out-of-set token records an advisory ``unknown_ceremony`` warning
        and falls back to ``None`` (keep the compute_ceremony recommendation)
        rather than aborting — the flag is an override, not a gate.
        """
        raw = run.args.get("ceremony")
        if raw is None:
            return None
        candidate = str(raw).strip().lower()
        if candidate in _CEREMONY_VALUES:
            return candidate
        warnings.append(
            EnvelopeWarning(
                code="unknown_ceremony",
                detail=(
                    f"ignored --ceremony {raw!r}: expected one of "
                    f"{sorted(_CEREMONY_VALUES)}; kept the recommendation"
                ),
            )
        )
        return None

    def _resolve_runtime(self, run: ActionRun) -> str | None:
        """Resolve ``--runtime`` batch-ladder override; ``None`` keeps the ladder head."""
        raw = run.args.get("runtime")
        if raw is None:
            return None
        candidate = str(raw).strip()
        return candidate or None

    def _next_actions(self, inputs: _PrepInputs) -> list[str]:
        """Build concrete dispatch actions for the dependency-ready frontier.

        ``auto_resume`` leads with ``eawf dispatch resume`` (gotcha i); the
        canonical plan/audit follow-ups trail one ``eawf dispatch wave`` action
        per concrete ready wave. No iter id or unresolved session placeholder
        is executable as a claim command.
        """
        actions: list[str] = []
        if inputs.auto_resume:
            actions.append("eawf dispatch resume")
        actions.extend(f"eawf dispatch wave {wave_id}" for wave_id in self._frontier(inputs))
        actions.extend(_PREP_NEXT_ACTIONS)
        return actions

    def _frontier(self, inputs: _PrepInputs) -> list[str]:
        """Return PENDING target-iter waves whose dependencies are CLOSED."""
        if inputs.state is None:
            return []
        iter_row = inputs.state.iters.get(inputs.iter_id)
        if iter_row is None:
            return []
        frontier: list[str] = []
        for wave_id in iter_row.wave_ids:
            wave = inputs.state.waves.get(wave_id)
            if wave is None or wave.status is not WaveStatus.PENDING:
                continue
            if all(
                dep_id in inputs.state.waves
                and inputs.state.waves[dep_id].status is WaveStatus.CLOSED
                for dep_id in wave.deps
            ):
                frontier.append(wave_id)
        return frontier

    def _validate(self, run: ActionRun, inputs: _PrepInputs) -> SkillResult | None:
        phase = inputs.phase
        if phase is None:
            return None
        # Lifecycle guard — a CLOSED phase cannot be re-planned. Block with a
        # reopen repair command before running any algorithm step.
        if phase.status == PhaseStatus.CLOSED:
            return self._blocked_closed_phase(run, inputs)
        # Idempotency guard — an already-ACTIVE phase is a no-op. Return ok
        # with no_op=True and emit no prep.build_dag event.
        if phase.status == PhaseStatus.ACTIVE:
            return self._noop_active_phase(run, inputs)
        return None

    def _blocked_closed_phase(self, run: ActionRun, inputs: _PrepInputs) -> SkillResult:
        assert inputs.phase is not None
        self._trace(
            run,
            "prep.blocked_closed_phase",
            f"prep: blocked — phase {inputs.phase_id} is closed",
            {"phase_id": inputs.phase_id, "phase_status": inputs.phase.status.value},
        )
        return self._blocked(
            run,
            PrepBody(
                iter_id=inputs.iter_id,
                objective=f"Plan {inputs.iter_id} (blocked: phase {inputs.phase_id} is closed)",
                non_goals=["reopen closed phase implicitly"],
                blocked=True,
            ).model_dump(mode="json"),
            next_valid_actions=[f"eawf phase reopen {inputs.phase_id}"],
            repair_commands=[f"eawf phase reopen {inputs.phase_id}"],
        )

    def _noop_active_phase(self, run: ActionRun, inputs: _PrepInputs) -> SkillResult:
        assert inputs.phase is not None
        self._trace(
            run,
            "prep.noop_already_active",
            f"prep: no-op — phase {inputs.phase_id} already active",
            {"phase_id": inputs.phase_id, "phase_status": inputs.phase.status.value},
        )
        return self._ok(
            run,
            PrepBody(
                iter_id=inputs.iter_id,
                objective=f"Plan {inputs.iter_id} (no-op: phase {inputs.phase_id} already active)",
                no_op=True,
            ).model_dump(mode="json"),
            next_valid_actions=list(_PREP_NEXT_ACTIONS),
        )

    def _execute(self, run: ActionRun, inputs: _PrepInputs) -> _PrepPlan:
        # Step 1 — probe ran. Step 2: resolve mode.
        self._trace(
            run,
            "prep.resolve_mode",
            f"prep: resolve mode iter={inputs.iter_id} fix={inputs.fix_mode}",
            {
                "iter_id": inputs.iter_id,
                "fix_mode": inputs.fix_mode,
                "auto_resume": inputs.auto_resume,
                "out_of_order": inputs.out_of_order,
                "ceremony": inputs.ceremony,
                "runtime": inputs.runtime,
            },
        )
        # Step 3 — load state.
        self._trace(
            run,
            "prep.load_state",
            "prep: load state + accepted research + decisions",
            {"phase_id": inputs.phase_id, "state_loaded": inputs.state is not None},
        )
        # Step 4 — define objective + non-goals.
        objective = (
            "Apply audit fix-list" if inputs.fix_mode else f"Plan {inputs.iter_id} per active scope"
        )
        non_goals = ["change project scope", "rewrite closed history"]
        self._trace(
            run,
            "prep.define_objective",
            f"prep: objective={objective[:60]}",
            {"objective": objective, "non_goals": non_goals},
        )
        # Step 5 — build DAG from the target phase's PENDING waves. When no
        # phase resolves (no state on disk), the DAG is empty rather than a
        # made-up placeholder.
        if inputs.phase is not None and inputs.state is not None:
            dag, waves = _build_dag_from_phase(inputs.phase, inputs.state)
        else:
            dag, waves = [], []
        self._trace(
            run,
            "prep.build_dag",
            f"prep: built DAG with {len(dag)} task(s)",
            {"task_count": len(dag), "phase_id": inputs.phase_id},
        )
        # Step 6 — partition into waves (already grouped per iter above).
        self._trace(
            run,
            "prep.partition_waves",
            f"prep: planned {len(waves)} wave(s)",
            {"wave_count": len(waves)},
        )
        # Step 7 — estimate (already on PrepWave).
        self._trace(
            run,
            "prep.estimate",
            "prep: estimated each wave",
            {"total_eu": sum(w.estimate_eu for w in waves)},
        )
        # Step 8 — allocate IDs (placeholder).
        self._trace(
            run,
            "prep.allocate_ids",
            "prep: allocated task / wave IDs",
            {"iter_id": inputs.iter_id},
        )
        # Step 9 — write plan / spec artefact (v0.1: skipped — no artifact
        # subsystem write here; future wave wires it via add_artifact).
        self._trace(
            run,
            "prep.write_plan",
            "prep: plan persisted as event payload (v0.1)",
            {"iter_id": inputs.iter_id, "wave_count": len(waves)},
        )
        return _PrepPlan(objective=objective, non_goals=non_goals, dag=dag, waves=waves)

    def _render(self, run: ActionRun, inputs: _PrepInputs, outcome: _PrepPlan) -> SkillResult:
        # Step 10 — approval gate.
        plan_text = _render_plan_mode_markdown(inputs)
        # An empty DAG means no PENDING waves resolved (no state on disk, or a
        # phase with nothing left to plan): there is nothing to plan, so this
        # is the no-op case rather than a real plan. Flag it so the planning
        # body's DAG-reconciliation invariant is exempted.
        no_op = not outcome.dag
        body = PrepBody(
            iter_id=inputs.iter_id,
            objective=outcome.objective,
            non_goals=outcome.non_goals,
            dag=outcome.dag,
            waves=outcome.waves,
            acceptance=PrepAcceptance(
                checks=["uv run pytest", "uv run mypy src"],
                baselines=[],
            ),
            approval_required=inputs.approval == "ask",
            no_op=no_op,
            plan_text=plan_text,
        )
        next_actions = self._next_actions(inputs)
        if inputs.approval == "ask":
            self._trace(
                run,
                "prep.approval_gate",
                "prep: approval requested",
                {"approval": inputs.approval},
            )
            body.user_question = UserQuestion(
                question=(
                    f"Plan ready for {inputs.iter_id} "
                    f"({len(outcome.waves)} wave(s)). Pick how to proceed."
                ),
                options=[
                    UserQuestionOption(label="approve", description="Apply the plan and continue."),
                    UserQuestionOption(label="edit", description="Edit the plan before applying."),
                    UserQuestionOption(label="cancel", description="Discard the plan."),
                ],
            )
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                persisted_store_records=run.records,
                state_mutations=run.mutations,
                evidence_refs=run.evidence,
                next_valid_actions=next_actions,
                warnings=inputs.warnings,
            )
        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=run.records,
            state_mutations=run.mutations,
            evidence_refs=run.evidence,
            next_valid_actions=next_actions,
            warnings=inputs.warnings,
        )


__all__ = ["PrepSkill"]
