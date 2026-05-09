"""``/flow`` skill — composite controller running the six core skills in order.

Per `ea-proposal.md` §14 ``/flow`` drives a one-click ADD iteration:
research → prep → execute (includes audit) → ship. The v0.1 plan §4 W03
constrains this further: ``/flow`` runs all six core skills sequentially
(research → prep → audit → ship → review → polish), accumulating per-step
envelopes under :attr:`FlowBody.steps`.

Short-circuit semantics (the W03 acceptance contract):

- The flow runs each core skill in order.
- After each step the flow inspects ``env.header.status``. Anything other
  than ``ok`` (``needs_user``, ``blocked``, ``failed``, ``partial``)
  triggers an immediate short-circuit. The flow's terminal envelope
  inherits the failing step's ``status`` and ``footer.repair_commands``.
- If every step returns ``ok``, the flow's terminal envelope is ``ok``
  and the body's ``terminal_status`` mirrors the last step's status.

The flow does **not** literally call :func:`eawf.skills.engine.run_skill`
on each subskill — instead it constructs a fresh :class:`SkillContext`
copy and routes through the engine so the per-step envelope is fully
populated (header status, instrument probe, footer mutations). This
keeps the short-circuit decision focused on the canonical envelope
shape rather than the action-side return type.

Honoured ``ctx.args`` keys:

- ``topic`` — free-form description recorded on :attr:`FlowBody.topic`.
- ``stop_after`` — short-circuit before the named step (matches §14's
  ``--stop-after`` flag). v0.1 honours the canonical names
  ``research|prep|audit|ship|review|polish``.
- ``args_per_step`` — optional dict of ``skill_name → ctx.args`` to
  forward to specific steps; absent steps inherit the flow's own args.

The implementation is intentionally explicit: each subskill is an
attribute on the flow class so a test can monkey-patch a single subskill
without rewriting the registry.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.render.envelope import OutputEnvelope, SkillName
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.audit import AuditSkill
from eawf.skills.bodies.flow import FlowBody
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult, run_skill
from eawf.skills.polish import PolishSkill
from eawf.skills.prep import PrepSkill
from eawf.skills.registry import register
from eawf.skills.research import ResearchSkill
from eawf.skills.review import ReviewSkill
from eawf.skills.ship import ShipSkill

logger = logging.getLogger(__name__)


# Canonical core-skill order for ``/flow`` per §14 + v0.1 plan §4 W03.
# The list lives at module level so tests can iterate it without copying
# the order from the docstring.
_CORE_FLOW_ORDER: tuple[tuple[SkillName, type[Skill]], ...] = (
    ("/research", ResearchSkill),
    ("/prep", PrepSkill),
    ("/audit", AuditSkill),
    ("/ship", ShipSkill),
    ("/review", ReviewSkill),
    ("/polish", PolishSkill),
)


def _stop_after_short_name(skill_name: SkillName) -> str:
    """Strip the leading ``/`` so ``stop_after`` can be a bare name.

    Mirrors the §14 ``--stop-after`` flag values: ``research|prep|audit|
    ship|review|polish`` (no leading slash) — the flow honours either
    form so operators don't have to escape the slash in shells.
    """
    return skill_name.lstrip("/")


def _resolve_stop_after(raw: Any) -> str | None:
    """Return a normalised ``stop_after`` short name or ``None``.

    Empty / unrecognised values yield ``None`` (the flow runs the full
    pipeline). Recognised values match a member of :data:`_CORE_FLOW_ORDER`.
    """
    if raw is None:
        return None
    candidate = str(raw).strip().lower().lstrip("/")
    if not candidate:
        return None
    valid = {_stop_after_short_name(name) for name, _ in _CORE_FLOW_ORDER}
    if candidate not in valid:
        return None
    return candidate


def short_circuit_terminal_status(statuses: list[str]) -> str:
    """Compute the flow's terminal status from a sequence of step statuses.

    Contract (mirrored by the test_skill_flow property test):

    - Empty input → ``"ok"`` (the flow ran nothing; nothing failed).
    - First non-``ok`` status wins (short-circuit); the rest of the list
      is ignored.
    - All-``ok`` input → ``"ok"`` (terminal status mirrors the last step).

    Returns:
        The terminal status string.
    """
    for s in statuses:
        if s != "ok":
            return s
    return "ok"


@register
class FlowSkill(Skill):
    """Concrete ``/flow`` skill (Phase 4 W03).

    Runs the six core skills sequentially. Short-circuits on the first
    non-``ok`` envelope status, propagating the failing step's
    ``repair_commands`` to the flow's own footer.
    """

    name: SkillName = "/flow"

    # Canonical step order — exposed so tests can read the sequence
    # without re-importing the module-private tuple.
    flow_order: tuple[tuple[SkillName, type[Skill]], ...] = _CORE_FLOW_ORDER

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)
        topic = args.get("topic")
        stop_after = _resolve_stop_after(args.get("stop_after"))
        args_per_step = args.get("args_per_step") or {}
        if not isinstance(args_per_step, dict):
            args_per_step = {}

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        evidence_refs: list[str] = []

        # Step 1 — flow start.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="flow.start",
            summary=f"flow: start topic={topic!r}",
            payload={
                "topic": topic,
                "stop_after": stop_after,
                "step_count": len(self.flow_order),
            },
        )
        persisted_records.append(evt_id)

        steps: list[dict[str, Any]] = []
        repair_commands: list[str] | None = None
        next_actions: list[str] = ["eawf flow status", "eawf audit"]

        for skill_name, skill_cls in self.flow_order:
            short = _stop_after_short_name(skill_name)
            # Build a per-step context — copy scope/session, fold in
            # both the flow-level args and any step-specific args from
            # ``args_per_step``.
            step_args: dict[str, Any] = {}
            forwarded = args_per_step.get(skill_name) or args_per_step.get(short)
            if isinstance(forwarded, dict):
                step_args.update(forwarded)

            step_ctx = SkillContext(
                scope=ctx.scope,
                session=ctx.session,
                instrument_probe=dict(ctx.instrument_probe),
                args=step_args,
                failure_repair_commands=ctx.failure_repair_commands,
            )

            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="flow.step_start",
                summary=f"flow: step {skill_name} starting",
                payload={"skill": skill_name},
            )
            persisted_records.append(evt_id)

            step_envelope: OutputEnvelope = run_skill(skill_cls(), step_ctx)
            step_status = step_envelope.header.status
            steps.append(step_envelope.model_dump(mode="json"))
            # Carry the step's footer references into the flow envelope.
            persisted_records.extend(step_envelope.footer.persisted_store_records)
            state_mutations.extend(step_envelope.footer.state_mutations)
            evidence_refs.extend(step_envelope.footer.evidence_refs)

            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="flow.step_end",
                summary=f"flow: step {skill_name} -> {step_status}",
                payload={"skill": skill_name, "status": step_status},
            )
            persisted_records.append(evt_id)

            # Short-circuit on first non-ok status.
            if step_status != "ok":
                repair_commands = list(step_envelope.footer.repair_commands or [])
                evt_id = emit_event(
                    state_path=state_path,
                    scope_id=scope_id,
                    event_type="flow.short_circuit",
                    summary=f"flow: short-circuit on {skill_name} ({step_status})",
                    payload={"skill": skill_name, "status": step_status},
                )
                persisted_records.append(evt_id)
                break

            # ``stop_after`` honours the §14 flag. Stops cleanly with
            # the last-run step's status (``ok``).
            if stop_after is not None and stop_after == short:
                evt_id = emit_event(
                    state_path=state_path,
                    scope_id=scope_id,
                    event_type="flow.stop_after",
                    summary=f"flow: stop-after {short}",
                    payload={"stop_after": stop_after},
                )
                persisted_records.append(evt_id)
                break

        terminal_status = short_circuit_terminal_status([s["header"]["status"] for s in steps])

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="flow.end",
            summary=f"flow: end terminal_status={terminal_status}",
            payload={
                "terminal_status": terminal_status,
                "steps_run": len(steps),
            },
        )
        persisted_records.append(evt_id)

        body = FlowBody(
            topic=str(topic) if topic is not None else None,
            steps=steps,
            terminal_status=terminal_status,
            user_question=None,
        )

        return SkillResult(
            status=terminal_status,  # type: ignore[arg-type]
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            evidence_refs=evidence_refs,
            next_valid_actions=next_actions,
            repair_commands=repair_commands,
        )


__all__ = ["FlowSkill", "short_circuit_terminal_status"]
