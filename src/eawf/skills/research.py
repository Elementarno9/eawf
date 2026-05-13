"""``/research`` skill — investigate questions and produce a peer-reviewed brief.

Implements the ``/research`` algorithm per ``docs/architecture/workflow.md``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort if hard requirement
   missing.
2. Resolve scope: explicit message, else active iter unknowns/blockers.
3. Detect continuation: open brief on the same scope → load and extend.
4. Define questions: facts to verify, options to compare, risks to audit,
   decision needed.
5. Dispatch parallel read-only agents (v0.1: degrade to user_question
   placeholder).
6. Synthesize options: 2-4 solutions with tradeoffs/complexity/etc.
7. Review findings: cross-check citations.
8. Recommend one path with confidence and fallback.
9. Persist the brief artifact when ``-f`` / ``research.auto_save=true``.
10. Record artefact / decision candidates in state.

Each algorithm step writes one row to ``store/event.jsonl`` via
:func:`eawf.skills._common.emit_event`. Heavy LLM-fanout steps degrade to
``status=needs_user`` with a typed :class:`UserQuestion` populated on the
body, per the design spec §14 degrade pattern.

Honoured flags (per the W02 acceptance contract):

- ``--depth quick|normal|deep`` — passed via ``ctx.args["depth"]``;
  controls the number of synthesised question slots and the body's
  ``recommendation.confidence`` default.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from eawf.artifacts.references import Citation
from eawf.render.envelope import SkillName
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.blitz import BlitzSkill, should_auto_invoke
from eawf.skills.bodies.research import (
    ResearchBody,
    ResearchOption,
    ResearchQuestion,
    ResearchRecommendation,
)
from eawf.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult, run_skill
from eawf.skills.registry import register
from eawf.state.enums import StoreKind
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.research import ResearchPayload
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)


_VALID_DEPTHS: tuple[str, ...] = ("quick", "normal", "deep")
_DEFAULT_DEPTH: str = "normal"


def _bool_arg(args: dict[str, Any], *names: str, default: bool = False) -> bool:
    """Return a bool-ish skill arg from the first present key."""
    for name in names:
        if name not in args:
            continue
        raw = args[name]
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    return default


def _depth_to_question_slots(depth: str) -> int:
    """Map ``--depth`` to a synthetic question slot count.

    The skill's v0.1 implementation cannot dispatch the parallel reviewer
    fanout described in §14 step 5, so it pre-allocates question slots
    sized by depth: more depth → more slots → richer body.
    """
    if depth == "quick":
        return 1
    if depth == "deep":
        return 3
    return 2  # normal


def _research_brief_urn(brief_id: str) -> str:
    """Return the public URN for a persisted research brief."""
    return f"urn:eawf:v1:store:research/{brief_id}"


def _append_research_brief(
    *,
    state_path: Any,
    scope_id: str,
    brief_id: str,
    topic: str,
    questions: list[ResearchQuestion],
) -> str:
    """Append one research-store record and return its public URN."""
    now = datetime.now(UTC)
    findings = [f"{q.q} {q.answer}".strip() for q in questions]
    sources = sorted({src for q in questions for src in q.sources})
    references = [
        Citation.from_legacy_source(i, source) for i, source in enumerate(sources, start=1)
    ]
    payload = ResearchPayload(topic=topic, findings=findings, references=references)
    envelope = Envelope(
        schema_version="1.0",
        id=brief_id,
        kind=StoreKind.RESEARCH,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=f"research brief {brief_id}: {topic[:120]}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(state_path, StoreKind.RESEARCH), envelope)
    return _research_brief_urn(brief_id)


@register
class ResearchSkill(Skill):
    """Concrete ``/research`` skill (Phase 4 W02)."""

    name: SkillName = "/research"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)
        raw_depth = str(args.get("depth", _DEFAULT_DEPTH))
        depth = raw_depth if raw_depth in _VALID_DEPTHS else _DEFAULT_DEPTH
        topic = str(args.get("topic") or args.get("message") or scope_id)
        final_requested = _bool_arg(args, "final", "save", default=False)
        blitz_enabled = _bool_arg(args, "blitz", default=True)

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf prep", "eawf hypothesis define"]
        evidence_refs: list[str] = []

        # Step 1 — probe already ran. Step 2: resolve scope.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="research.resolve_scope",
            summary=f"research: resolve scope ({depth})",
            payload={"depth": depth, "scope_id": scope_id},
        )
        persisted_records.append(evt_id)

        # Step 3 — continuation detection. v0.1 always starts a fresh brief.
        brief_id = f"BR-{uuid.uuid4().hex[:8].upper()}"
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="research.start_brief",
            summary=f"research: start brief {brief_id}",
            payload={"brief_id": brief_id},
        )
        persisted_records.append(evt_id)

        # Step 4 — define questions. v0.1 emits placeholder slots scaled by depth.
        question_count = _depth_to_question_slots(depth)
        questions = [
            ResearchQuestion(
                q=f"Open question #{i + 1} for scope {scope_id}",
                answer="(awaiting agent fanout)",
                confidence="low",
                sources=[],
            )
            for i in range(question_count)
        ]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="research.define_questions",
            summary=f"research: defined {len(questions)} question(s)",
            payload={"count": len(questions), "depth": depth},
        )
        persisted_records.append(evt_id)

        # Step 5 — dispatch parallel agents. v0.1: degrade to needs_user with
        # a typed user_question so the runtime / human can supply the agent
        # plan before re-invocation.
        if depth == "deep":
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="research.fanout_pending",
                summary="research: deep depth requires explicit fanout decision",
                payload={"depth": depth},
            )
            persisted_records.append(evt_id)
            return SkillResult(
                status="needs_user",
                body=ResearchBody(
                    brief_id=brief_id,
                    questions=questions,
                    options=[],
                    recommendation=None,
                    peer_review=None,
                    persisted_brief=None,
                    user_question=UserQuestion(
                        question=(
                            f"Deep research on {scope_id} needs a fanout plan. Pick how to proceed."
                        ),
                        options=[
                            UserQuestionOption(
                                label="proceed_default",
                                description="Use the built-in three-agent fanout.",
                            ),
                            UserQuestionOption(
                                label="adjust_agents",
                                description="Adjust agent assignments before fanout.",
                            ),
                            UserQuestionOption(
                                label="cancel",
                                description="Abort the research run.",
                            ),
                        ],
                    ),
                ).model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                evidence_refs=evidence_refs,
                next_valid_actions=next_actions,
            )

        # Step 6 — synthesise options (v0.1 placeholder pair).
        options = [
            ResearchOption(
                name=f"option-{i + 1}",
                tradeoffs="(needs evidence)",
                complexity="low" if i == 0 else "medium",
                reversibility="high" if i == 0 else "medium",
                risks=[],
            )
            for i in range(2)
        ]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="research.synthesize_options",
            summary=f"research: synthesised {len(options)} option(s)",
            payload={"count": len(options)},
        )
        persisted_records.append(evt_id)

        # Step 7 — peer review (v0.1: skipped, leaves peer_review=None).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="research.peer_review",
            summary="research: peer review skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        # Step 8 — recommend.
        recommendation = ResearchRecommendation(
            choice=options[0].name,
            confidence="medium" if depth == "normal" else "low",
            fallback=options[1].name,
        )
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="research.recommend",
            summary=f"research: recommended {recommendation.choice}",
            payload={"choice": recommendation.choice},
        )
        persisted_records.append(evt_id)

        persisted_brief: str | None = None
        if final_requested:
            persisted_brief = _append_research_brief(
                state_path=state_path,
                scope_id=scope_id,
                brief_id=brief_id,
                topic=topic,
                questions=questions,
            )
            persisted_records.append(persisted_brief)

        residual_unknowns = sum(1 for q in questions if "awaiting" in q.answer.lower())
        if blitz_enabled and should_auto_invoke(residual_unknowns=residual_unknowns):
            blitz_ctx = SkillContext(
                scope=ctx.scope,
                session=ctx.session,
                instrument_probe=dict(ctx.instrument_probe),
                args={
                    "residual_unknowns": residual_unknowns,
                    "followup_research_args": {
                        "topic": topic,
                        "depth": "quick",
                        "blitz": False,
                    },
                },
            )
            blitz_env = run_skill(BlitzSkill(), blitz_ctx)
            next_actions.append("eawf skill run /blitz")
            next_actions.extend(blitz_env.footer.next_valid_actions)
            if blitz_env.header.status != "ok":
                return SkillResult(
                    status=blitz_env.header.status,
                    body=ResearchBody(
                        brief_id=brief_id,
                        questions=questions,
                        options=options,
                        recommendation=recommendation,
                        peer_review=None,
                        persisted_brief=persisted_brief,
                    ).model_dump(mode="json"),
                    persisted_store_records=persisted_records,
                    state_mutations=state_mutations,
                    evidence_refs=evidence_refs,
                    next_valid_actions=next_actions,
                    repair_commands=blitz_env.footer.repair_commands
                    or ["rerun /research with blitz=false"],
                )

        # Step 9 — persist brief when explicitly requested.
        # Step 10 — record decisions / artefacts (v0.1: omitted; the audit
        # path captures verdicts).

        body = ResearchBody(
            brief_id=brief_id,
            questions=questions,
            options=options,
            recommendation=recommendation,
            peer_review=None,
            persisted_brief=persisted_brief,
        )

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            evidence_refs=evidence_refs,
            next_valid_actions=next_actions,
        )


__all__ = ["ResearchSkill"]
