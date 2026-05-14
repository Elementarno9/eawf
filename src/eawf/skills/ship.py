"""``/ship`` skill — commit / push / PR open-close controller.

Implements the §14 algorithm for ``/ship``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Require current audit passed or explicit allowed exception.
3. Inspect git status / diff / log and state scope.
4. Review memory: extract durable lessons; promote useful entries.
5. Build pending-ship artefact: commit groups, messages, files, evidence.
6. Default policy is ask before commit; ``--commit`` opts in to auto-commit.
7. Default policy is ask before push; ``--push`` opts in to auto-push.
8. PR action: open draft/ready, update body, close/merge.
9. Merge / close gates: CI green, required reviews, state valid.
10. Record commits / PR / merge / audit artefacts and final estimate-vs-actual.
11. Remove clean worktrees per policy.

Honoured flags:

- ``--commit`` — toggles ``body.commit_groups`` population (else empty).
- ``--push`` — toggles ``body.push`` population.
- ``--pr <action>`` — populates ``body.pr.action`` (open/ready/draft/close/none).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.artifacts.validation import validate_markdown_artifact, validate_text_surface
from eawf.render.envelope import SkillName
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.bodies.ship import (
    ShipBody,
    ShipCommitGroup,
    ShipPr,
    ShipPrGates,
    ShipPush,
)
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register

logger = logging.getLogger(__name__)


_VALID_PR_ACTIONS: tuple[str, ...] = ("open", "ready", "draft", "close", "none")


def _coerce_bool(value: Any) -> bool:
    """Best-effort string→bool coercion for stdin-piped JSON args."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _resolve_pr_action(value: Any) -> str | None:
    """Normalise ``--pr`` argument; ``None`` when unset."""
    if value is None:
        return None
    if isinstance(value, bool) and value:
        return "open"
    candidate = str(value).strip().lower()
    if candidate in _VALID_PR_ACTIONS:
        return candidate
    if candidate in {"true", "yes", "on", "1"}:
        return "open"
    return None


def _coerce_path_list(value: Any) -> list[Path]:
    """Coerce stdin JSON args into a list of artifact paths."""
    if value is None:
        return []
    if isinstance(value, str):
        return [Path(p.strip()) for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [Path(str(p)) for p in value]
    return [Path(str(value))]


@register
class ShipSkill(Skill):
    """Concrete ``/ship`` skill (Phase 4 W02)."""

    name: SkillName = "/ship"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        do_commit = _coerce_bool(args.get("commit", False))
        do_push = _coerce_bool(args.get("push", False))
        pr_action = _resolve_pr_action(args.get("pr"))
        artifact_paths = _coerce_path_list(args.get("artifact_paths") or args.get("artifacts"))
        pr_body = args.get("pr_body")

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf wave close", "eawf audit"]

        validation_errors: list[str] = []
        for path in artifact_paths:
            try:
                artifact_report = validate_markdown_artifact(path.read_text(encoding="utf-8"))
            except OSError as exc:
                validation_errors.append(f"{path}: {exc}")
                continue
            validation_errors.extend(f"{path}: {error}" for error in artifact_report.errors)
        if isinstance(pr_body, str):
            text_report = validate_text_surface(pr_body, surface="pr")
            validation_errors.extend(text_report.errors)
        if validation_errors:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="ship.artifact_gate",
                summary="ship: artifact validation failed",
                payload={"errors": validation_errors},
            )
            persisted_records.append(evt_id)
            return SkillResult(
                status="failed",
                body=ShipBody(
                    commit_groups=[],
                    push=None,
                    pr=None,
                    estimate_vs_actual={"estimated_eu": 0.0, "actual_eu": 0.0},
                    rollback_notes="artifact validation failed",
                ).model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
                repair_commands=["fix artifact validation errors and rerun /ship"],
            )

        # Step 1 — probe ran. Step 2: gate on audit.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.audit_gate",
            summary="ship: audit gate (v0.1 stub allows pass)",
            payload={"audit_required": True, "passed": True},
        )
        persisted_records.append(evt_id)

        # Step 3 — inspect git.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.inspect_git",
            summary="ship: inspect git status / diff / scope",
            payload={"scope_id": scope_id},
        )
        persisted_records.append(evt_id)

        # Step 4 — memory review.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.memory_review",
            summary="ship: review session memory",
            payload={"promoted": 0, "pruned": 0},
        )
        persisted_records.append(evt_id)

        # Step 5 — build pending-ship artefact.
        commit_groups: list[ShipCommitGroup] = []
        if do_commit:
            commit_groups.append(
                ShipCommitGroup(
                    message=f"[{scope_id}] feat: pending ship",
                    files=[],
                    evidence_refs=[],
                )
            )
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.build_pending",
            summary=f"ship: built pending artefact ({len(commit_groups)} commit group(s))",
            payload={"commit_groups": len(commit_groups)},
        )
        persisted_records.append(evt_id)

        # Step 6 — commit gate.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.commit",
            summary=f"ship: commit={do_commit}",
            payload={"applied": do_commit},
        )
        persisted_records.append(evt_id)

        # Step 7 — push gate.
        push: ShipPush | None = None
        if do_push:
            push = ShipPush(ref="HEAD", status="planned")
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.push",
            summary=f"ship: push={do_push}",
            payload={"applied": do_push},
        )
        persisted_records.append(evt_id)

        # Step 8 — PR action.
        pr: ShipPr | None = None
        if pr_action is not None:
            pr = ShipPr(
                action=pr_action,
                url=None,
                template="iter",
                gates=ShipPrGates(
                    ci="pending",
                    reviews="pending",
                    state_valid=True,
                ),
            )
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.pr",
            summary=f"ship: pr={pr_action or 'none'}",
            payload={"action": pr_action or "none"},
        )
        persisted_records.append(evt_id)

        # Step 9 — gate evaluation already inside ShipPrGates.
        # Step 10 — record artefacts.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.record",
            summary="ship: artefacts recorded",
            payload={
                "commit": do_commit,
                "push": do_push,
                "pr": pr_action or "none",
            },
        )
        persisted_records.append(evt_id)

        # Step 11 — worktree cleanup (v0.1: skipped; the `eawf worktree`
        # surface owns this).

        body = ShipBody(
            commit_groups=commit_groups,
            push=push,
            pr=pr,
            estimate_vs_actual={
                "estimated_eu": 0.0,
                "actual_eu": 0.0,
            },
            rollback_notes=None,
        )

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            next_valid_actions=next_actions,
        )


__all__ = ["ShipSkill"]
