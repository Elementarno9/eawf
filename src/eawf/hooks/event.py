"""Typed :class:`HookEvent` Pydantic model + frozen v1 :class:`HookEventType`.

The hook event carries the canonical metadata every runtime adapter sees
when an Eä lifecycle transition fires. Per Phase 4 W04 design spec §3.3,
the model is frozen for v0.1 with ``extra="forbid"`` so additive
extensions require an explicit schema bump.

Initial v1 :class:`HookEventType` set (extensible additively in later
v0.x releases):

``pre_commit | post_commit | pre_push | post_push | pre_audit |
post_audit | session_start | session_end | wave_open | wave_close |
iter_open | iter_close | phase_open | phase_close | agent_end``.

The optional ``payloads`` mapping carries per-event extension shapes;
``docs/hook-events.md`` enumerates the v1 shape for each
:class:`HookEventType`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.types import UtcDatetime


class HookEventType(StrEnum):
    """Frozen v1 enumeration of Eä hook events.

    Mirrors the Phase 4 W04 design spec §3.3 list. Adding a new event
    requires a `[CORE]` schema bump and an entry in
    ``docs/hook-events.md``; the strict ``extra="forbid"`` on
    :class:`HookEvent` blocks ad-hoc extensions.
    """

    PRE_COMMIT = "pre_commit"
    POST_COMMIT = "post_commit"
    PRE_PUSH = "pre_push"
    POST_PUSH = "post_push"
    PRE_AUDIT = "pre_audit"
    POST_AUDIT = "post_audit"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    WAVE_OPEN = "wave_open"
    WAVE_CLOSE = "wave_close"
    ITER_OPEN = "iter_open"
    ITER_CLOSE = "iter_close"
    PHASE_OPEN = "phase_open"
    PHASE_CLOSE = "phase_close"
    AGENT_END = "agent_end"


# Frozen runtime literal. ``"generic"`` is the catch-all used by the
# stdin-driven ``eawf hook run`` CLI when no specific runtime adapter
# was identified.
HookRuntime = Literal["claude", "codex", "opencode", "generic"]


class HookEvent(BaseModel):
    """Typed canonical hook event.

    Attributes:
        event_type: Frozen :class:`HookEventType` literal.
        scope_id: Eä scope identifier the event was raised inside (for
            example, a wave or iter ID). Empty string is permitted for
            session-level events that pre-date scope resolution.
        command: The originating Eä CLI command string ("eawf wave close",
            etc.). Empty string when the runtime adapter has no
            command-level context (for example, a Claude
            ``SessionStart`` event).
        args: CLI flags as parsed (or runtime-adapter free-form context
            dictionary). JSON-serialisable.
        runtime: Adapter that produced the event.
        occurred_at: UTC timestamp when the runtime adapter raised the
            event. Idempotence is keyed on ``(event_type, scope_id,
            occurred_at)`` per Phase 4 W04 acceptance §4.
        payloads: Per-event extension mapping. Keys are
            :class:`HookEventType` string values; values are
            JSON-serialisable mappings whose v1 shape is documented in
            ``docs/hook-events.md``.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: HookEventType
    scope_id: str = ""
    command: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    runtime: HookRuntime = "generic"
    occurred_at: UtcDatetime
    payloads: dict[str, dict[str, Any]] = Field(default_factory=dict)


__all__ = [
    "HookEvent",
    "HookEventType",
    "HookRuntime",
]
