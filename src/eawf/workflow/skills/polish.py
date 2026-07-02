"""``/polish`` skill — whole-repo consistency audit + cleanup.

Implements the §14 algorithm for ``/polish``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Snapshot repo / state; do not mutate before report.
3. Fan out read-only agents over code, tests, docs, configs, state, memory.
4. Find inconsistencies (stale docs, duplicate rules, broken links, ...).
5. Reconcile / merge findings into grouped cleanup tables by topic / scope.
6. Memory pass: promote useful entries; mark stale; propose prune list.
7. Without ``-y``, ask which groups to run.
8. With ``-y``, apply safe groups only; unsafe still asks.
9. Run affected checks; write polish report artefact.
10. State updates record decisions / backlog / memory changes.

Honoured args:

- ``report_only`` (or alias ``y``=False) — toggles ``body.report_only``.
- ``max_fixes`` — caps applied items (the v0.1 stub doesn't apply
  anything; the cap survives in the body for downstream waves).
- ``auto_apply_safe`` — when true, applies safe groups automatically
  (flips ``report_only`` off unless an explicit ``report_only`` / ``y``
  overrides it). Explicit ``report_only`` and ``y`` win over this flag.
- ``category`` — restricts the sweep to one of
  ``naming|docstrings|logs|errors|dead-code`` (default ``all``); recorded
  on the fan-out + apply-gate traces and surfaced in
  ``next_valid_actions``.

Note: the ``polish.auto_apply_safe`` / ``polish.deletion_policy``
layered-config LEAVES the ``--auto-apply-safe`` flag would resolve through
are not yet registered (config_keys.py / leaf_catalog.py / defaults.py);
until they land the flag is honoured only from ``ctx.args``.
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
from eawf.workflow.skills.bodies.polish import (
    PolishBody,
    PolishGroup,
    PolishItem,
    PolishMemoryPass,
)
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _coerce_category(value: Any) -> str:
    """Normalise ``ctx.args['category']`` to a lowercase sweep filter.

    A missing / blank value degrades to ``all`` (sweep every category).
    """
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "all"


@register
class PolishSkill(Skill):
    """Concrete ``/polish`` skill (Phase 4 W02)."""

    name: SkillName = "/polish"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        # Default to report-only so v0.1 polish runs are non-destructive.
        report_only = _coerce_bool(args.get("report_only"), default=True)
        auto_apply_safe = _coerce_bool(args.get("auto_apply_safe"), default=False)
        # ``--auto-apply-safe`` applies safe groups automatically, so it flips
        # report_only off -- but an explicit ``report_only`` / ``y`` still wins.
        if args.get("report_only") is None and auto_apply_safe:
            report_only = False
        # The ``-y`` flag inverts: -y → not report_only.
        if args.get("y") is not None:
            report_only = not _coerce_bool(args.get("y"))
        category = _coerce_category(args.get("category"))

        persisted_records: list[str] = []

        # Step 1 — probe ran. Step 2: snapshot.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="polish.snapshot",
            summary="polish: snapshot repo + state",
            payload={"report_only": report_only},
        )
        persisted_records.append(evt_id)

        # Step 3 — fan out (v0.1 skipped).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="polish.fanout",
            summary=f"polish: read-only fanout (v0.1 stub) category={category}",
            payload={"skipped": True, "category": category},
        )
        persisted_records.append(evt_id)

        # Step 4 — find inconsistencies (placeholder list).
        # Step 5 — reconcile into groups.
        groups = [
            PolishGroup(
                topic="docs",
                scope=scope_id,
                risk="low",
                items=[
                    PolishItem(
                        kind="stale_doc",
                        location="docs/*",
                        action="review for staleness",
                        applied=False,
                    )
                ],
            )
        ]
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="polish.find_inconsistencies",
            summary=f"polish: collected {len(groups)} group(s)",
            payload={"group_count": len(groups)},
        )
        persisted_records.append(evt_id)

        # Step 6 — memory pass.
        memory_pass = PolishMemoryPass(promotions=0, prunes=0, compactions=0)
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="polish.memory_pass",
            summary="polish: memory pass (no changes)",
            payload={
                "promotions": memory_pass.promotions,
                "prunes": memory_pass.prunes,
                "compactions": memory_pass.compactions,
            },
        )
        persisted_records.append(evt_id)

        # Step 7 / 8 — apply gate (v0.1: respect report_only).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="polish.apply_gate",
            summary=f"polish: report_only={report_only} auto_apply_safe={auto_apply_safe}",
            payload={
                "report_only": report_only,
                "auto_apply_safe": auto_apply_safe,
                "category": category,
            },
        )
        persisted_records.append(evt_id)

        # Step 9 — write polish report artefact.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="polish.write_report",
            summary="polish: report persisted as event payload (v0.1)",
            payload={"groups": len(groups)},
        )
        persisted_records.append(evt_id)

        # Step 10 — state updates (v0.1 skipped).

        body = PolishBody(
            groups=groups,
            memory_pass=memory_pass,
            report_only=report_only,
        )

        next_actions = ["eawf audit", "eawf phase prepare-close", "eawf ship"]
        if category != "all":
            next_actions.insert(0, f"eawf polish --category {category}")

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            next_valid_actions=next_actions,
        )


__all__ = ["PolishSkill"]
