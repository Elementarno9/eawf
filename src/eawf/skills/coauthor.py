"""``/coauthor`` skill — resolve the ``Co-Authored-By:`` trailer policy.

``/coauthor`` is a thin skill-surface wrapper over the already-shipped
co-author policy machinery:

- :class:`eawf.vcs.coauthor.CoauthorConfig` — the validated
  ``vcs.coauthor`` config block (``mode`` ∈ ``runtime|project|disabled``).
- :func:`eawf.vcs.coauthor.resolve_coauthor_trailer` — the canonical
  resolver that turns a config + the explicit runtime opt-in into a
  trailer line (or ``None`` when trailers are disabled).
- :func:`eawf.runtimes.coauthor.resolve_runtime_explicit` — the
  KISS-001 explicit-opt-in runtime resolver consulted by the resolver
  above.

The skill does **not** reimplement any of that logic; it reads the
caller-supplied ``mode`` / ``runtime`` args (defaulting to the
config defaults), runs the resolver, and folds the outcome into a
dict envelope body. Escalation: when ``mode=runtime`` and no runtime
can be resolved (no explicit opt-in and no usable default identity),
the skill degrades to ``status=needs_user`` so the runtime adapter
surfaces a ``coauthor resolve`` AskUserQuestion rather than guessing.

Honoured args:

- ``mode`` — ``runtime`` (default) / ``project`` / ``disabled``;
  overrides ``CoauthorConfig.mode`` for this resolution.
- ``runtime`` — explicit runtime id (``claude`` / ``codex`` / …);
  takes precedence over env-var detection.
- ``message_text`` — optional commit/PR text; ``disabled`` mode
  rejects an existing trailer in it (mirrors the CLI surface).
"""

from __future__ import annotations

import logging
from typing import Any, cast

from eawf.render.envelope import SkillName
from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register
from eawf.vcs.coauthor import (
    CoauthorConfig,
    CoauthorMode,
    CoauthorPolicyError,
    resolve_coauthor_trailer,
)

logger = logging.getLogger(__name__)


MANIFEST = SkillManifest(
    name="/coauthor",
    description="Resolve the Co-Authored-By trailer policy for the active repo.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "reuse"},
    output_envelope_kind="coauthor_resolution",
)

_VALID_MODES: frozenset[str] = frozenset({"runtime", "project", "disabled"})


def _coerce_mode(value: Any, default: CoauthorMode) -> CoauthorMode:
    """Coerce a caller-supplied ``mode`` arg into the closed literal.

    Unknown / unparseable values fall back to *default* so a stray arg
    never crashes the skill; the resolver still enforces the policy.
    """
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in _VALID_MODES:
            return cast(CoauthorMode, normalised)
    return default


@register
class CoauthorSkill(Skill):
    """Concrete ``/coauthor`` skill."""

    name: SkillName = "/coauthor"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        config = CoauthorConfig()
        mode = _coerce_mode(args.get("mode"), config.mode)
        config = config.model_copy(update={"mode": mode})
        runtime = args.get("runtime")
        runtime_str = str(runtime) if runtime else None
        message_text = args.get("message_text")
        message_str = str(message_text) if message_text else None

        persisted_records: list[str] = []
        try:
            trailer = resolve_coauthor_trailer(
                config,
                runtime=runtime_str,
                message_text=message_str,
            )
        except CoauthorPolicyError as exc:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="coauthor.policy_rejected",
                summary=f"coauthor: policy rejected ({mode})",
                payload={"mode": mode, "reason": str(exc)},
            )
            persisted_records.append(evt_id)
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "coauthor_resolution",
                    "mode": mode,
                    "runtime": runtime_str,
                    "trailer": None,
                    "reason": str(exc),
                },
                persisted_store_records=persisted_records,
                next_valid_actions=["eawf coauthor resolve"],
            )

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="coauthor.resolve",
            summary=f"coauthor: resolved trailer (mode={mode})",
            payload={"mode": mode, "trailer_present": trailer is not None},
        )
        persisted_records.append(evt_id)

        return SkillResult(
            status="ok",
            body={
                "kind": "coauthor_resolution",
                "mode": mode,
                "runtime": runtime_str,
                "trailer": trailer,
            },
            persisted_store_records=persisted_records,
            next_valid_actions=["eawf coauthor resolve"],
        )


__all__ = ["MANIFEST", "CoauthorSkill"]
