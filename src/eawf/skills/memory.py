"""``/memory`` skill — read/write/list curated durable memory records.

``/memory`` is the skill surface over the tiered-memory store. The
three verbs map onto the operator-facing ``eawf memory`` CLI:

- ``/memory save <name>``  → records a WORKING-tier entry intent.
- ``/memory list``         → enumerates the current entries.
- ``/memory forget <name>``→ soft-deletes (prunes) an entry.

Per the authority map, the daemon is the sole canonical writer of the
memory JSONL store; the skill therefore does **not** mutate the store
itself. It validates + normalises the requested operation, appends a
single append-only ``EVENT`` describing the intent, and routes the
operator to the canonical ``eawf memory`` writer via
``next_valid_actions``. The memory tier (``WORKING|ARCHIVAL|RETRIEVAL``)
is surfaced on the body so a downstream dispatch can carry it through.

Honoured args:

- ``verb`` — ``save`` (default) / ``list`` / ``forget``.
- ``name`` — entry name; required for ``save`` / ``forget`` (a missing
  name on those verbs degrades to ``status=needs_user``).
- ``tier`` — ``working`` (default) / ``archival`` / ``retrieval``.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.kernel.state.enums import MemoryTier
from eawf.render.envelope import SkillName
from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register

logger = logging.getLogger(__name__)


MANIFEST = SkillManifest(
    name="/memory",
    description="Save, list, or forget curated durable memory entries.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "continue"},
    output_envelope_kind="memory_operation",
)

_VALID_VERBS: frozenset[str] = frozenset({"save", "list", "forget"})
_DEFAULT_VERB = "save"

# Verbs that operate on a single named entry and therefore require ``name``.
_NAMED_VERBS: frozenset[str] = frozenset({"save", "forget"})

# Map each verb to the canonical ``eawf memory`` writer command.
_VERB_TO_CLI: dict[str, str] = {
    "save": "eawf memory add",
    "list": "eawf memory list",
    "forget": "eawf memory prune",
}


def _coerce_verb(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in _VALID_VERBS:
        return value.strip().lower()
    return _DEFAULT_VERB


def _coerce_tier(value: Any) -> MemoryTier:
    if isinstance(value, str):
        try:
            return MemoryTier(value.strip().lower())
        except ValueError:
            return MemoryTier.WORKING
    return MemoryTier.WORKING


@register
class MemorySkill(Skill):
    """Concrete ``/memory`` skill."""

    name: SkillName = "/memory"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        verb = _coerce_verb(args.get("verb"))
        name = args.get("name")
        name_str = str(name) if name else None
        tier = _coerce_tier(args.get("tier"))
        cli = _VERB_TO_CLI[verb]

        if verb in _NAMED_VERBS and not name_str:
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "memory_operation",
                    "verb": verb,
                    "name": None,
                    "tier": tier.value,
                    "reason": f"{verb!r} requires a memory entry name",
                },
                next_valid_actions=[f"{cli} <name>"],
            )

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type=f"memory.{verb}",
            summary=f"memory: {verb} intent for {name_str or '*'}",
            payload={"verb": verb, "name": name_str, "tier": tier.value},
        )

        return SkillResult(
            status="ok",
            body={
                "kind": "memory_operation",
                "verb": verb,
                "name": name_str,
                "tier": tier.value,
            },
            persisted_store_records=[evt_id],
            next_valid_actions=[cli if name_str is None else f"{cli} {name_str}"],
        )


__all__ = ["MANIFEST", "MemorySkill"]
