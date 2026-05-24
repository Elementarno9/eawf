"""eawf JSONL store package.

Re-exports the canonical surface so callers can write::

    from eawf.kernel.store import Event, EventPayload, EventKind, append_envelope

instead of reaching into submodules. Per C07b (Q14 / D14) the
:class:`Event` model is the single source of truth for event envelopes:
C02 streaming, C06 reactivity, C09 telemetry, and C11 webhook ingress
all consume this shape rather than defining their own.
"""

from __future__ import annotations

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.compact import CompactReport, compact_store
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.event import Event, EventKind, EventPayload
from eawf.kernel.store.paths import store_dir, store_path, store_paths

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
