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
:func:`eawf.workflow.skills._common.emit_event`. Heavy LLM-fanout steps degrade to
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research import ResearchPayload
from eawf.kernel.store.paths import store_path
from eawf.platform.artifacts.references import Citation
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills.blitz import BlitzSkill, should_auto_invoke
from eawf.workflow.skills.bodies.research import (
    ResearchBody,
    ResearchOption,
    ResearchQuestion,
    ResearchRecommendation,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.engine import ActionRun, SkillAction, SkillContext, SkillResult, run_skill
from eawf.workflow.skills.registry import register

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


@dataclass
class _ResearchInputs:
    """Resolved ``/research`` inputs gathered before any algorithm step runs.

    Attributes:
        depth: The validated depth (``quick`` / ``normal`` / ``deep``).
        topic: The resolved research topic.
        brief_id: The freshly minted brief id (``BR-...``).
        final_requested: Whether the brief should be persisted.
        blitz_enabled: Whether the blitz auto-chain may fire.
    """

    depth: str
    topic: str
    brief_id: str
    final_requested: bool
    blitz_enabled: bool


@dataclass
class _ResearchWork:
    """The questions / options / recommendation produced by the execute stage.

    Attributes:
        questions: The synthesised question slots.
        options: The synthesised options.
        recommendation: The chosen recommendation.
        persisted_brief: The persisted-brief URN, or ``None``.
        next_actions: The accumulated next-valid-actions list.
    """

    questions: list[ResearchQuestion]
    options: list[ResearchOption]
    recommendation: ResearchRecommendation
    persisted_brief: str | None
    next_actions: list[str] = field(default_factory=list)


@register
class ResearchSkill(SkillAction):
    """Concrete ``/research`` skill (Phase 4 W02)."""

    name: SkillName = "/research"

    def _gather(self, run: ActionRun) -> _ResearchInputs:
        raw_depth = str(run.args.get("depth", _DEFAULT_DEPTH))
        depth = raw_depth if raw_depth in _VALID_DEPTHS else _DEFAULT_DEPTH
        topic = str(run.args.get("topic") or run.args.get("message") or run.scope_id)
        return _ResearchInputs(
            depth=depth,
            topic=topic,
            brief_id=f"BR-{uuid.uuid4().hex[:8].upper()}",
            final_requested=_bool_arg(run.args, "final", "save", default=False),
            blitz_enabled=_bool_arg(run.args, "blitz", default=True),
        )

    def _validate(self, run: ActionRun, inputs: _ResearchInputs) -> SkillResult | None:
        # The probe (engine-owned) is the only up-front gate; nothing else to
        # short-circuit before the algorithm runs.
        return None

    def _execute(self, run: ActionRun, inputs: _ResearchInputs) -> _ResearchWork | SkillResult:
        # Step 1 — probe already ran. Step 2: resolve scope.
        self._trace(
            run,
            "research.resolve_scope",
            f"research: resolve scope ({inputs.depth})",
            {"depth": inputs.depth, "scope_id": run.scope_id},
        )
        # Step 3 — continuation detection. v0.1 always starts a fresh brief.
        self._trace(
            run,
            "research.start_brief",
            f"research: start brief {inputs.brief_id}",
            {"brief_id": inputs.brief_id},
        )
        # Step 4 — define questions. v0.1 emits placeholder slots scaled by depth.
        questions = self._build_questions(run, inputs.depth)
        # Step 5 — dispatch parallel agents. v0.1: deep depth degrades to
        # needs_user with a typed user_question so the runtime / human can
        # supply the agent plan before re-invocation.
        if inputs.depth == "deep":
            return self._deep_depth_needs_user(run, inputs, questions)
        # Step 6 — synthesise options (v0.1 placeholder pair).
        options = self._build_options(run)
        # Step 7 — peer review (v0.1: skipped, leaves peer_review=None).
        self._trace(
            run,
            "research.peer_review",
            "research: peer review skipped in v0.1",
            {"skipped": True},
        )
        # Step 8 — recommend.
        recommendation = self._build_recommendation(run, inputs.depth, options)
        # Step 9 — persist brief when explicitly requested.
        persisted_brief = self._maybe_persist_brief(run, inputs, questions)
        work = _ResearchWork(
            questions=questions,
            options=options,
            recommendation=recommendation,
            persisted_brief=persisted_brief,
            next_actions=["eawf prep", "eawf hypothesis define"],
        )
        blitz_result = self._maybe_blitz(run, inputs, work)
        if blitz_result is not None:
            return blitz_result
        return work

    def _build_questions(self, run: ActionRun, depth: str) -> list[ResearchQuestion]:
        question_count = _depth_to_question_slots(depth)
        questions = [
            ResearchQuestion(
                q=f"Open question #{i + 1} for scope {run.scope_id}",
                answer="(awaiting agent fanout)",
                confidence="low",
                sources=[],
            )
            for i in range(question_count)
        ]
        self._trace(
            run,
            "research.define_questions",
            f"research: defined {len(questions)} question(s)",
            {"count": len(questions), "depth": depth},
        )
        return questions

    def _build_options(self, run: ActionRun) -> list[ResearchOption]:
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
        self._trace(
            run,
            "research.synthesize_options",
            f"research: synthesised {len(options)} option(s)",
            {"count": len(options)},
        )
        return options

    def _build_recommendation(
        self, run: ActionRun, depth: str, options: list[ResearchOption]
    ) -> ResearchRecommendation:
        recommendation = ResearchRecommendation(
            choice=options[0].name,
            confidence="medium" if depth == "normal" else "low",
            fallback=options[1].name,
        )
        self._trace(
            run,
            "research.recommend",
            f"research: recommended {recommendation.choice}",
            {"choice": recommendation.choice},
        )
        return recommendation

    def _maybe_persist_brief(
        self, run: ActionRun, inputs: _ResearchInputs, questions: list[ResearchQuestion]
    ) -> str | None:
        if not inputs.final_requested:
            return None
        persisted_brief = _append_research_brief(
            state_path=run.state_path,
            scope_id=run.scope_id,
            brief_id=inputs.brief_id,
            topic=inputs.topic,
            questions=questions,
        )
        run.records.append(persisted_brief)
        return persisted_brief

    def _deep_depth_needs_user(
        self, run: ActionRun, inputs: _ResearchInputs, questions: list[ResearchQuestion]
    ) -> SkillResult:
        self._trace(
            run,
            "research.fanout_pending",
            "research: deep depth requires explicit fanout decision",
            {"depth": inputs.depth},
        )
        body = ResearchBody(
            brief_id=inputs.brief_id,
            questions=questions,
            options=[],
            recommendation=None,
            peer_review=None,
            persisted_brief=None,
            user_question=UserQuestion(
                question=(
                    f"Deep research on {run.scope_id} needs a fanout plan. Pick how to proceed."
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
                    UserQuestionOption(label="cancel", description="Abort the research run."),
                ],
            ),
        )
        return self._needs_user(
            run,
            body.model_dump(mode="json"),
            next_valid_actions=["eawf prep", "eawf hypothesis define"],
        )

    def _maybe_blitz(
        self, run: ActionRun, inputs: _ResearchInputs, work: _ResearchWork
    ) -> SkillResult | None:
        residual_unknowns = sum(1 for q in work.questions if "awaiting" in q.answer.lower())
        if not (inputs.blitz_enabled and should_auto_invoke(residual_unknowns=residual_unknowns)):
            return None
        blitz_ctx = SkillContext(
            scope=run.ctx.scope,
            session=run.ctx.session,
            instrument_probe=dict(run.ctx.instrument_probe),
            args={
                "residual_unknowns": residual_unknowns,
                "followup_research_args": {
                    "topic": inputs.topic,
                    "depth": "quick",
                    "blitz": False,
                },
            },
        )
        blitz_env = run_skill(BlitzSkill(), blitz_ctx)
        work.next_actions.append("eawf skill run /blitz")
        work.next_actions.extend(blitz_env.footer.next_valid_actions)
        if blitz_env.header.status == "ok":
            return None
        return SkillResult(
            status=blitz_env.header.status,
            body=ResearchBody(
                brief_id=inputs.brief_id,
                questions=work.questions,
                options=work.options,
                recommendation=work.recommendation,
                peer_review=None,
                persisted_brief=work.persisted_brief,
            ).model_dump(mode="json"),
            persisted_store_records=run.records,
            state_mutations=run.mutations,
            evidence_refs=run.evidence,
            next_valid_actions=work.next_actions,
            repair_commands=blitz_env.footer.repair_commands
            or ["rerun /research with blitz=false"],
        )

    def _render(
        self, run: ActionRun, inputs: _ResearchInputs, outcome: _ResearchWork
    ) -> SkillResult:
        # Step 10 — record decisions / artefacts (v0.1: omitted; the audit
        # path captures verdicts).
        body = ResearchBody(
            brief_id=inputs.brief_id,
            questions=outcome.questions,
            options=outcome.options,
            recommendation=outcome.recommendation,
            peer_review=None,
            persisted_brief=outcome.persisted_brief,
        )
        return self._ok(
            run,
            body.model_dump(mode="json"),
            next_valid_actions=outcome.next_actions,
        )


__all__ = ["ResearchSkill"]
