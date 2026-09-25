"""Codex hook-package event selection and name mapping.

Only lifecycle events consumed by Eä are installed. Codex hook configuration
uses PascalCase provider event names while Eä's CLI uses lowercase
:class:`HookEventType` values. Keeping both names explicit prevents internal
lifecycle names from leaking into ``hooks/hooks.json``.

Every event in :data:`CODEX_HOOK_EVENT_NAMES` genuinely has a runner
callable under the Codex runtime: ``runtime.codex_lifecycle`` handles
SESSION_START / SUBAGENT_START / SUBAGENT_STOP unconditionally on
``register_runtime_capture_hooks``, and SESSION_END is handled by both
``runtime.capture`` and ``session.end_stamp``. A module-level boot guard
verifies this against the real runner registrations rather than trusting
this dict by inspection, so an event added here with no registered
handler fails import instead of shipping an idle wrapper.
"""

from __future__ import annotations

from typing import Final

from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks

CODEX_HOOK_EVENT_NAMES: Final[dict[HookEventType, str]] = {
    HookEventType.SESSION_START: "SessionStart",
    HookEventType.SUBAGENT_START: "SubagentStart",
    HookEventType.SUBAGENT_STOP: "SubagentStop",
    HookEventType.SESSION_END: "SessionEnd",
}

CODEX_HOOK_EVENT_TYPES: Final[tuple[HookEventType, ...]] = tuple(CODEX_HOOK_EVENT_NAMES)


def registered_handler_event_types() -> frozenset[HookEventType]:
    """Return every event type with at least one runner-registered handler.

    Builds a scratch :class:`~eawf.runtime.hooks.runner.HookRunner` and
    registers the real handler set via
    :func:`~eawf.runtime.hooks.runner.register_runtime_capture_hooks`
    rather than hand-listing event names here, so the check below (and any
    caller cross-checking a packaged hook set) reads off the actual
    registry instead of a second hand-kept list.
    """
    scratch = HookRunner()
    register_runtime_capture_hooks(scratch)
    return frozenset(
        event_type for event_type in HookEventType if any(scratch.hooks_for(event_type))
    )


_UNBACKED_EVENTS: Final[frozenset[HookEventType]] = (
    frozenset(CODEX_HOOK_EVENT_NAMES) - registered_handler_event_types()
)
if _UNBACKED_EVENTS:  # pragma: no cover - boot guard
    raise RuntimeError(
        "codex hook_map lists events with no registered handler: "
        f"{sorted(e.value for e in _UNBACKED_EVENTS)}; "
        "drop them from CODEX_HOOK_EVENT_NAMES or register a handler first"
    )


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
    "registered_handler_event_types",
]
