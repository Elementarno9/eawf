"""``/roadmap`` skill — propose or extend the steady Eä roadmap.

Implements the ``/roadmap`` algorithm per ``docs/architecture/workflow.md``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``.
2. Resolve scope: workspace, repo, project, subproject, explicit goal
   set, or active state.
3. Load current state: goals, outcomes, phases, iters, decisions, …
4. Research context (LLM-fanout — v0.1 stubs).
5. Propose steady roadmap: goals, outcomes, phase sequence, candidate
   iters, dependencies, audit gates, risks, low/medium-confidence phase
   estimate envelopes.
6. If roadmap exists, propose extension/revision (append / split / merge
   / mark stale) without deleting history.
7. Ask user to approve, edit, research more, apply partial, defer, or
   stop. v0.1 degrades to ``status=needs_user`` with a typed
   :class:`UserQuestion` whenever ``ctx.args["approval"] == "ask"``.
8. Apply approved roadmap through Eä CLI only (skipped in v0.1 — the
   approval path lands first; CLI dispatch is a follow-up wave).
9. Record decision rationale, evidence refs, roadmap artefact, estimate
   basis, and next suggested action.

Each algorithm step writes one ``EVENT`` row to ``store/event.jsonl``
via :func:`eawf.skills._common.emit_event`. Heavy LLM-fanout steps
degrade to ``status=needs_user`` with a typed :class:`UserQuestion`
populated on the body, per the design spec §14 degrade pattern.

Honoured ``ctx.args`` keys:

- ``horizon`` — ``"short"|"medium"|"long"``; controls the candidate slot
  count.
- ``approval`` — ``"ask"|"auto"``; ``"ask"`` short-circuits to
  ``status=needs_user``.
- ``extend`` / ``revise`` — bool flags that bias the body's
  ``chosen_order`` (revise prepends a stale-marker entry).
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
from eawf.skills.bodies.roadmap import RoadmapBody, RoadmapItem
from eawf.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register

logger = logging.getLogger(__name__)


_VALID_HORIZONS: tuple[str, ...] = ("short", "medium", "long")
_DEFAULT_HORIZON: str = "medium"


def _horizon_to_slot_count(horizon: str) -> int:
    """Map ``--horizon`` to a synthetic candidate slot count.

    Mirrors the depth-to-question mapping used by ``/research``: short
    horizons get a single candidate, medium gets three, long gets five.
    The numbers stay small so the v0.1 placeholder body remains readable.
    """
    if horizon == "short":
        return 1
    if horizon == "long":
        return 5
    return 3  # medium


def _build_approval_question(horizon: str, candidate_count: int) -> UserQuestion:
    """Render the canonical roadmap-approval :class:`UserQuestion`."""
    return UserQuestion(
        question=(
            f"Roadmap proposal ready ({candidate_count} candidate(s), horizon={horizon})."
            f" Pick how to proceed."
        ),
        options=[
            UserQuestionOption(
                label="approve",
                description="Apply the roadmap as proposed.",
            ),
            UserQuestionOption(
                label="edit",
                description="Edit the candidate list before applying.",
            ),
            UserQuestionOption(
                label="research_more",
                description="Run more research before applying.",
            ),
            UserQuestionOption(
                label="defer",
                description="Defer the roadmap; keep current state.",
            ),
        ],
    )


@register
class RoadmapSkill(Skill):
    """Concrete ``/roadmap`` skill (Phase 4 W03).

    v0.1 implementation: each §14 step writes one ``EVENT`` row and the
    body is populated with placeholder candidates scaled by horizon. The
    LLM-fanout step (4) and approval gate (7) degrade to
    ``status=needs_user`` with a typed :class:`UserQuestion`.
    """

    name: SkillName = "/roadmap"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        raw_horizon = str(args.get("horizon", _DEFAULT_HORIZON))
        horizon = raw_horizon if raw_horizon in _VALID_HORIZONS else _DEFAULT_HORIZON
        approval = str(args.get("approval", "auto")).lower()
        extend = bool(args.get("extend"))
        revise = bool(args.get("revise"))

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf prep", "eawf research", "eawf differentiate"]

        # Step 2 — resolve scope.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.resolve_scope",
            summary=f"roadmap: resolve scope ({horizon})",
            payload={"horizon": horizon, "scope_id": scope_id},
        )
        persisted_records.append(evt_id)

        # Step 3 — load state.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.load_state",
            summary="roadmap: load goals + outcomes + phases",
            payload={"extend": extend, "revise": revise},
        )
        persisted_records.append(evt_id)

        # Step 4 — research context (LLM fanout). v0.1 records a stub
        # event; the candidates below carry placeholder rationale.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.research_context",
            summary="roadmap: research context skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        # Step 5 — propose roadmap candidates.
        slot_count = _horizon_to_slot_count(horizon)
        candidates: list[RoadmapItem] = [
            RoadmapItem(
                item_id=f"R-{i + 1:02d}",
                title=f"Candidate phase #{i + 1} ({horizon})",
                rationale="(needs research evidence)",
                priority="medium" if i == 0 else "low",
                estimate_eu=None,
            )
            for i in range(slot_count)
        ]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.propose",
            summary=f"roadmap: proposed {len(candidates)} candidate(s)",
            payload={"count": len(candidates), "horizon": horizon},
        )
        persisted_records.append(evt_id)

        # Step 6 — extend/revise tag.
        chosen_order = [c.item_id for c in candidates]
        if revise and chosen_order:
            chosen_order = [f"REVISE:{chosen_order[0]}", *chosen_order]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.extend_or_revise",
            summary=f"roadmap: extend={extend} revise={revise}",
            payload={"extend": extend, "revise": revise},
        )
        persisted_records.append(evt_id)

        body = RoadmapBody(
            horizon=horizon,
            candidates=candidates,
            chosen_order=chosen_order,
            user_question=None,
        )

        # Step 7 — approval gate. ``approval=ask`` flips the envelope to
        # status=needs_user with a typed UserQuestion.
        if approval == "ask":
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="roadmap.approval_gate",
                summary="roadmap: approval requested",
                payload={"approval": approval},
            )
            persisted_records.append(evt_id)
            body.user_question = _build_approval_question(horizon, len(candidates))
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
            )

        # Step 8 — apply (skipped in v0.1) + Step 9 — record next action.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.apply",
            summary="roadmap: apply skipped in v0.1 (CLI dispatch follow-up)",
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


__all__ = ["RoadmapSkill"]
