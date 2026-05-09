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

v0.1 implementation: each step writes one ``EVENT`` row to
``store/event.jsonl`` and the body is populated with placeholder DAG /
wave skeletons. Heavy LLM-fanout (steps 4-5) degrade to
``status=needs_user`` with a typed :class:`UserQuestion` when the caller
flags ``approval=ask``.
"""

from __future__ import annotations

import logging
from typing import Any

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

logger = logging.getLogger(__name__)


_DEFAULT_ITER_ID: str = "P00-I01"


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

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf wave plan", "eawf audit"]

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
            payload={},
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

        # Step 5 — build DAG. v0.1 emits a single placeholder task so the
        # body is non-empty without making up content.
        dag = [
            PrepDagTask(
                task_id="T01",
                deps=[],
                file_scope=[],
                commands=[],
                evidence=[],
                risk="low",
            )
        ]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="prep.build_dag",
            summary=f"prep: built DAG with {len(dag)} task(s)",
            payload={"task_count": len(dag)},
        )
        persisted_records.append(evt_id)

        # Step 6 — partition into waves.
        waves = [
            PrepWave(
                wave_id=f"{iter_id}-W01",
                tasks=[t.task_id for t in dag],
                worktree_policy="auto",
                estimate_eu=1.0,
            )
        ]
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
