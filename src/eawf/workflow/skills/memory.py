"""``/memory`` skill — read/write/list curated durable memory records.

``/memory`` is the skill surface over the tiered-memory store. The
three skill verbs map onto real ``eawf memory`` CLI commands:

- ``/memory save <name>``   → ``eawf memory add`` (``<name>`` is the entry
  ``--title``).
- ``/memory list``          → ``eawf memory list``.
- ``/memory forget <name>`` → ``eawf memory prune`` (a scope/age-filtered
  soft-delete; the CLI has no by-name forget, so the routed hint names the
  ``--scope`` filter rather than a positional name).

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
  name on those verbs degrades to ``status=needs_user`` with a typed
  ``user_question`` whose every option says what it does).
- ``tier`` — ``working`` (default) / ``archival`` / ``retrieval``.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.kernel.state.enums import MemoryTier
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.workflow.skills.bodies.memory import MemoryBody
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

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

# Map each skill verb to the canonical ``eawf memory`` CLI command it routes
# to. The leaf command (``add`` / ``list`` / ``prune``) must stay a registered
# ``memory_app`` verb; the skill-vs-CLI drift test pins exactly that.
_VERB_TO_CLI: dict[str, str] = {
    "save": "eawf memory add",
    "list": "eawf memory list",
    "forget": "eawf memory prune",
}


def _next_action_for(verb: str, name: str | None) -> str:
    """Return the flag-correct ``eawf memory`` hint for *verb*.

    The ``add`` and ``prune`` CLI verbs take options, not a positional entry
    name: ``add`` carries the name as ``--title`` and ``prune`` is scope/age
    filtered. The hint therefore renders the real flag shape rather than
    appending a bare positional that the CLI would reject.

    Args:
        verb: One of the skill verbs in :data:`_VALID_VERBS`.
        name: Entry name when supplied; ``None`` for unnamed verbs (``list``).

    Returns:
        A copy-pasteable ``eawf memory`` command hint.
    """
    cli = _VERB_TO_CLI[verb]
    if verb == "save":
        return f"{cli} --title {name}" if name else f"{cli} --title <name>"
    if verb == "forget":
        return f"{cli} --scope <scope>"
    return cli


def missing_name_question(verb: str) -> UserQuestion:
    """Return the typed question a named verb asks when it was given no name.

    The operator either supplies the name, looks at the saved entries first,
    or drops the request; each option says what it does so the choice is
    made on the option text rather than on a guess.

    Args:
        verb: A named verb from :data:`_NAMED_VERBS`.

    Returns:
        A three-option :class:`UserQuestion`.

    Raises:
        ValueError: *verb* does not operate on a named entry.
    """
    if verb not in _NAMED_VERBS:
        raise ValueError(f"{verb!r} does not take a memory entry name")
    return UserQuestion(
        question=f"Which memory entry should /memory {verb} act on?",
        options=[
            UserQuestionOption(
                label="name the entry",
                description=f"Re-invoke /memory {verb} with name=<entry>; the entry name is "
                "the title the store keys it by.",
            ),
            UserQuestionOption(
                label="list entries first",
                description="Run /memory list to see the saved entries, then pick a name.",
            ),
            UserQuestionOption(
                label="cancel",
                description=f"Drop this {verb} request; the memory store is left unchanged.",
            ),
        ],
    )


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

        if verb in _NAMED_VERBS and not name_str:
            body = MemoryBody.model_validate(
                {
                    "verb": verb,
                    "name": None,
                    "tier": tier.value,
                    "reason": f"{verb!r} requires a memory entry name",
                    "user_question": missing_name_question(verb),
                }
            )
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                next_valid_actions=[_next_action_for(verb, None)],
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
            next_valid_actions=[_next_action_for(verb, name_str)],
        )


__all__ = ["MANIFEST", "MemorySkill", "missing_name_question"]
