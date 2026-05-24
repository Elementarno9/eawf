"""Mapping table from Eä HookEventType to Codex CLI hook names (P14-W06).

Codex exposes a different hook event vocabulary than Claude Code; this
module pins the translation so the install renderer never inlines a
literal mapping. Adding a new hook event requires extending the table
plus the matching :class:`HookEventType` member upstream.
"""

from __future__ import annotations

from typing import Final

from eawf.runtime.hooks.event import HookEventType

# Codex names mirror the upstream session-event vocabulary documented in
# the Codex CLI agent SDK. Each Eä HookEventType maps to one Codex hook
# script filename (without extension) under ``.codex/hooks/``.
CODEX_HOOK_NAMES: Final[dict[HookEventType, str]] = {event: event.value for event in HookEventType}


def codex_hook_name(event_type: HookEventType) -> str:
    """Return the Codex-side hook script stem for *event_type*."""
    return CODEX_HOOK_NAMES[event_type]
