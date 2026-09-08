"""SessionClosedPayload -- typed payload for a runtime session that ended.

Emitted when a dispatched runtime session closes. The payload carries every
fact the telemetry projection needs to materialise one session row, so a
close event is *self-contained*: the projector never has to pair it with an
earlier open event. That matters because the event store is projected
incrementally by byte offset -- a tail slice can carry the close line
without any line that preceded it, so a pairing projector would silently
drop exactly the sessions an incremental rebuild is supposed to add.

``payload_schema_version`` pins the shape. The pre-bump spelling of a
session close was a flat :class:`~eawf.kernel.store.kinds.event.EventPayload`
row whose session facts lived in the untyped ``extras`` map; such a row
carries no version and no derivable duration, so it is rejected rather than
migrated. Both boundaries reject it instead of reporting success over an
empty session table: the write boundary because
:func:`~eawf.kernel.store.kinds.event.validate_event_payload` routes every
``session_closed`` tag through the typed union, and the read boundary
because the telemetry aggregator raises on a version mismatch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload
from eawf.observability.telemetry.models import EndMarker

SESSION_PAYLOAD_SCHEMA_VERSION: Final[str] = "2.0"
"""Pinned ``payload_schema_version`` of the session-close payload.

Version ``1.0`` names the untyped flat-event spelling that predates this
payload. It is rejected rather than migrated because its session facts were
never recorded in a form a typed row could be derived from -- a migration
would have to invent the timestamps it is supposed to carry.
"""


class SessionClosedPayload(TracedEventPayload):
    """Payload for a ``session_closed`` event.

    Attributes:
        event_type: Discriminator tag; always ``"session_closed"``.
        payload_schema_version: Pinned payload shape version; a value other
            than :data:`SESSION_PAYLOAD_SCHEMA_VERSION` is not projectable.
        timestamp: When the session closed.
        opened_at: When the session opened. Carried on the close event so a
            session row's duration is derivable from one self-contained
            line.
        session_id: Runtime-specific session identifier.
        runtime: Runtime that served the session.
        session_log_handle: Opaque handle resolvable by the daemon to the
            session log. A handle rather than a path because the event store
            is committed to version control and must never carry a
            filesystem path.
        wave_id: ``W<NN>`` wave the session served, or ``None`` for an
            interactive (non-wave) CLI session.
        attempt_id: Dispatch-attempt id, or ``None`` for an interactive
            session with no attempt envelope.
        model: Model identifier the session ran against; ``None`` when the
            runtime never reported one (the row then prices to zero).
        end_marker: How the session ended.
        input_tokens: Non-cached input tokens billed across the session.
        output_tokens: Output tokens billed across the session.
        cache_creation_input_tokens: Tokens written to the prompt cache.
        cache_read_input_tokens: Tokens served from the prompt cache.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["session_closed"] = "session_closed"
    payload_schema_version: Literal["2.0"] = "2.0"
    timestamp: datetime
    opened_at: datetime
    session_id: Annotated[str, Field(min_length=1)]
    runtime: RuntimeTriple
    session_log_handle: Annotated[str, Field(min_length=1)]
    wave_id: str | None = None
    attempt_id: str | None = None
    model: str | None = None
    end_marker: EndMarker
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_read_input_tokens: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _closed_not_before_opened(self) -> SessionClosedPayload:
        """Reject a close that precedes its own open.

        A reversed pair would project a negative duration, which every
        downstream percentile silently drops -- so it fails at the boundary
        instead.
        """
        if self.timestamp < self.opened_at:
            raise ValueError("timestamp cannot precede opened_at")
        return self


__all__ = [
    "SESSION_PAYLOAD_SCHEMA_VERSION",
    "SessionClosedPayload",
]
