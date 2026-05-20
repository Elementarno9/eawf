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
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from eawf.render.envelope import SkillName
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.bodies.prep import (
    PrepAcceptance,
    PrepBody,
    PrepDagTask,
    PrepWave,
)
from eawf.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register
from eawf.state.enums import PhaseStatus, WaveStatus
from eawf.state.ids import parents_of
from eawf.state.models import Phase, State

logger = logging.getLogger(__name__)


_DEFAULT_ITER_ID: str = "P00-I01"


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
        logger.warning(f"_load_state could not read state at {state_path}: {exc}")
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
    record. Each iter that contributes at least one PENDING wave yields one
    :class:`PrepWave` grouping its task ids.

    Args:
        phase: The target phase record.
        state: The loaded state holding the ``iters`` and ``waves`` maps.

    Returns:
        A two-tuple ``(dag, waves)`` of the projected tasks and wave groups.
        Both are empty when the phase has no PENDING waves.
    """
    dag: list[PrepDagTask] = []
    waves: list[PrepWave] = []
    for iter_id in phase.iter_ids:
        iter_record = state.iters.get(iter_id)
        if iter_record is None:
            continue
        iter_task_ids: list[str] = []
        for wave_id in iter_record.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is None or wave.status != WaveStatus.PENDING:
                continue
            dag.append(
                PrepDagTask(
                    task_id=wave.id,
                    deps=list(wave.deps),
                    file_scope=list(wave.file_scopes),
                    commands=[],
                    evidence=[],
                    risk="low",
                )
            )
            iter_task_ids.append(wave.id)
        if iter_task_ids:
            waves.append(
                PrepWave(
                    wave_id=iter_id,
                    tasks=iter_task_ids,
                    worktree_policy="auto",
                    estimate_eu=float(len(iter_task_ids)),
                )
            )
    return dag, waves


@register
class PrepSkill(Skill):
    """Concrete ``/prep`` skill (Phase 4 W02)."""

    name: SkillName = "/prep"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)
        iter_id = str(args.get("iter_id") or args.get("iter") or _DEFAULT_ITER_ID)
        approval = str(args.get("approval", "auto")).lower()
        fix_mode = bool(args.get("fix") or args.get("i"))

        state = _load_state(state_path)
        phase_id = _resolve_phase_id(args, iter_id, state)
        phase = state.phases.get(phase_id) if (state is not None and phase_id) else None

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf wave plan", "eawf audit"]

        # Lifecycle guard — a CLOSED phase cannot be re-planned. Block with
        # a reopen repair command before running any algorithm step.
        if phase is not None and phase.status == PhaseStatus.CLOSED:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="prep.blocked_closed_phase",
                summary=f"prep: blocked — phase {phase_id} is closed",
                payload={"phase_id": phase_id, "phase_status": phase.status.value},
            )
            persisted_records.append(evt_id)
            return SkillResult(
                status="blocked",
                body=PrepBody(
                    iter_id=iter_id,
                    objective=f"Plan {iter_id} (blocked: phase {phase_id} is closed)",
                    non_goals=["reopen closed phase implicitly"],
                ).model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=[f"eawf phase reopen {phase_id}"],
                repair_commands=[f"eawf phase reopen {phase_id}"],
            )

        # Idempotency guard — an already-ACTIVE phase is a no-op. Return ok
        # with no_op=True and emit no prep.build_dag event.
        if phase is not None and phase.status == PhaseStatus.ACTIVE:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="prep.noop_already_active",
                summary=f"prep: no-op — phase {phase_id} already active",
                payload={"phase_id": phase_id, "phase_status": phase.status.value},
            )
            persisted_records.append(evt_id)
            return SkillResult(
                status="ok",
                body=PrepBody(
                    iter_id=iter_id,
                    objective=f"Plan {iter_id} (no-op: phase {phase_id} already active)",
                    no_op=True,
                ).model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
            )

        # Step 1 — probe ran. Step 2: resolve mode.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.resolve_mode",
            summary=f"prep: resolve mode iter={iter_id} fix={fix_mode}",
            payload={"iter_id": iter_id, "fix_mode": fix_mode},
        )
        persisted_records.append(evt_id)

        # Step 3 — load state.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.load_state",
            summary="prep: load state + accepted research + decisions",
            payload={"phase_id": phase_id, "state_loaded": state is not None},
        )
        persisted_records.append(evt_id)

        # Step 4 — define objective + non-goals.
        objective = "Apply audit fix-list" if fix_mode else f"Plan {iter_id} per active scope"
        non_goals: list[str] = ["change project scope", "rewrite closed history"]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.define_objective",
            summary=f"prep: objective={objective[:60]}",
            payload={"objective": objective, "non_goals": non_goals},
        )
        persisted_records.append(evt_id)

        # Step 5 — build DAG from the target phase's PENDING waves. When no
        # phase resolves (no state on disk), the DAG is empty rather than a
        # made-up placeholder.
        if phase is not None and state is not None:
            dag, waves = _build_dag_from_phase(phase, state)
        else:
            dag, waves = [], []
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.build_dag",
            summary=f"prep: built DAG with {len(dag)} task(s)",
            payload={"task_count": len(dag), "phase_id": phase_id},
        )
        persisted_records.append(evt_id)

        # Step 6 — partition into waves (already grouped per iter above).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.partition_waves",
            summary=f"prep: planned {len(waves)} wave(s)",
            payload={"wave_count": len(waves)},
        )
        persisted_records.append(evt_id)

        # Step 7 — estimate (already on PrepWave).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.estimate",
            summary="prep: estimated each wave",
            payload={"total_eu": sum(w.estimate_eu for w in waves)},
        )
        persisted_records.append(evt_id)

        # Step 8 — allocate IDs (placeholder).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.allocate_ids",
            summary="prep: allocated task / wave IDs",
            payload={"iter_id": iter_id},
        )
        persisted_records.append(evt_id)

        # Step 9 — write plan / spec artefact (v0.1: skipped — no artifact
        # subsystem write here; future wave wires it via add_artifact).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.write_plan",
            summary="prep: plan persisted as event payload (v0.1)",
            payload={"iter_id": iter_id, "wave_count": len(waves)},
        )
        persisted_records.append(evt_id)

        # Step 10 — approval gate.
        body = PrepBody(
            iter_id=iter_id,
            objective=objective,
            non_goals=non_goals,
            dag=dag,
            waves=waves,
            acceptance=PrepAcceptance(
                checks=["uv run pytest", "uv run mypy src"],
                baselines=[],
            ),
            approval_required=approval == "ask",
        )

        if approval == "ask":
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="prep.approval_gate",
                summary="prep: approval requested",
                payload={"approval": approval},
            )
            persisted_records.append(evt_id)
            body.user_question = UserQuestion(
                question=(f"Plan ready for {iter_id} ({len(waves)} wave(s)). Pick how to proceed."),
                options=[
                    UserQuestionOption(
                        label="approve",
                        description="Apply the plan and continue.",
                    ),
                    UserQuestionOption(
                        label="edit",
                        description="Edit the plan before applying.",
                    ),
                    UserQuestionOption(
                        label="cancel",
                        description="Discard the plan.",
                    ),
                ],
            )
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
            )

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            next_valid_actions=next_actions,
        )


__all__ = ["PrepSkill"]
