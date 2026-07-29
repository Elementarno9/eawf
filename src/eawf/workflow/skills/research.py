"""``/research`` skill — investigate questions and produce a peer-reviewed brief.

Implements the ``/research`` algorithm per ``docs/architecture/workflow.md``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort if hard requirement
   missing.
2. Resolve scope: explicit message, else active iter unknowns/blockers.
3. Detect continuation: open brief on the same scope → load and extend.
4. Define questions: facts to verify, options to compare, risks to audit,
   decision needed.
5. Dispatch parallel read-only agents (the fan-out depths ``deep`` /
   ``exhaustive`` emit a typed ResearchPlan for the caller to dispatch).
6. Synthesize options: 2-4 solutions with tradeoffs/complexity/etc.
7. Review findings: cross-check citations.
8. Recommend one path with confidence and fallback.
9. Persist the brief artifact when ``-f`` / ``research.auto_save=true``.
10. Record artefact / decision candidates in state.

Each algorithm step writes one row to ``store/event.jsonl`` via
:func:`eawf.workflow.skills._common.emit_event`. Deep LLM-fanout emits a typed
``ResearchPlan`` body instead of asking the operator to hand-author the plan.

Honoured flags (per the W02 acceptance contract):

- ``--depth shallow|medium|deep|exhaustive`` — passed via
  ``ctx.args["depth"]`` and resolved against the canonical
  :class:`~eawf.kernel.spec.research.ResearchDepth` ladder; controls the
  number of synthesised question slots and the body's
  ``recommendation.confidence`` default. An unknown flag value falls back
  to the default depth rather than aborting the run. With no flag the
  stage reads the ``research.default_depth`` layered-config leaf (default
  ``medium``); a leaf set to an out-of-ladder token aborts the run.
- ``--agents <n>`` — fan-out width. An explicit value wins; with no flag
  the stage resolves the ``research.agent_count`` layered-config leaf
  (default ``4``) — this skill is that previously-idle leaf's first
  production consumer. The width caps the fan-out envelope count a
  deep/exhaustive plan emits and is clamped to the leaf's [1, 12] band.
- ``--rounds <n>`` — the fan-out iteration ceiling (default ``1``);
  recorded on the resolve-scope + fan-out traces.
- ``--budget <tokens>`` — token budget for the run (default uncapped);
  recorded on the traces until metering rows bind enforcement.

A malformed ``--agents`` / ``--rounds`` / ``--budget`` value degrades to
its default rather than aborting the run.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.kernel.spec.research import (
    DEFAULT_RESEARCH_DEPTH,
    ResearchDepth,
    coerce_research_depth,
    research_depth_emits_fanout,
    research_depth_question_slots,
    resolve_default_research_depth,
)
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
    ResearchFanoutEnvelope,
    ResearchOption,
    ResearchPlan,
    ResearchQuestion,
    ResearchRecommendation,
)
from eawf.workflow.skills.engine import ActionRun, SkillAction, SkillContext, SkillResult, run_skill
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


#: Ultra-fallback fan-out width when the ``research.agent_count`` leaf is
#: unreadable (mirrors ``src/eawf/kernel/config/defaults.py`` ``research.agent_count``).
#: The layered merge normally supplies the built-in default, so this constant
#: only bites when ``_merged_config`` degrades to an empty mapping.
_DEFAULT_RESEARCH_AGENT_COUNT = 4

#: The ``research.agent_count`` leaf's [min, max] band (config_keys.py:390).
_AGENT_COUNT_MIN = 1
_AGENT_COUNT_MAX = 12


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


def _int_arg(args: dict[str, Any], *names: str, default: int | None) -> int | None:
    """Return the first present non-negative int-ish skill arg, else *default*.

    Coerces numeric strings; a bool, a non-numeric token, or a negative value
    falls back to *default* so a malformed flag degrades rather than aborting
    the run (matching the lenient-coercion contract of ``--depth``).
    """
    for name in names:
        if name not in args:
            continue
        raw = args[name]
        if isinstance(raw, bool):
            return default
        if isinstance(raw, int):
            return raw if raw >= 0 else default
        if isinstance(raw, str):
            token = raw.strip()
            if token.isdigit():
                return int(token)
        return default
    return default


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
        depth: The resolved canonical depth on the
            :class:`~eawf.kernel.spec.research.ResearchDepth` ladder
            (``shallow`` / ``medium`` / ``deep`` / ``exhaustive``).
        topic: The resolved research topic.
        brief_id: The freshly minted brief id (``BR-...``).
        final_requested: Whether the brief should be persisted.
        blitz_enabled: Whether the blitz auto-chain may fire.
        rounds: The requested number of fan-out iterations (``--rounds``,
            default 1). Recorded on the resolve-scope trace so downstream
            round-bounded fan-out has a named ceiling.
        agents: The resolved fan-out width (``--agents`` else the
            ``research.agent_count`` layered-config leaf, clamped to [1, 12]).
            Caps the number of fan-out envelopes a deep/exhaustive plan emits.
        budget: The token budget for the run (``--budget``) or ``None`` when
            uncapped; recorded on the trace until metering binds it.
    """

    depth: ResearchDepth
    topic: str
    brief_id: str
    final_requested: bool
    blitz_enabled: bool
    rounds: int
    agents: int
    budget: int | None


@dataclass
class _ResearchWork:
    """The questions / options / recommendation produced by the execute stage.

    Attributes:
        questions: The synthesised question slots.
        options: The synthesised options.
        recommendation: The chosen recommendation, or ``None`` for plan-only deep research.
        persisted_brief: The persisted-brief URN, or ``None``.
        research_plan: The typed fan-out plan for deep research, or ``None``.
        next_actions: The accumulated next-valid-actions list.
    """

    questions: list[ResearchQuestion]
    options: list[ResearchOption]
    recommendation: ResearchRecommendation | None
    persisted_brief: str | None
    research_plan: ResearchPlan | None = None
    next_actions: list[str] = field(default_factory=list)


@register
class ResearchSkill(SkillAction):
    """Concrete ``/research`` skill (Phase 4 W02)."""

    name: SkillName = "/research"

    def _gather(self, run: ActionRun) -> _ResearchInputs:
        depth = self._resolve_depth(run)
        topic = str(run.args.get("topic") or run.args.get("message") or run.scope_id)
        return _ResearchInputs(
            depth=depth,
            topic=topic,
            brief_id=f"BR-{uuid.uuid4().hex[:8].upper()}",
            final_requested=self._resolve_auto_save(run),
            blitz_enabled=_bool_arg(run.args, "blitz", default=True),
            rounds=self._resolve_rounds(run),
            agents=self._resolve_agents(run),
            budget=_int_arg(run.args, "budget", default=None),
        )

    def _resolve_auto_save(self, run: ActionRun) -> bool:
        """Resolve explicit save/final flags before the research.auto_save leaf."""
        if "final" in run.args or "save" in run.args:
            return _bool_arg(run.args, "final", "save", default=False)
        merged = self._merged_config(run.state_path)
        research = merged.get("research")
        value = research.get("auto_save", False) if isinstance(research, dict) else False
        return value if isinstance(value, bool) else False

    @staticmethod
    def _resolve_rounds(run: ActionRun) -> int:
        """Resolve the fan-out round count (``--rounds``, floor 1).

        A missing / malformed / below-floor value degrades to a single round
        rather than aborting the run.
        """
        rounds = _int_arg(run.args, "rounds", default=1)
        return rounds if rounds is not None and rounds >= 1 else 1

    def _resolve_agents(self, run: ActionRun) -> int:
        """Resolve the fan-out width for this run.

        An explicit ``--agents`` flag wins; with no flag the stage reads the
        ``research.agent_count`` layered-config leaf (default 4) -- this skill
        is the first production consumer of that previously-idle leaf. The
        resolved value is clamped to the leaf's [1, 12] band so an
        out-of-band flag or config value degrades to the nearest bound.

        Returns:
            The clamped fan-out width.
        """
        explicit = _int_arg(run.args, "agents", default=None)
        if explicit is None:
            merged = self._merged_config(run.state_path)
            research_cfg = merged.get("research")
            raw = (
                research_cfg.get("agent_count", _DEFAULT_RESEARCH_AGENT_COUNT)
                if isinstance(research_cfg, dict)
                else _DEFAULT_RESEARCH_AGENT_COUNT
            )
            explicit = (
                int(raw)
                if isinstance(raw, int) and not isinstance(raw, bool)
                else _DEFAULT_RESEARCH_AGENT_COUNT
            )
        return max(_AGENT_COUNT_MIN, min(_AGENT_COUNT_MAX, explicit))

    def _resolve_depth(self, run: ActionRun) -> ResearchDepth:
        """Resolve the survey depth for this run.

        An explicit ``--depth`` flag wins and is coerced leniently (an
        out-of-ladder token falls back to the default rather than aborting,
        per the skill's documented flag contract). With no flag the stage
        honours the ``research.default_depth`` layered-config leaf — closing
        the standing idle config contract where the leaf was registered but
        nothing read it. A misconfigured leaf (out-of-ladder token) raises
        out of :func:`resolve_default_research_depth`; the engine maps the
        raise onto a ``status=failed`` envelope.

        Returns:
            The resolved canonical :class:`ResearchDepth`.
        """
        raw_depth = run.args.get("depth")
        if raw_depth is not None:
            return coerce_research_depth(str(raw_depth))
        merged = self._merged_config(run.state_path)
        return resolve_default_research_depth(merged)

    @staticmethod
    def _merged_config(state_path: Path) -> dict[str, Any]:
        """Compose the layered config anchored at the active repo.

        Deferred import mirrors :func:`eawf.workflow.skills._common.has_research_profile`
        so the skill does not pull the profile/Yaml machinery at import time.
        The anchor (``<repo>``) is the state file's grandparent (``.ea`` is the
        parent). A merge failure degrades to an empty mapping so the caller
        falls back to the built-in default rather than crashing the run.
        """
        from eawf.kernel.config.layered import merge_config

        anchor = state_path.parent.parent
        try:
            merged, _sources = merge_config(repo=anchor, workspace=anchor)
        except Exception as exc:  # pragma: no cover - defensive only
            logger.debug(f"_merged_config merge_error={exc!r}")
            return {}
        return merged

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
            {
                "depth": inputs.depth,
                "scope_id": run.scope_id,
                "rounds": inputs.rounds,
                "agents": inputs.agents,
                "budget": inputs.budget,
            },
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
        # Step 5 — dispatch parallel agents. Fan-out depths (deep / exhaustive)
        # emit the typed plan the runtime can fan out, while the shallow / medium
        # rungs keep the v0.1 placeholder synthesis path.
        research_plan = self._build_research_plan(run, inputs, questions)
        if research_plan is not None:
            return _ResearchWork(
                questions=questions,
                options=[],
                recommendation=None,
                persisted_brief=None,
                research_plan=research_plan,
                next_actions=["eawf agent dispatch", "eawf prep", "eawf hypothesis define"],
            )
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

    def _build_questions(self, run: ActionRun, depth: ResearchDepth) -> list[ResearchQuestion]:
        live = self._live_open_questions(run)
        if live:
            # The campaign control plane has populated the OpenQuestion ledger
            # (the o-key channel / round-end reconcile writes real rows), so
            # surface those instead of fabricating placeholder slots -- the
            # idle-slot path is reserved for a scope that has logged none yet.
            self._trace(
                run,
                "research.define_questions",
                f"research: surfaced {len(live)} open question(s) from the ledger",
                {"count": len(live), "depth": depth, "source": "ledger"},
            )
            return live
        question_count = research_depth_question_slots(depth)
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
            {"count": len(questions), "depth": depth, "source": "placeholder"},
        )
        return questions

    @staticmethod
    def _live_open_questions(run: ActionRun) -> list[ResearchQuestion]:
        """Read the scope's :class:`OpenQuestion` ledger into question slots.

        Loads the active ``state.json`` and folds every still-open
        (``OPEN`` / ``BLOCKED``) :class:`~eawf.kernel.state.models.OpenQuestion`
        row into a :class:`ResearchQuestion` so the brief surfaces the real
        campaign questions rather than placeholder slots. A scope with no
        loadable state, no question ledger, or no open question yields an empty
        list so the caller falls back to the placeholder path.

        Args:
            run: The active skill run (carries the resolved ``state_path``).

        Returns:
            The live open questions as research-question slots, in natural-id
            order; empty when the ledger carries none.
        """
        from eawf.kernel.state.enums import OpenQuestionStatus
        from eawf.kernel.state.ids import natural_key
        from eawf.workflow.evidence._io import load_state

        if not run.state_path.exists():
            return []
        try:
            state = load_state(run.state_path)
        except Exception as exc:  # pragma: no cover - defensive only
            logger.debug(f"_live_open_questions load_error={exc!r}")
            return []
        rows = state.open_questions or {}
        live_statuses = {OpenQuestionStatus.OPEN, OpenQuestionStatus.BLOCKED}
        slots: list[ResearchQuestion] = []
        for key in sorted(rows, key=natural_key):
            question = rows[key]
            if question.status not in live_statuses:
                continue
            slots.append(
                ResearchQuestion(
                    q=question.title,
                    answer="(blocking)" if question.blocking else "(awaiting agent fanout)",
                    confidence="low",
                    sources=[],
                )
            )
        return slots

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
        self, run: ActionRun, depth: ResearchDepth, options: list[ResearchOption]
    ) -> ResearchRecommendation:
        recommendation = ResearchRecommendation(
            choice=options[0].name,
            confidence="medium" if depth == DEFAULT_RESEARCH_DEPTH else "low",
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

    def _build_research_plan(
        self, run: ActionRun, inputs: _ResearchInputs, questions: list[ResearchQuestion]
    ) -> ResearchPlan | None:
        if not research_depth_emits_fanout(inputs.depth):
            return None
        # The resolved --agents fan-out width caps how many question slots
        # dispatch in parallel: with the default width (4) every question of a
        # deep/exhaustive plan (<=4 slots) fans out, and a narrower --agents
        # trims the tail so the emitted envelope count never exceeds the width.
        fanout_envelopes = [
            ResearchFanoutEnvelope(
                envelope_id=f"{inputs.brief_id}-F{i + 1:02d}",
                agent_role="researcher",
                question=question.q,
                prompt=(
                    f"Investigate {question.q!r} for {run.scope_id}. "
                    f"Return concise findings with repo-relative citations."
                ),
                expected_output="agent_end researcher report with cited findings",
            )
            for i, question in enumerate(questions)
        ][: inputs.agents]
        plan = ResearchPlan(
            depth=inputs.depth,
            topic=inputs.topic,
            fanout_envelopes=fanout_envelopes,
        )
        self._trace(
            run,
            "research.fanout_plan",
            f"research: emitted {inputs.depth} fanout plan ({len(fanout_envelopes)} envelope(s))",
            {
                "depth": inputs.depth,
                "fanout_envelopes": len(fanout_envelopes),
                "rounds": inputs.rounds,
                "agents": inputs.agents,
                "budget": inputs.budget,
            },
        )
        return plan

    def _maybe_blitz(
        self, run: ActionRun, inputs: _ResearchInputs, work: _ResearchWork
    ) -> SkillResult | None:
        if work.research_plan is not None:
            return None
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
                    "depth": ResearchDepth.SHALLOW.value,
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
                research_plan=work.research_plan,
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
            research_plan=outcome.research_plan,
        )
        return self._ok(
            run,
            body.model_dump(mode="json"),
            next_valid_actions=outcome.next_actions,
        )


__all__ = ["ResearchSkill"]
