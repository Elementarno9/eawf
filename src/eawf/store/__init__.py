"""eawf JSONL store package.

Re-exports the canonical surface so callers can write::

    from eawf.store import Event, EventPayload, EventKind, append_envelope

instead of reaching into submodules. Per C07b (Q14 / D14) the
:class:`Event` model is the single source of truth for event envelopes:
C02 streaming, C06 reactivity, C09 telemetry, and C11 webhook ingress
all consume this shape rather than defining their own.
"""

from __future__ import annotations

from eawf.state.enums import StoreKind
from eawf.store.append import append_envelope
from eawf.store.compact import CompactReport, compact_store
from eawf.store.envelope import Envelope
from eawf.store.kinds import PAYLOAD_MODELS
from eawf.store.kinds.event import Event, EventKind, EventPayload
from eawf.store.paths import store_dir, store_path, store_paths

__all__ = [
    "PAYLOAD_MODELS",
    "CompactReport",
    "Envelope",
    "Event",
    "EventKind",
    "EventPayload",
    "StoreKind",
    "append_envelope",
    "compact_store",
    "store_dir",
    "store_path",
    "store_paths",
]
