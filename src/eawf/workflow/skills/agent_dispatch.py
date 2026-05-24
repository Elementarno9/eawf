"""``/agent-dispatch`` skill — dispatch a wave to a runtime (V8 reuse).

``/agent-dispatch`` is the skill surface that routes a claimed wave to
a runtime per the V8 hybrid session-reuse contract. The
canonical mutator is the daemon's ``agent.dispatch`` RPC; the skill does
not perform the dispatch itself. It resolves the runtime ladder for the
target wave and folds the resolution into a dict envelope body, routing
the operator to the canonical ``eawf wave dispatch`` writer via
``next_valid_actions``.

Runtime resolution reads the ``Wave.runtime_preference`` ladder — an
ordered ``list[str]`` of runtime ids the planner sized the wave at. The
first element is the preferred runtime; the skill surfaces both the full
ladder and the resolved head so a downstream dispatch can honour the
preference or fall back deterministically. An explicit ``runtime`` arg
overrides the ladder head.

Honoured args:

- ``wave_id`` — the wave to dispatch; required (a missing id degrades
  to ``status=needs_user``).
- ``runtime_preference`` — optional explicit ladder (``list[str]``);
  overrides whatever the caller would otherwise read off the wave.
- ``runtime`` — explicit single runtime id; takes precedence over the
  ladder head.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.render.envelope import EnvelopeStatus, SkillName
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.workflow.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


MANIFEST = SkillManifest(
    name="/agent-dispatch",
    description="Dispatch a claimed wave to a runtime per the V8 session-reuse ladder.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "hybrid"},
    output_envelope_kind="agent_dispatch",
)


def _coerce_ladder(value: Any) -> list[str]:
    """Coerce a caller-supplied ``runtime_preference`` arg into a str list."""
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


@register
class AgentDispatchSkill(Skill):
    """Concrete ``/agent-dispatch`` skill."""

    name: SkillName = "/agent-dispatch"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        wave = args.get("wave_id")
        wave_id = str(wave) if wave else None
        ladder = _coerce_ladder(args.get("runtime_preference"))
        explicit = args.get("runtime")
        explicit_runtime = str(explicit) if explicit else None

        if not wave_id:
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "agent_dispatch",
                    "wave_id": None,
                    "runtime_preference": ladder,
                    "resolved_runtime": None,
                    "reason": "wave_id is required to dispatch a wave",
                },
                next_valid_actions=["eawf wave dispatch <wave-id>"],
            )

        resolved_runtime = explicit_runtime or (ladder[0] if ladder else None)

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="agent.dispatch",
            summary=f"agent-dispatch: wave={wave_id} runtime={resolved_runtime or '?'}",
            payload={
                "wave_id": wave_id,
                "runtime_preference": ladder,
                "resolved_runtime": resolved_runtime,
            },
        )

        # No resolvable runtime is a soft outcome: the dispatch can still
        # proceed against the daemon's default, but we flag it so the
        # operator can pin a preference.
        status: EnvelopeStatus = "ok" if resolved_runtime else "partial"
        return SkillResult(
            status=status,
            body={
                "kind": "agent_dispatch",
                "wave_id": wave_id,
                "runtime_preference": ladder,
                "resolved_runtime": resolved_runtime,
            },
            persisted_store_records=[evt_id],
            next_valid_actions=[f"eawf wave dispatch {wave_id}"],
        )


__all__ = ["MANIFEST", "AgentDispatchSkill"]
