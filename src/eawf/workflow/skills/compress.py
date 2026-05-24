"""``/compress`` skill — compress the session conversation near the limit.

``/compress`` is the skill surface that compacts a session's
conversation when its context approaches the model limit. It wires to
the V8 cache-control hooks and emits a ``compression_emitted``
event carrying the token counts before and after the pass so the
telemetry projector can chart context pressure over a session.

The skill itself does not run a model summarisation pass in v0.3 — that
fan-out lives behind the runtime adapter's cache-control hook. The skill
records the requested compression (token deltas the adapter supplies as
args), appends the canonical ``compression_emitted`` event, and returns
a dict body with the before/after counts and the realised ratio.

Honoured args:

- ``tokens_before`` — context token count prior to compression
  (required; a missing/zero value degrades to ``status=needs_user``
  because there is nothing to compress against).
- ``tokens_after`` — context token count after compression. Defaults
  to ``tokens_before`` (a no-op ratio of 1.0) when omitted.
- ``runtime`` — canonical runtime id the compression pass targets.
  Defaults to ``claude-code`` (the only runtime exposing a caller-side
  cache-control marker, §5.6); an unknown id degrades to
  ``status=needs_user``. Drives the cache-control wiring recorded in the
  emitted event payload.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.render.envelope import SkillName
from eawf.runtime.runtimes.cache_control import compression_directive
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.workflow.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)

#: Default runtime for a ``/compress`` pass when the caller names none.
#: ``claude-code`` is the only runtime with a caller-side cache-control
#: marker (§5.6), so it is the natural default for the cache-control
#: wiring.
_DEFAULT_RUNTIME = "claude-code"


MANIFEST = SkillManifest(
    name="/compress",
    description="Compress the session conversation when context approaches the limit.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "continue"},
    output_envelope_kind="compression_result",
)

_COMPRESSION_EVENT = "compression_emitted"


def _coerce_tokens(value: Any) -> int | None:
    """Coerce a token-count arg into a non-negative int, or ``None``."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


@register
class CompressSkill(Skill):
    """Concrete ``/compress`` skill."""

    name: SkillName = "/compress"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        tokens_before = _coerce_tokens(args.get("tokens_before"))
        if not tokens_before:
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "compression_result",
                    "tokens_before": None,
                    "tokens_after": None,
                    "ratio": None,
                    "reason": "tokens_before is required and must be > 0",
                },
                next_valid_actions=["eawf skill run /compress"],
            )

        tokens_after = _coerce_tokens(args.get("tokens_after"))
        if tokens_after is None:
            tokens_after = tokens_before
        # Clamp: a compression pass can only shrink (or no-op) the context.
        tokens_after = min(tokens_after, tokens_before)
        ratio = round(tokens_after / tokens_before, 4)

        runtime_id = str(args.get("runtime") or _DEFAULT_RUNTIME)
        # Wire the cache-control side: build the per-runtime compression
        # directive so the emitted event records whether the
        # post-compression prefix carried a caller-side cache-control
        # marker. An unknown runtime id is a caller error, not a crash.
        try:
            directive = compression_directive(
                runtime_id=runtime_id,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        except ValueError:
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "compression_result",
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                    "ratio": ratio,
                    "reason": f"unknown runtime: {runtime_id!r}",
                },
                next_valid_actions=["eawf skill run /compress"],
            )

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type=_COMPRESSION_EVENT,
            summary=f"compress: {tokens_before} -> {tokens_after} tokens (ratio={ratio})",
            payload={
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "ratio": ratio,
                "runtime": directive.runtime_id,
                "cache_control_applied": directive.cache_control_applied,
            },
        )

        return SkillResult(
            status="ok",
            body={
                "kind": "compression_result",
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "ratio": ratio,
                "runtime": directive.runtime_id,
                "cache_control_applied": directive.cache_control_applied,
            },
            persisted_store_records=[evt_id],
            next_valid_actions=["eawf skill run /compress"],
        )


__all__ = ["MANIFEST", "CompressSkill"]
