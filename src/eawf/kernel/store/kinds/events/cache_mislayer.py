"""CacheMislayerAlarmPayload — prompt-cache mislayering alarm payload.

Emitted by the daemon's cost projector when two dispatches inside a
sliding window both blow past the cache-creation floor with a high
creation-to-read ratio — the signature of a mislayered prompt prefix
that defeats the runtime's prompt cache. Carries the window config + the
two observed dispatch ratios so the alarm is self-describing for triage.

The defaults (``window_seconds=300``, ``cache_creation_floor_tokens=
2000``, ``ratio_threshold=10.0``) are configurable via
``telemetry.cache_mislayer.*`` config keys; the threshold was raised
from an earlier 4.0 that over-fired on the real corpus.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict

from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload


class CacheMislayerAlarmPayload(TracedEventPayload):
    """Payload for a ``cache_mislayer_alarm`` event.

    Attributes:
        event_type: Discriminator tag; always
            ``"cache_mislayer_alarm"``.
        timestamp: When the alarm fired.
        runtime: Runtime whose dispatches tripped the alarm.
        scope_id: State scope the dispatches ran under, or ``None`` for
            an interactive session.
        window_seconds: Sliding-window width the two dispatches fell
            inside (default 300).
        cache_creation_floor_tokens: Minimum cache-creation tokens a
            dispatch must exceed to count toward the alarm (default
            2000).
        ratio_threshold: Creation-to-read ratio above which a dispatch
            is flagged (default 10.0).
        observed_ratio_a: Creation-to-read ratio of the first flagged
            dispatch in the window.
        observed_ratio_b: Creation-to-read ratio of the second flagged
            dispatch.
        observed_cc_a: Cache-creation tokens for the first dispatch.
        observed_cc_b: Cache-creation tokens for the second dispatch.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["cache_mislayer_alarm"] = "cache_mislayer_alarm"
    timestamp: datetime
    runtime: RuntimeTriple
    scope_id: str | None
    window_seconds: int
    cache_creation_floor_tokens: int
    ratio_threshold: float
    observed_ratio_a: float
    observed_ratio_b: float
    observed_cc_a: int
    observed_cc_b: int
