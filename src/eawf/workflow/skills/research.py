"""``/research`` skill — the headless body refuses and routes to a research Campaign.

The ``/research`` skill is answered by a host agent following its catalog
prompt. A headless run (``eawf skill run research``) has no agent to read
sources or answer questions, and the catalog forbids turning the invocation
into a Campaign implicitly, so this body never invents questions, options or
a recommendation. It resolves the run's inputs, surfaces the scope's live
OpenQuestion rows, and refuses with ``status=blocked``, a
``campaign_required`` warning, and the ``eawf campaign new`` /
``eawf campaign run`` route that plans and drives the research natively.

Each step writes one row to ``store/event.jsonl`` via
:func:`eawf.workflow.skills._common.emit_event`.

Honoured flags:

- ``--question <text>`` — the route's first ``--question``, ahead of the
  scope's live OpenQuestion rows; the topic stands in when there are none.
- ``--depth shallow|medium|deep|exhaustive`` — passed via
  ``ctx.args["depth"]`` and resolved against the canonical
  :class:`~eawf.kernel.spec.research.ResearchDepth` ladder; carried onto the
  route's ``--depth``. An unknown flag value falls back to the default depth
  rather than aborting the run. With no flag the stage reads the
  ``research.default_depth`` layered-config leaf (default ``medium``); a
  leaf set to an out-of-ladder token aborts the run.
- ``--agents <n>`` — fan-out width. An explicit value wins; with no flag
  the stage resolves the ``research.agent_count`` layered-config leaf
  (default ``4``), clamped to the leaf's [1, 12] band, and carries it onto
  the route's ``--agents``.
- ``--rounds <n>`` — the fan-out iteration ceiling (default ``1``);
  recorded on the resolve-scope trace.
- ``--budget <tokens>`` — token budget for the run (default uncapped);
  recorded on the resolve-scope trace.

A malformed ``--agents`` / ``--rounds`` / ``--budget`` value degrades to
its default rather than aborting the run.
"""

from __future__ import annotations

import logging
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eawf.kernel.spec.research import (
    ResearchDepth,
    coerce_research_depth,
    resolve_default_research_depth,
)
from eawf.surfaces.render.envelope import EnvelopeWarning, SkillName
from eawf.workflow.skills.bodies.research import ResearchBody, ResearchQuestion
from eawf.workflow.skills.engine import ActionRun, SkillAction, SkillResult
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

#: ``runtime.campaign.start`` takes at most this many questions.
_CAMPAIGN_MAX_QUESTIONS = 12


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


@dataclass
class _ResearchInputs:
    """Resolved ``/research`` inputs gathered before any algorithm step runs.

    Attributes:
        depth: The resolved canonical depth on the
            :class:`~eawf.kernel.spec.research.ResearchDepth` ladder
            (``shallow`` / ``medium`` / ``deep`` / ``exhaustive``).
        topic: The resolved research topic.
        question: The invocation's own ``--question``, or ``None``.
        brief_id: The freshly minted brief id (``BR-...``).
        rounds: The requested number of fan-out iterations (``--rounds``,
            default 1), recorded on the resolve-scope trace.
        agents: The resolved fan-out width (``--agents`` else the
            ``research.agent_count`` layered-config leaf, clamped to [1, 12]).
        budget: The token budget for the run (``--budget``) or ``None`` when
            uncapped; recorded on the trace.
    """

    depth: ResearchDepth
    topic: str
    question: str | None
    brief_id: str
    rounds: int
    agents: int
    budget: int | None


@register
class ResearchSkill(SkillAction):
    """Headless ``/research``: refuse and name the research Campaign route."""

    name: SkillName = "/research"

    def _gather(self, run: ActionRun) -> _ResearchInputs:
        depth = self._resolve_depth(run)
        topic = str(run.args.get("topic") or run.args.get("message") or run.scope_id)
        return _ResearchInputs(
            depth=depth,
            topic=topic,
            question=str(run.args["question"]) if run.args.get("question") else None,
            brief_id=f"BR-{uuid.uuid4().hex[:8].upper()}",
            rounds=self._resolve_rounds(run),
            agents=self._resolve_agents(run),
            budget=_int_arg(run.args, "budget", default=None),
        )

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
        ``research.agent_count`` layered-config leaf (default 4). The
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
        honours the ``research.default_depth`` layered-config leaf. A
        misconfigured leaf (out-of-ladder token) raises out of
        :func:`resolve_default_research_depth`; the engine maps the raise
        onto a ``status=failed`` envelope.

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

    def _execute(self, run: ActionRun, inputs: _ResearchInputs) -> list[ResearchQuestion]:
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
        return self._live_open_questions(run)

    @staticmethod
    def _live_open_questions(run: ActionRun) -> list[ResearchQuestion]:
        """Read the scope's :class:`OpenQuestion` ledger into question slots.

        Loads the active ``state.json`` and folds every still-open
        (``OPEN`` / ``BLOCKED``) :class:`~eawf.kernel.state.models.OpenQuestion`
        row into a :class:`ResearchQuestion`. A scope with no loadable state,
        no question ledger, or no open question yields an empty list.

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
                    answer="(blocking)" if question.blocking else "(open)",
                    confidence="low",
                    sources=[],
                )
            )
        return slots

    def _render(
        self, run: ActionRun, inputs: _ResearchInputs, outcome: list[ResearchQuestion]
    ) -> SkillResult:
        own = [inputs.question] if inputs.question else []
        questions = [*own, *(q.q for q in outcome)][:_CAMPAIGN_MAX_QUESTIONS] or [inputs.topic]
        question_flags = " ".join(f"--question {shlex.quote(q)}" for q in questions)
        route = [
            f"eawf campaign new {shlex.quote(inputs.topic)} --track <track-urn>"
            f" {question_flags} --depth {inputs.depth} --agents {inputs.agents}"
            f" --actor <principal-key> --run",
            "eawf campaign run <CAM-####> --actor <principal-key>",
        ]
        self._trace(
            run,
            "research.campaign_required",
            "research: refused headless run; plan a research Campaign",
            {"questions": len(questions), "depth": inputs.depth, "agents": inputs.agents},
        )
        body = ResearchBody(brief_id=inputs.brief_id, questions=outcome)
        result = self._blocked(
            run,
            body.model_dump(mode="json"),
            next_valid_actions=["eawf campaign new", "eawf campaign run"],
            repair_commands=route,
        )
        result.warnings.append(
            EnvelopeWarning(
                code="campaign_required",
                detail=(
                    "a headless /research run has no agent to answer its questions; plan"
                    " and drive a research Campaign with eawf campaign new and"
                    " eawf campaign run"
                ),
            )
        )
        return result


__all__ = ["ResearchSkill"]
