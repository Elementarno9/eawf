"""Codex hook-package event selection and name mapping.

Only lifecycle events consumed by Eä are installed. Codex hook configuration
uses PascalCase provider event names while Eä's CLI uses lowercase
:class:`HookEventType` values. Keeping both names explicit prevents internal
lifecycle names from leaking into ``hooks/hooks.json``.
"""

from __future__ import annotations

from typing import Final

from eawf.runtime.hooks.event import HookEventType

CODEX_HOOK_EVENT_NAMES: Final[dict[HookEventType, str]] = {
    HookEventType.SESSION_START: "SessionStart",
    HookEventType.SUBAGENT_START: "SubagentStart",
    HookEventType.SUBAGENT_STOP: "SubagentStop",
    HookEventType.SESSION_END: "SessionEnd",
}

CODEX_HOOK_EVENT_TYPES: Final[tuple[HookEventType, ...]] = tuple(CODEX_HOOK_EVENT_NAMES)


def codex_hook_name(event_type: HookEventType) -> str:
    """Return the managed shell-script stem for *event_type*."""
    if event_type not in CODEX_HOOK_EVENT_NAMES:
        raise KeyError(f"unsupported Codex hook event: {event_type.value!r}")
    return event_type.value


def codex_hook_event_name(event_type: HookEventType) -> str:
    """Return the provider's PascalCase event name for *event_type*."""
    return CODEX_HOOK_EVENT_NAMES[event_type]


__all__ = [
    "CODEX_HOOK_EVENT_NAMES",
    "CODEX_HOOK_EVENT_TYPES",
    "codex_hook_event_name",
    "codex_hook_name",
]
