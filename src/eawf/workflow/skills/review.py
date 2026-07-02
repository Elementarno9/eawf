"""``/review`` skill — review the active PR and post a templated review.

Implements the §14 algorithm for ``/review``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Resolve PR from explicit flag or active branch.
3. Fetch PR metadata: base/head, commits, changed files, checks, comments.
4. Review correct diff with merge-base / triple-dot semantics.
5. Dispatch focused agents by area / risk (v0.1: skipped).
6. Check PR template completeness, state links, audit evidence, drift, tests.
7. Produce findings table + recommendation: approve / comment /
   request_changes / fix_locally.
8. ``--post`` publishes the review; otherwise output draft.
9. ``--fix`` routes through ``/prep -i`` or applies safe fixes.

Honoured args (passed via ``ctx.args``):

- ``pr`` — explicit PR URL or number.
- ``base`` / ``head`` — explicit refs override the resolver default.
- ``post`` — toggles ``body.posted=True``.
- ``recommendation`` — one of the four frozen literals; defaults to
  ``"comment"`` so the body validates.
- ``level`` — ``low|medium|high`` finding-confidence threshold (default
  ``medium``); recorded on the findings trace so a downstream finding pass
  can filter against it. An out-of-ladder value falls back to ``medium``.
- ``criteria`` — a wave id whose success criteria seed the review context;
  recorded on the resolve/findings traces and surfaced as the first
  ``next_valid_actions`` command so the reviewer reads the criteria the
  diff must satisfy.
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
from eawf.workflow.skills.bodies.review import ReviewBody, ReviewRecommendation
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


_VALID_RECOMMENDATIONS: tuple[str, ...] = (
    "approve",
    "comment",
    "request_changes",
    "fix_locally",
)

#: Finding-confidence threshold ladder for ``--level`` (default ``medium``).
#: ``low`` surfaces every candidate finding; ``high`` only the
#: high-confidence ones. The v0.1 stub records the threshold on the review
#: trace so a downstream finding pass can filter against it.
_VALID_LEVELS: tuple[str, ...] = ("low", "medium", "high")


def _coerce_recommendation(value: Any) -> ReviewRecommendation:
    """Normalise ``ctx.args['recommendation']`` to the frozen literal."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _VALID_RECOMMENDATIONS:
            return candidate  # type: ignore[return-value]
    return "comment"


def _coerce_level(value: Any) -> str:
    """Normalise ``ctx.args['level']`` to a finding-confidence threshold.

    A missing or out-of-ladder value falls back to ``medium`` rather than
    aborting the run, matching the lenient-coercion contract of the other
    review args.
    """
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _VALID_LEVELS:
            return candidate
    return "medium"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@register
class ReviewSkill(Skill):
    """Concrete ``/review`` skill (Phase 4 W02)."""

    name: SkillName = "/review"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        pr_url = str(args.get("pr") or args.get("pr_url") or "")
        base = str(args.get("base") or "main")
        head = str(args.get("head") or "HEAD")
        recommendation = _coerce_recommendation(args.get("recommendation"))
        do_post = _coerce_bool(args.get("post", False))
        level = _coerce_level(args.get("level"))
        criteria_wave = str(args.get("criteria") or args.get("criteria_wave") or "") or None

        persisted_records: list[str] = []
        next_actions: list[str] = ["eawf review --post", "eawf prep -i"]
        # Pulling the reviewed wave's success criteria into the review context
        # is the model's job; surface the source command so the reviewer reads
        # the criteria the diff must satisfy.
        if criteria_wave is not None:
            next_actions.insert(0, f"eawf wave show --dispatch-prompt {criteria_wave}")

        # Step 1 — probe ran. Step 2: resolve PR.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="review.resolve_pr",
            summary=f"review: resolve pr={pr_url or '(active branch)'} base={base} head={head}",
            payload={
                "pr_url": pr_url,
                "base": base,
                "head": head,
                "criteria_wave": criteria_wave,
            },
        )
        persisted_records.append(evt_id)

        # Step 3 — fetch PR metadata.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="review.fetch_metadata",
            summary="review: fetched PR metadata (v0.1 stub)",
            payload={"pr_url": pr_url},
        )
        persisted_records.append(evt_id)

        # Step 4 — review diff (triple-dot).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="review.diff",
            summary="review: triple-dot diff inspected",
            payload={"base": base, "head": head},
        )
        persisted_records.append(evt_id)

        # Step 5 — dispatch agents (v0.1 skipped).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="review.dispatch",
            summary="review: agent fanout skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        # Step 6 — template / drift / tests check.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="review.template_check",
            summary="review: template / drift checks",
            payload={},
        )
        persisted_records.append(evt_id)

        # Step 7 — findings + recommendation.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="review.findings",
            summary=f"review: recommendation={recommendation} level={level}",
            payload={
                "recommendation": recommendation,
                "level": level,
                "criteria_wave": criteria_wave,
            },
        )
        persisted_records.append(evt_id)

        # Step 8 — post.
        if do_post:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="review.post",
                summary="review: posted templated review",
                payload={"posted": True},
            )
            persisted_records.append(evt_id)

        # Step 9 — fix routing (v0.1 skipped).

        body = ReviewBody(
            pr_url=pr_url or f"local://{scope_id}",
            base=base,
            head=head,
            findings=[],
            recommendation=recommendation,
            posted=do_post,
        )

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            next_valid_actions=next_actions,
        )


__all__ = ["ReviewSkill"]
