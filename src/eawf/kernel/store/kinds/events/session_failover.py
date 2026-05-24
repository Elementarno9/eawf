"""SessionFailoverPayload — V8 session-failover event payload.

Emitted by the daemon's failover path when a ``--continue`` attempt is
rejected (expired session, deleted log, vendor refusal) and the dispatch
falls back to a fresh session. Records both attempt ids + the failed
handle so a replay can distinguish a continued run from a forced-fresh
restart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict

from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload


class SessionFailoverPayload(TracedEventPayload):
    """Payload for a ``session_failover`` event.

    Attributes:
        event_type: Discriminator tag; always ``"session_failover"``.
        timestamp: When the failover occurred.
        wave_id: ``W<NN>`` wave whose session failed over to fresh.
        attempt_id_continue: Dispatch-attempt id of the rejected
            ``--continue`` attempt.
        attempt_id_fresh: Dispatch-attempt id of the fresh-session
            replacement.
        runtime: Runtime whose session failed over.
        reason: Why the continue was rejected.
        prior_session_handle: The session handle that failed
            ``--continue``.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["session_failover"] = "session_failover"
    timestamp: datetime
    wave_id: str
    attempt_id_continue: str
    attempt_id_fresh: str
    runtime: RuntimeTriple
    reason: Literal["session_expired", "file_deleted", "continue_rejected", "other"]
    prior_session_handle: str
