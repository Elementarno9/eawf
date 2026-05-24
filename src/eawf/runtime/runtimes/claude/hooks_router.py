"""Translate Claude Code hook payloads into Eä :class:`HookEvent`.

Claude Code emits hook payloads with a stable ``hook_event_name`` field
identifying the event ("PreToolUse", "PostToolUse", "SessionStart",
"Stop", etc., per the Claude Code hooks reference). This module is the
lossless translation layer: it inspects the payload, picks the correct
:class:`~eawf.runtime.hooks.event.HookEventType`, and returns a typed
:class:`HookEvent`.

Behaviour rules per Phase 4 W04 design spec §3.3 / acceptance §2:

- Recognised payloads → fully populated :class:`HookEvent` (the original
  Claude payload is preserved verbatim under
  ``payloads["claude_code"]`` so downstream consumers can read raw
  fields without re-parsing).
- Unrecognised payloads (missing or unknown ``hook_event_name``) →
  ``None`` and a single ``logging.warning(...)`` entry. We MUST NOT
  raise from the router so a bad upstream payload cannot crash an Eä
  CLI invocation.
- Mapping is intentionally narrow: only the payloads that have a
  well-defined Eä counterpart in v1 :class:`HookEventType` map. Claude
  events without an Eä counterpart (``PreCompact``, ``Notification``,
  ``UserPromptSubmit``, ``SubagentStop``) are warned-and-skipped — W05
  may broaden the mapping later.

The router never reads or writes state. The CLI handler in
:mod:`eawf.cli.commands.hook` consumes the typed event and dispatches
through :class:`~eawf.runtime.hooks.runner.HookRunner`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from eawf.runtime.hooks.event import HookEvent, HookEventType

logger = logging.getLogger(__name__)


# Claude Code → Eä event-type mapping. Keys are the Claude
# ``hook_event_name`` values per the Claude Code hooks reference; values
# are the Eä :class:`HookEventType` we translate them to. Entries are
# limited to events where the v1 Eä semantic is unambiguous.
_CLAUDE_TO_EAWF: dict[str, HookEventType] = {
    "SessionStart": HookEventType.SESSION_START,
    "SessionEnd": HookEventType.SESSION_END,
    "Stop": HookEventType.SESSION_END,
}

# Tool-name → Eä event mapping for ``PreToolUse`` / ``PostToolUse`` payloads.
# Claude emits one hook for any tool call; we translate the common
# ``Bash`` git invocations into our pre/post-commit and pre/post-push
# events so user-supplied commit/push hooks still fire under Claude.
_BASH_PREFIX_TO_PRE: dict[str, HookEventType] = {
    "git commit": HookEventType.PRE_COMMIT,
    "git push": HookEventType.PRE_PUSH,
}

_BASH_PREFIX_TO_POST: dict[str, HookEventType] = {
    "git commit": HookEventType.POST_COMMIT,
    "git push": HookEventType.POST_PUSH,
}


def _resolve_bash_event(
    hook_event_name: str,
    tool_input: dict[str, Any],
) -> HookEventType | None:
    """Translate a ``PreToolUse`` / ``PostToolUse`` payload to an Eä event.

    Only the Bash-tool / git-subcommand combinations are handled; every
    other tool returns ``None`` (the router logs a warning and emits no
    event).
    """
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    stripped = command.strip()
    table = _BASH_PREFIX_TO_PRE if hook_event_name == "PreToolUse" else _BASH_PREFIX_TO_POST
    for prefix, event_type in table.items():
        if stripped.startswith(prefix):
            return event_type
    return None


def _scope_from_payload(claude_payload: dict[str, Any]) -> str:
    """Return a best-effort scope string from the Claude payload.

    Claude payloads carry ``cwd`` (working directory), ``session_id``
    and tool-specific metadata. None of these are Eä scope IDs, so v1
    routes the empty string when no Eä-side scope is available — the
    CLI caller fills in ``--scope`` if it knows better. Future passes
    may grow this to read ``.ea/state.json`` and fold in the active
    pointer.
    """
    cwd = claude_payload.get("cwd")
    if isinstance(cwd, str):
        return cwd
    return ""


def route_claude_payload(claude_payload: dict[str, Any]) -> HookEvent | None:
    """Translate *claude_payload* into a typed :class:`HookEvent`.

    Args:
        claude_payload: Raw mapping decoded from the Claude Code hook's
            stdin JSON. Must carry a ``hook_event_name`` key; everything
            else is optional.

    Returns:
        A populated :class:`HookEvent` for recognised payloads, or
        ``None`` for unrecognised / unmapped payloads. ``None`` returns
        emit a single ``logging.warning(f"...")`` entry so the operator
        sees the skipped payload during ``eawf hook run`` debugging.

    Raises:
        Never. Every malformed-payload path returns ``None`` + warning.
        Tests pin the no-raise contract so a future widening of the
        mapping cannot regress the surface accidentally.
    """
    hook_event_name = claude_payload.get("hook_event_name")
    if not isinstance(hook_event_name, str) or not hook_event_name:
        logger.warning(
            f"route_claude_payload missing-hook-event-name "
            f"keys={sorted(claude_payload.keys())[:8]!r}"
        )
        return None

    event_type: HookEventType | None
    if hook_event_name in _CLAUDE_TO_EAWF:
        event_type = _CLAUDE_TO_EAWF[hook_event_name]
    elif hook_event_name in {"PreToolUse", "PostToolUse"}:
        tool_input = claude_payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        event_type = _resolve_bash_event(hook_event_name, tool_input)
        if event_type is None:
            logger.warning(
                f"route_claude_payload tool-use-unmapped "
                f"event={hook_event_name!r} "
                f"tool={claude_payload.get('tool_name')!r}"
            )
            return None
    else:
        logger.warning(
            f"route_claude_payload unknown-hook-event event={hook_event_name!r}; skipping"
        )
        return None

    occurred_at = datetime.now(UTC)
    scope_id = _scope_from_payload(claude_payload)
    return HookEvent(
        event_type=event_type,
        scope_id=scope_id,
        command="",
        args={},
        runtime="claude",
        occurred_at=occurred_at,
        payloads={"claude_code": dict(claude_payload)},
    )


__all__ = [
    "route_claude_payload",
]
