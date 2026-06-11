"""``/differentiate`` skill — generate project-specialised agent definitions.

Implements the ``/differentiate`` algorithm per
``docs/architecture/workflow.md``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``.
2. Resolve scope: workspace, repo, project, track, phase, or
   explicit user request.
3. Inspect existing Eä agents, profile agents, runtime agents, languages,
   frameworks, architecture, tests, docs, and recurring work types.
4. Propose desired agent set before writing: roles, count, runtime
   targets, model/tool permissions, read/write access, memory policy,
   worktree policy, and naming.
5. Ask user to choose options: minimal/adaptive/full, read-only vs writer
   agents, …, replace or extend existing agents.
6. Draft agent definitions by adapting Eä baselines.
7. Validate with ``/agent-lint`` (v0.1: skipped).
8. Render approved agents (v0.1: skipped — degrade pattern).

v0.1 implementation: each step writes one ``EVENT`` row to
``store/event.jsonl``. The body's ``axes`` list is populated with
placeholder comparisons scaled by ``ctx.args["preset"]``. Heavy
LLM-fanout (steps 3-4) and the approval gate (step 5) degrade to
``status=needs_user`` with a typed :class:`UserQuestion` per design
spec §14 degrade pattern.

Honoured ``ctx.args`` keys:

- ``preset`` — ``"minimal"|"adaptive"|"full"``; controls the comparison
  axes count.
- ``approval`` — ``"ask"|"auto"``; ``"ask"`` flips to needs_user.
- ``runtime`` — ``"claude"|"opencode"|"all"``; recorded but not yet
  branched.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.workflow.skills.bodies.differentiate import DifferentiateAxis, DifferentiateBody
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


_VALID_PRESETS: tuple[str, ...] = ("minimal", "adaptive", "full")
_DEFAULT_PRESET: str = "adaptive"


def _preset_to_axes_count(preset: str) -> int:
    """Map ``--preset`` to a synthetic comparison-axes count.

    ``minimal`` → one axis; ``adaptive`` → three; ``full`` → five. Mirrors
    the depth/horizon scaling in the sibling meta skills so the bodies
    stay deterministic and readable.
    """
    if preset == "minimal":
        return 1
    if preset == "full":
        return 5
    return 3  # adaptive


def _build_approval_question(preset: str, axes_count: int) -> UserQuestion:
    """Render the canonical agent-set approval :class:`UserQuestion`."""
    return UserQuestion(
        question=(
            f"Specialised agent set ready ({axes_count} axis/axes, preset={preset})."
            f" Pick how to proceed."
        ),
        options=[
            UserQuestionOption(
                label="approve",
                description="Render the proposed agent set.",
            ),
            UserQuestionOption(
                label="edit",
                description="Edit the proposal before rendering.",
            ),
            UserQuestionOption(
                label="replace_existing",
                description="Replace existing agents instead of extending.",
            ),
            UserQuestionOption(
                label="cancel",
                description="Abort the differentiate run.",
            ),
        ],
    )


@register
class DifferentiateSkill(Skill):
    """Concrete ``/differentiate`` skill (Phase 4 W03).

    v0.1 implementation: persist a placeholder body whose axes list is
    sized by preset. Heavy LLM-fanout steps (inspect existing agents,
    propose agent set) emit a stub event and the approval gate degrades
    to ``status=needs_user`` with a typed :class:`UserQuestion`.
    """

    name: SkillName = "/differentiate"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        raw_preset = str(args.get("preset", _DEFAULT_PRESET))
        preset = raw_preset if raw_preset in _VALID_PRESETS else _DEFAULT_PRESET
        approval = str(args.get("approval", "auto")).lower()
        runtime_target = str(args.get("runtime", "all"))

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf prep", "eawf research"]

        # Step 2 — resolve scope.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="differentiate.resolve_scope",
            summary=f"differentiate: resolve scope ({preset})",
            payload={"preset": preset, "scope_id": scope_id, "runtime": runtime_target},
        )
        persisted_records.append(evt_id)

        # Step 3 — inspect existing agents (LLM fanout). v0.1 stub.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="differentiate.inspect_agents",
            summary="differentiate: inspect existing agents skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        # Step 4 — propose agent set.
        axes_count = _preset_to_axes_count(preset)
        axes: list[DifferentiateAxis] = [
            DifferentiateAxis(
                name=f"axis-{i + 1}",
                current="(needs introspection)",
                peers=["baseline-eawf"],
                advantage=None,
            )
            for i in range(axes_count)
        ]
        conclusions = [f"Adopt {preset}-preset agent set ({axes_count} axis/axes)"]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="differentiate.propose_agent_set",
            summary=f"differentiate: proposed {len(axes)} axis/axes",
            payload={"count": len(axes), "preset": preset},
        )
        persisted_records.append(evt_id)

        body = DifferentiateBody(
            target_scope=scope_id,
            axes=axes,
            conclusions=conclusions,
            user_question=None,
        )

        # Step 5 — approval gate.
        if approval == "ask":
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="differentiate.approval_gate",
                summary="differentiate: approval requested",
                payload={"approval": approval},
            )
            persisted_records.append(evt_id)
            body.user_question = _build_approval_question(preset, len(axes))
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
            )

        # Step 6 — draft agent definitions (v0.1: skipped).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="differentiate.draft_agents",
            summary="differentiate: draft agents skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        # Step 7 — render approved agents (v0.1: skipped).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="differentiate.render",
            summary="differentiate: render skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            next_valid_actions=next_actions,
        )


__all__ = ["DifferentiateSkill"]
