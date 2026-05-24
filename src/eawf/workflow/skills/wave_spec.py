"""``/wave-spec`` skill — scaffold / validate a WaveSpec for a wave.

``/wave-spec`` is the skill surface over the WaveSpec deliverable
document (:class:`eawf.kernel.spec.wave.WaveSpec`). Two verbs map onto the
operator-facing ``eawf spec`` CLI:

- ``/wave-spec init <wave-id>``     → scaffold a new WaveSpec.
- ``/wave-spec validate <wave-id>`` → re-hash + revalidate the spec.

Per the authority map, the daemon owns spec scaffolding + cache
mutation; the skill validates the requested verb + wave id, appends a
single append-only ``EVENT`` describing the intent, and routes the
operator to the canonical ``eawf spec`` writer via
``next_valid_actions``. The Mockup-waiver path is honoured by
threading an optional ``mockup_waiver_reason`` arg through the body so a
downstream scaffold can carry it onto the WaveSpec without forcing an
ASCII mockup for non-UI waves.

Honoured args:

- ``verb`` — ``init`` (default) / ``validate``.
- ``wave_id`` — the target wave id; required (a missing id degrades to
  ``status=needs_user``).
- ``mockup_waiver_reason`` — optional reason carried through for the
  ``init`` verb when the wave touches no UI scope.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.render.envelope import SkillName
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
    name="/wave-spec",
    description="Scaffold or validate a WaveSpec deliverable for a claimed wave.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "continue"},
    output_envelope_kind="wave_spec_operation",
)

_VALID_VERBS: frozenset[str] = frozenset({"init", "validate"})
_DEFAULT_VERB = "init"

_VERB_TO_CLI: dict[str, str] = {
    "init": "eawf spec init",
    "validate": "eawf spec validate",
}


def _coerce_verb(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in _VALID_VERBS:
        return value.strip().lower()
    return _DEFAULT_VERB


@register
class WaveSpecSkill(Skill):
    """Concrete ``/wave-spec`` skill."""

    name: SkillName = "/wave-spec"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        verb = _coerce_verb(args.get("verb"))
        wave = args.get("wave_id")
        wave_id = str(wave) if wave else None
        waiver = args.get("mockup_waiver_reason")
        waiver_str = str(waiver) if waiver else None
        cli = _VERB_TO_CLI[verb]

        if not wave_id:
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "wave_spec_operation",
                    "verb": verb,
                    "wave_id": None,
                    "mockup_waiver_reason": waiver_str,
                    "reason": f"{verb!r} requires a wave id",
                },
                next_valid_actions=[f"{cli} <wave-id>"],
            )

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type=f"wave_spec.{verb}",
            summary=f"wave-spec: {verb} intent for {wave_id}",
            payload={
                "verb": verb,
                "wave_id": wave_id,
                "mockup_waiver_reason": waiver_str,
            },
        )

        return SkillResult(
            status="ok",
            body={
                "kind": "wave_spec_operation",
                "verb": verb,
                "wave_id": wave_id,
                "mockup_waiver_reason": waiver_str,
            },
            persisted_store_records=[evt_id],
            next_valid_actions=[f"{cli} {wave_id}"],
        )


__all__ = ["MANIFEST", "WaveSpecSkill"]
