"""Canonical Event + EventPayload models.

This module is the **single source of truth** for the event envelope
shape used across eawf. Any subsystem that emits, consumes, projects,
or ingests events MUST import ``Event`` / ``EventPayload`` /
``EventKind`` from here — never define its own.

The outer :class:`Event` model is the canonical wrapper consumed by:

* C02 subscription bus (re-exports ``Event``, no separate envelope).
* C06 reactivity surface (consumes ``Event`` via ``event.subscribe``).
* C09 telemetry projector (ingests ``Event`` rows from the JSONL store).
* C11 webhook ingress (maps inbound GitHub / Linear callbacks to
  ``Event`` rows before emit).

The inner :class:`EventPayload` is what lands inside the generic
:class:`eawf.kernel.store.envelope.Envelope` ``payload`` field when the
envelope kind is :attr:`StoreKind.EVENT`. Existing JSONL rows + 100+
construction call sites keep working through the optional
``event_kind`` discriminator: rows without it stay valid; new emitters
populate the closed ``EventKind`` literal so the v0.5+ governance
phase can flip ``event_kind`` to non-optional once every caller has
been migrated.

The ``actor_principal_id`` field is a v0.3-v0.5 placeholder: rows may
carry the principal id when known, but ``actor`` stays the
load-bearing identity string for backward compatibility until the
v0.5+ governance phase renames callers and back-fills the principal
database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.events import (
    C09_EVENT_TYPE_TAGS,
    C09EventPayloadUnion,
    TracedEventPayload,
)

EventKind = Literal[
    "state_mutated",
    "wave_claimed",
    "wave_closed",
    "phase_activated",
    "phase_closed",
    "iter_activated",
    "iter_closed",
    "runtime_switched",
    "runtime_paused",
    "runtime_auth_failed",
    "runtime_unavailable",
    "session_continued",
    "session_failover",
    "session_handle_pruned",
    "cache_mislayer_alarm",
    "dispatch_cost",
    "audit_emitted",
    "memory_appended",
    "spec_validated",
    "config_reloaded",
    "subscription_lag",
    "subscription_dropped",
    "subprocess_oom_killed",
    "daemon_service_enabled",
    "daemon_service_disabled",
    "wal_recovery",
    "git_state_drift_detected",
    "bucket_drift_detected",
]
"""Closed ``EventKind`` literal. Adding a new kind requires a
``schema_version`` bump (planned for v0.5+ when the typed Mutation
discriminated union lands).
"""


class EventPayload(BaseModel):
    """Payload for an event store record.

    Lands inside :class:`eawf.kernel.store.envelope.Envelope.payload` when the
    envelope kind is :attr:`StoreKind.EVENT`. The ``event_kind`` field
    is optional during the v0.3-v0.5 migration window so existing rows
    + the 100+ call sites that pass ``event_type`` keep validating
    without backfill; v0.5+ governance flips ``event_kind`` to
    non-optional once every caller has been migrated.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    event_type: str
    event_kind: EventKind | None = None
    actor: str
    actor_principal_id: str | None = None
    command: str
    args_hash: str
    before_state_version: str | None = None
    after_state_version: str | None = None
    status: str
    message: str
    error_class: str | None = None
    extras: dict[str, str | int | float | bool] = Field(default_factory=dict)


class Event(BaseModel):
    """Canonical event envelope.

    The outer wrapper consumed by C02 streaming, C06 reactivity, C09
    telemetry projection, and C11 webhook ingress. Distinct from the
    generic :class:`eawf.kernel.store.envelope.Envelope`: ``Event`` is the
    in-memory + wire shape used by event-bus subscribers; ``Envelope``
    with ``kind=EVENT`` is the on-disk JSONL row. The two interconvert
    losslessly (``Envelope.payload`` is :class:`EventPayload`,
    ``Envelope.id`` mirrors :attr:`Event.id`, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(min_length=1)]
    scope_id: str
    occurred_at: UtcDatetime
    idempotency_key: str | None = None
    payload: EventPayload


_C09_UNION_ADAPTER: TypeAdapter[TracedEventPayload] = TypeAdapter(C09EventPayloadUnion)


def validate_event_payload(payload: dict[str, Any]) -> BaseModel:
    """Validate a ``StoreKind.EVENT`` envelope payload, typed-aware.

    The event store carries two payload families that share the
    ``event_type`` key but have disjoint field sets:

    * legacy **flat** rows validate against :class:`EventPayload`, whose
      ``event_type`` is an open ``str``; and
    * typed **C09** rows (runtime/session/cost/cache-alarm) validate
      through :data:`~eawf.kernel.store.kinds.events.C09EventPayloadUnion`,
      whose ``event_type`` is a closed discriminator literal.

    A single ``Field(discriminator=...)`` union cannot fold both families
    (the flat arm's open ``str`` tag has no single literal to key on), so
    validation dispatches on ``event_type``: a value in
    :data:`~eawf.kernel.store.kinds.events.C09_EVENT_TYPE_TAGS` routes to the C09
    union; anything else routes to the flat model. Both arms keep
    ``extra="forbid"``, so a genuinely malformed body — including a C09
    tag worn over the wrong shape — still fails fast.

    Args:
        payload: The decoded envelope payload mapping.

    Returns:
        The validated payload model — a C09 union member for typed rows,
        otherwise an :class:`EventPayload`.

    Raises:
        pydantic.ValidationError: When *payload* matches neither the C09
            union arm for its tag nor the flat :class:`EventPayload`.
    """
    if payload.get("event_type") in C09_EVENT_TYPE_TAGS:
        return _C09_UNION_ADAPTER.validate_python(payload)
    return EventPayload.model_validate(payload)
