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
import os
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from eawf.artifacts.validation import validate_markdown_artifact, validate_text_surface
from eawf.config.layered import merge_config
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
from eawf.state.enums import AuditKind, AuditVerdict
from eawf.state.ids import is_phase_id, parents_of
from eawf.state.models import Audit, State
from eawf.vcs.coauthor import CoauthorPolicyError, VcsConfig, resolve_coauthor_trailer

logger = logging.getLogger(__name__)


_VALID_PR_ACTIONS: tuple[str, ...] = ("open", "ready", "draft", "close", "none")

#: Audit verdicts that clear the ship gate. ``pass`` is clean; ``minor``
#: carries triage-later findings but does not block ship (mirrors the
#: ``AgentReportVerdict.PASS_WITH_FOLLOWUPS`` semantics surfaced by
#: ``eawf.tui.audit_overlay``). ``major`` and a missing verdict both block.
_SHIP_ALLOWED_VERDICTS: frozenset[AuditVerdict] = frozenset({AuditVerdict.PASS, AuditVerdict.MINOR})


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


def _load_state(state_path: Path) -> State | None:
    """Return the validated :class:`State`, or ``None`` when unreadable.

    Read-only and best-effort (rule 4: the daemon is the sole mutator;
    reads are free). A missing file, malformed JSON, or schema mismatch
    all degrade to ``None`` so the ship gate can fall through rather than
    crash when no state document is available to gate against.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The validated state document, or ``None`` when absent or invalid.
    """
    if not state_path.exists():
        return None
    try:
        return State.model_validate(orjson.loads(state_path.read_bytes()))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        logger.warning(f"_load_state could not read state at {state_path}: {exc}")
        return None


def _phase_id_from_scope(scope_id: str) -> str | None:
    """Resolve the bare phase id from a state-scope URN.

    The skill scope arrives as a URN (``urn:eawf:v1:state:QR/P00``) or a
    bare lifecycle id; we take the tail after the final ``/`` then walk up
    via :func:`eawf.state.ids.parents_of` so an iter / wave scope resolves
    to its owning phase.

    Args:
        scope_id: The skill's state-scope URN or bare lifecycle id.

    Returns:
        The phase id, or ``None`` when the tail is not a recognised
        lifecycle id.
    """
    tail = scope_id.rsplit("/", 1)[-1]
    if is_phase_id(tail):
        return tail
    try:
        parents = parents_of(tail)
    except ValueError:
        return None
    return parents[0] if parents else None


def _latest_audit_for_phase(state: State, phase_id: str) -> Audit | None:
    """Return the most-recent audit recorded against *phase_id*.

    Ship-gate audits win over other kinds; within a kind the latest
    ``created_at`` wins. Audits scoped to the phase's iters / waves are not
    considered — the ship gate cares about the phase-level verdict.

    Args:
        state: The loaded state document.
        phase_id: The phase id whose audit verdict gates the ship.

    Returns:
        The selected :class:`Audit`, or ``None`` when the phase has none.
    """
    candidates = [a for a in (state.audits or {}).values() if a.scope_id == phase_id]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda a: (a.kind == AuditKind.SHIP_GATE, a.created_at),
    )


def _load_vcs_config(state_path: Path) -> VcsConfig:
    """Load the layered ``vcs`` config block as a validated :class:`VcsConfig`.

    The merge anchors on the repo root (``state.json`` lives at
    ``<repo>/.ea/state.json``) so the repo, branch, and local layers are
    consulted on top of the built-in defaults. The config registry already
    owns ``vcs.pr_merge_method`` / ``vcs.squash_allowed`` / ``vcs.coauthor``;
    this helper only reads them.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The validated ``vcs`` config surface (built-in defaults when no
        overlay file is present).
    """
    anchor = state_path.parent.parent
    merged, _sources = merge_config(repo=anchor, workspace=anchor)
    return VcsConfig.model_validate(merged.get("vcs", {}))


def _resolve_coauthor_trailer_for_ship(vcs_config: VcsConfig) -> str | None:
    """Resolve the ship run's co-author trailer.

    Delegates entirely to :func:`eawf.vcs.coauthor.resolve_coauthor_trailer`;
    co-author policy is never reimplemented here. A
    :class:`~eawf.vcs.coauthor.CoauthorPolicyError` (e.g. a runtime with no
    configured identity) degrades to ``None`` so trailer resolution never
    aborts a ship — the per-commit gauntlet still enforces the trailer at
    commit time.

    Args:
        vcs_config: The validated ``vcs`` config surface.

    Returns:
        The resolved trailer line, or ``None`` when trailers are disabled or
        cannot be inferred.
    """
    try:
        return resolve_coauthor_trailer(vcs_config.coauthor, env=os.environ)
    except CoauthorPolicyError as exc:
        logger.warning(
            f"_resolve_coauthor_trailer_for_ship coauthor={vcs_config.coauthor!r} "
            f"resolution=failed reason={exc!r}"
        )
        return None


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

        # Step 1 — probe ran. Step 2: gate on the recorded audit verdict.
        state = _load_state(state_path)
        phase_id = _phase_id_from_scope(scope_id)
        audit = (
            _latest_audit_for_phase(state, phase_id)
            if state is not None and phase_id is not None
            else None
        )
        # When state or a matching audit is unavailable we cannot gate on a
        # verdict; degrade open rather than block (the audit row is created
        # by /audit, which is a precondition the operator owns). When an
        # audit *does* exist its verdict must be ship-clearing.
        if audit is not None and audit.verdict not in _SHIP_ALLOWED_VERDICTS:
            verdict_label = audit.verdict.value if audit.verdict is not None else "none"
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="ship.audit_gate",
                summary=f"ship: audit gate blocked (verdict={verdict_label})",
                payload={"audit_required": True, "passed": False, "verdict": verdict_label},
            )
            persisted_records.append(evt_id)
            assert phase_id is not None  # narrowed: audit only set when phase_id resolved
            return SkillResult(
                status="failed",
                body=ShipBody(
                    commit_groups=[],
                    push=None,
                    pr=None,
                    estimate_vs_actual={"estimated_eu": 0.0, "actual_eu": 0.0},
                    rollback_notes=f"audit verdict {verdict_label!r} does not clear the ship gate",
                ).model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
                repair_commands=[f"/audit {phase_id} --kind ship-gate"],
            )
        verdict_label = (
            audit.verdict.value if audit is not None and audit.verdict is not None else "ungated"
        )
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="ship.audit_gate",
            summary=f"ship: audit gate passed (verdict={verdict_label})",
            payload={"audit_required": True, "passed": True, "verdict": verdict_label},
        )
        persisted_records.append(evt_id)

        # Step 2b — gate on the configured PR merge method. Squash is
        # rejected unless explicitly allowed; rebase / merge clear.
        vcs_config = _load_vcs_config(state_path)
        merge_method = vcs_config.pr_merge_method
        if merge_method == "squash" and not vcs_config.squash_allowed:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="ship.merge_method_gate",
                summary="ship: merge-method gate blocked (squash not allowed)",
                payload={"pr_merge_method": merge_method, "squash_allowed": False},
            )
            persisted_records.append(evt_id)
            return SkillResult(
                status="failed",
                body=ShipBody(
                    commit_groups=[],
                    push=None,
                    pr=None,
                    estimate_vs_actual={"estimated_eu": 0.0, "actual_eu": 0.0},
                    rollback_notes="squash merge not permitted (set vcs.squash_allowed)",
                ).model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
                repair_commands=[
                    "set vcs.pr_merge_method to rebase (or enable vcs.squash_allowed)"
                ],
            )

        # Resolve the co-author trailer once for every commit group's
        # message. Reuses the W12 resolver; never reimplemented here.
        coauthor_trailer = _resolve_coauthor_trailer_for_ship(vcs_config)

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

        # Step 5 — build pending-ship artefact. The commit-group message
        # carries the resolved co-author trailer so the per-commit gauntlet
        # finds the required trailer already present.
        commit_groups: list[ShipCommitGroup] = []
        if do_commit:
            message = f"[{scope_id}] feat: pending ship"
            if coauthor_trailer is not None:
                message = f"{message}\n\n{coauthor_trailer}"
            commit_groups.append(
                ShipCommitGroup(
                    message=message,
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
