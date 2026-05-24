"""SessionContinuedPayload — V8 session-continue event payload.

Emitted by the daemon's session-continue path when a wave's dispatch
resumes an existing runtime session (``--continue`` / session-handle
replay) rather than starting fresh. Records the runtime session handle +
log path so the projector can stitch the continued turns onto the prior
session.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict

from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload


class SessionContinuedPayload(TracedEventPayload):
    """Payload for a ``session_continued`` event.

    Attributes:
        event_type: Discriminator tag; always ``"session_continued"``.
        timestamp: When the session was continued.
        wave_id: ``W<NN>`` wave whose dispatch resumed a session.
        attempt_id: Dispatch-attempt id for the continued invocation.
        runtime: Runtime whose session was continued.
        session_handle: Per-runtime session id replayed via
            ``--continue`` (CC / Codex / OpenCode session id).
        session_log_path: On-disk path of the runtime session log.
        prior_turn_count: Turn count of the session at continue time.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["session_continued"] = "session_continued"
    timestamp: datetime
    wave_id: str
    attempt_id: str
    runtime: RuntimeTriple
    session_handle: str
    session_log_path: str
    prior_turn_count: int
