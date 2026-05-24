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
  count on the placeholder propose path.
- ``approval`` — ``"ask"|"auto"``; ``"ask"`` short-circuits to
  ``status=needs_user`` so the AUQ gates the (apply or propose) plan.
- ``extend`` / ``revise`` — bool flags that bias the body's
  ``chosen_order`` (revise prepends a stale-marker entry on the propose
  path only).
- ``phase`` / ``phase_id`` — when set (or ``state.current.phase_id``
  resolves) and the target phase has PENDING waves, the body is prefilled
  with the wave DAG so applying lands the waves PENDING with their deps.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import Phase, State
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


def _load_state(state_path: Path) -> State | None:
    """Return the validated :class:`State`, or ``None`` when unreadable.

    The read is best-effort and read-only (rule 4: the daemon is the sole
    mutator; reads are free). A missing file, malformed JSON, or schema
    mismatch all degrade to ``None`` so the skill falls back to the
    placeholder-candidate path rather than crashing.

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


def _resolve_phase(args: dict[str, Any], state: State | None) -> Phase | None:
    """Resolve the target :class:`Phase` for an apply-prefill invocation.

    Precedence: an explicit ``phase`` / ``phase_id`` arg, then
    ``state.current.phase_id``. Returns ``None`` when no state is loaded or
    the resolved id is absent from ``state.phases`` — callers degrade to the
    placeholder-candidate path in that case.

    Args:
        args: The skill's parsed arg dict.
        state: The loaded state, or ``None`` when no state file is present.

    Returns:
        The target phase record, or ``None`` when none resolves.
    """
    if state is None:
        return None
    explicit = args.get("phase_id") or args.get("phase")
    phase_id = str(explicit) if explicit else state.current.phase_id
    if not phase_id:
        return None
    return state.phases.get(phase_id)


def _prefill_wave_candidates(phase: Phase, state: State) -> list[RoadmapItem]:
    """Project a phase's PENDING waves into ordered roadmap candidates.

    Walks ``phase.iter_ids`` in order; for each iter walks ``iter.wave_ids``
    and emits one :class:`RoadmapItem` per wave whose status is
    :attr:`WaveStatus.PENDING`. The wave id, title, and dep summary are
    surfaced so applying the plan shows the waves landing PENDING with their
    deps. The first wave of each iter ranks ``high``; the rest ``medium`` so
    the chosen order reads as a frontier-first DAG.

    Args:
        phase: The target phase record.
        state: The loaded state holding the ``iters`` / ``waves`` maps.

    Returns:
        Candidate rows in iter-then-wave order; empty when no PENDING wave
        exists under the phase.
    """
    candidates: list[RoadmapItem] = []
    for iter_id in phase.iter_ids:
        iter_record = state.iters.get(iter_id)
        if iter_record is None:
            continue
        first_in_iter = True
        for wave_id in iter_record.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is None or wave.status != WaveStatus.PENDING:
                continue
            deps_summary = ", ".join(wave.deps) if wave.deps else "no deps"
            candidates.append(
                RoadmapItem(
                    item_id=wave.id,
                    title=wave.title,
                    rationale=f"PENDING wave under {iter_id} ({deps_summary})",
                    priority="high" if first_in_iter else "medium",
                    estimate_eu=None,
                )
            )
            first_in_iter = False
    return candidates


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
    """Render the canonical roadmap-proposal :class:`UserQuestion`."""
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


def _build_apply_question(phase_id: str, wave_count: int) -> UserQuestion:
    """Render the apply-approval :class:`UserQuestion` for a prefilled DAG.

    The prompt names the rendered wave count so the operator gates the
    apply against the full wave DAG already populated on the body.

    Args:
        phase_id: The phase whose wave DAG is being applied.
        wave_count: Count of PENDING waves rendered on the body.

    Returns:
        A typed question with approve / revise / cancel options.
    """
    return UserQuestion(
        question=(
            f"Apply plan for {phase_id} ({wave_count} pending wave(s))."
            f" Review the wave DAG and pick how to proceed."
        ),
        options=[
            UserQuestionOption(
                label="approve",
                description="Apply the plan; waves stay PENDING with their deps.",
            ),
            UserQuestionOption(
                label="revise",
                description="Reshape the wave DAG before applying.",
            ),
            UserQuestionOption(
                label="cancel",
                description="Discard the plan; keep current state.",
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

        # Step 5 — propose candidates. When a target phase with PENDING
        # waves resolves, prefill the wave DAG (apply path); otherwise fall
        # back to horizon-scaled placeholder candidates (propose path).
        state = _load_state(state_path)
        phase = _resolve_phase(args, state)
        apply_mode = False
        candidates: list[RoadmapItem]
        if phase is not None and state is not None:
            prefilled = _prefill_wave_candidates(phase, state)
            if prefilled:
                candidates = prefilled
                apply_mode = True
        if not apply_mode:
            slot_count = _horizon_to_slot_count(horizon)
            candidates = [
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
            payload={"count": len(candidates), "horizon": horizon, "apply_mode": apply_mode},
        )
        persisted_records.append(evt_id)

        # Step 6 — extend/revise tag. In apply mode the chosen order is the
        # prefilled wave-id DAG order, so the propose-only REVISE marker is
        # not prepended.
        chosen_order = [c.item_id for c in candidates]
        if revise and chosen_order and not apply_mode:
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
        # status=needs_user with a typed UserQuestion; in apply mode the
        # question renders the prefilled wave DAG so the AUQ gates the apply.
        if approval == "ask":
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="roadmap.approval_gate",
                summary="roadmap: approval requested",
                payload={"approval": approval, "apply_mode": apply_mode},
            )
            persisted_records.append(evt_id)
            if apply_mode and phase is not None:
                body.user_question = _build_apply_question(phase.id, len(candidates))
            else:
                body.user_question = _build_approval_question(horizon, len(candidates))
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
            )

        # Step 8 — apply. In apply mode the prefilled waves are surfaced as
        # already-PENDING with their deps; the daemon (propose / revise) is
        # the canonical writer, so this step reports the projection rather
        # than mutating the wave set. Otherwise the v0.1 propose path stays
        # a no-op until CLI dispatch lands.
        if apply_mode and phase is not None:
            apply_summary = f"roadmap: applied {len(candidates)} pending wave(s) for {phase.id}"
            apply_payload: dict[str, Any] = {
                "phase_id": phase.id,
                "wave_count": len(candidates),
                "applied": True,
            }
        else:
            apply_summary = "roadmap: apply skipped in v0.1 (no resolved phase)"
            apply_payload = {"skipped": True}
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="roadmap.apply",
            summary=apply_summary,
            payload=apply_payload,
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
