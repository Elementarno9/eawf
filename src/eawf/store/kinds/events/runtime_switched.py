"""RuntimeSwitchedPayload — V5 runtime-fallback event payload.

Emitted by the daemon's dispatch runner when a wave's runtime is
switched mid-flight (the V5 fallback ladder), e.g. on a vendor 5xx,
timeout, or operator-driven manual swap. Carries the from/to runtime +
attempt ids so a replay can trace the switchover chain, plus a scrubbed
error detail for diagnosis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict

from eawf.store.kinds.events.base import RuntimeTriple, TracedEventPayload


class RuntimeSwitchedPayload(TracedEventPayload):
    """Payload for a ``runtime_switched`` event.

    Attributes:
        event_type: Discriminator tag; always ``"runtime_switched"``.
        timestamp: When the switchover occurred.
        wave_id: ``W<NN>`` wave whose dispatch switched runtimes.
        attempt_id_from: Dispatch-attempt id that failed / was
            superseded.
        attempt_id_to: Dispatch-attempt id minted for the replacement
            runtime.
        runtime_from: Runtime that was switched away from.
        runtime_to: Runtime that was switched to.
        cause: Error-class string that triggered the switch (scrubbed,
            daemon-stamped). Tightens to the typed ``RuntimeErrorClass``
            enum once the telemetry-models wave lands it.
        error_detail: Scrubbed stderr / failure detail for diagnosis.
        idempotency_key: De-dup key for the switchover event so a retried
            emit does not double-count.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["runtime_switched"] = "runtime_switched"
    timestamp: datetime
    wave_id: str
    attempt_id_from: str
    attempt_id_to: str
    runtime_from: RuntimeTriple
    runtime_to: RuntimeTriple
    cause: str
    error_detail: str
    idempotency_key: str
