"""C09-owned typed ``EventPayload`` discriminated-union sub-classes.

Per the C09 quality-observability spec §5.11, today's flat
``EventPayload.event_type: str`` is promoted to a typed discriminated
union. C03 owns the union shape across every event kind; C09 contributes
the payload sub-classes for the runtime, session, cost, and cache-alarm
event families.

Each sub-class:

* declares a closed ``event_type`` :class:`~typing.Literal` that doubles
  as the Pydantic discriminator tag, and
* derives from :class:`TracedEventPayload` so the §5.8 correlation-ID
  chain (``trace_request_id`` / ``trace_wave_id`` / ``trace_attempt_id``)
  is carried uniformly on every C09 payload.

:data:`C09EventPayloadUnion` is the C09-local discriminated union over
the sub-classes this module owns. The eventual project-wide union (owned
by C03) folds these in alongside the remaining catalog rows; this local
union lets C09 emit / ingest its own payload families with fail-fast
discriminator dispatch ahead of that consolidation.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from eawf.store.kinds.events.base import TracedEventPayload
from eawf.store.kinds.events.cache_mislayer import CacheMislayerAlarmPayload
from eawf.store.kinds.events.dispatch_cost import DispatchCostPayload
from eawf.store.kinds.events.runtime_switched import RuntimeSwitchedPayload
from eawf.store.kinds.events.session_continued import SessionContinuedPayload
from eawf.store.kinds.events.session_failover import SessionFailoverPayload

C09EventPayloadUnion = Annotated[
    RuntimeSwitchedPayload
    | SessionContinuedPayload
    | SessionFailoverPayload
    | DispatchCostPayload
    | CacheMislayerAlarmPayload,
    Field(discriminator="event_type"),
]
"""C09-local discriminated union keyed on ``event_type``.

Validation dispatches to the matching sub-class by the ``event_type``
tag; a payload whose body does not match its tag fails fast with a
:class:`pydantic.ValidationError` at append time rather than at
projection time.
"""

__all__ = [
    "C09EventPayloadUnion",
    "CacheMislayerAlarmPayload",
    "DispatchCostPayload",
    "RuntimeSwitchedPayload",
    "SessionContinuedPayload",
    "SessionFailoverPayload",
    "TracedEventPayload",
]
