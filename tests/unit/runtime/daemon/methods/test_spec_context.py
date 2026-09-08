"""Contract tests for the shared ``spec.*`` mutator plumbing.

The four helpers here used to be private names inside
:mod:`eawf.runtime.daemon.methods.spec`, reached across the package by the
convert and repoint modules. They are now a public collaborator that three
mutator modules depend on, so their boundaries (a missing key, a ``None``
key, an expired row) and their error path (a post-mutation payload that
fails validation) are pinned directly rather than only through whichever
spec verb happens to exercise them.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec_context import (
    IDEMPOTENCY_TTL_SECONDS,
    CachedSpecMutation,
    cache_replay,
    evict_expired,
    idempotency_cache,
    idempotent_replay,
    publish_envelope,
    validate_post_sync,
)


class _RecordingBus:
    """Minimal stand-in for the daemon subscription bus."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, envelope: Any) -> None:
        self.published.append(envelope)


class _BusWithoutPublish:
    """A bus-shaped object that does not implement ``publish``."""


class _Envelope:
    """Envelope stand-in carrying only the id the helper reads."""

    def __init__(self, envelope_id: str) -> None:
        self.id = envelope_id


def _ctx(*, bus: Any = None) -> MethodContext:
    return MethodContext(
        started_at="2026-09-08T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version="1.0",
        version=__version__,
        bus=bus,
    )


def test_idempotency_cache_installs_a_dict_on_a_bare_context() -> None:
    """A context built without a cache slot gets one lazily."""
    ctx = _ctx()
    cache = idempotency_cache(ctx)
    assert cache == {}
    assert ctx.idempotency_cache is cache


def test_idempotency_cache_reuses_the_attached_dict() -> None:
    """A second call returns the same dict, so rows survive across verbs."""
    ctx = _ctx()
    first = idempotency_cache(ctx)
    first["K"] = CachedSpecMutation(result={"ok": True}, cached_at=time.monotonic())
    assert idempotency_cache(ctx) is first


def test_evict_expired_drops_only_rows_past_the_ttl() -> None:
    """Off-by-one boundary: a row exactly at the TTL is kept, past it is dropped."""
    now = 1_000.0
    cache: dict[str, Any] = {
        "fresh": CachedSpecMutation(result={}, cached_at=now),
        "at_ttl": CachedSpecMutation(result={}, cached_at=now - IDEMPOTENCY_TTL_SECONDS),
        "expired": CachedSpecMutation(result={}, cached_at=now - IDEMPOTENCY_TTL_SECONDS - 0.001),
    }
    evict_expired(cache, now=now)
    assert sorted(cache) == ["at_ttl", "fresh"]


def test_evict_expired_leaves_an_empty_cache_empty() -> None:
    """Empty boundary: pruning nothing is a no-op rather than an error."""
    cache: dict[str, Any] = {}
    evict_expired(cache, now=time.monotonic())
    assert cache == {}


def test_evict_expired_keeps_rows_with_no_cached_at() -> None:
    """A foreign row (another mutator family's shape) is never evicted here."""
    cache: dict[str, Any] = {"foreign": object()}
    evict_expired(cache, now=time.monotonic())
    assert sorted(cache) == ["foreign"]


def test_cache_replay_round_trips_through_idempotent_replay() -> None:
    """A cached result replays with the ``idempotent_replay`` flag set."""
    ctx = _ctx()
    cache_replay(ctx, idempotency_key="K1", result={"operation": "sync"})
    assert idempotent_replay(ctx, "K1") == {"operation": "sync", "idempotent_replay": True}


def test_cache_replay_with_no_key_writes_nothing() -> None:
    """``None`` is the opt-out: the call must not create a cache row."""
    ctx = _ctx()
    cache_replay(ctx, idempotency_key=None, result={"operation": "sync"})
    assert idempotency_cache(ctx) == {}


def test_cache_replay_does_not_alias_the_caller_result() -> None:
    """The replay is a copy, so a later mutation cannot rewrite history."""
    ctx = _ctx()
    cache_replay(ctx, idempotency_key="K1", result={"operation": "sync"})
    replayed = idempotent_replay(ctx, "K1")
    assert replayed is not None
    replayed["operation"] = "tampered"
    second = idempotent_replay(ctx, "K1")
    assert second is not None
    assert second["operation"] == "sync"


def test_idempotent_replay_with_no_key_returns_none() -> None:
    """A call that opted out of replay never matches a cached row."""
    ctx = _ctx()
    cache_replay(ctx, idempotency_key="K1", result={"operation": "sync"})
    assert idempotent_replay(ctx, None) is None


def test_idempotent_replay_missing_key_returns_none() -> None:
    """A key with no row is a miss, not a ``KeyError``."""
    ctx = _ctx()
    assert idempotent_replay(ctx, "absent") is None


def test_idempotent_replay_skips_a_row_of_a_foreign_shape() -> None:
    """The cache is shared with other mutators, so a foreign row is a miss."""
    ctx = _ctx()
    idempotency_cache(ctx)["K1"] = object()
    assert idempotent_replay(ctx, "K1") is None


def test_idempotent_replay_drops_an_expired_row_before_lookup() -> None:
    """An over-TTL row is evicted on the lookup that would have replayed it."""
    ctx = _ctx()
    idempotency_cache(ctx)["K1"] = CachedSpecMutation(
        result={"operation": "sync"},
        cached_at=time.monotonic() - IDEMPOTENCY_TTL_SECONDS - 1.0,
    )
    assert idempotent_replay(ctx, "K1") is None
    assert idempotency_cache(ctx) == {}


def test_cached_spec_mutation_rejects_an_unknown_field() -> None:
    """``extra='forbid'``: an unexpected key is a ``ValidationError``."""
    with pytest.raises(ValidationError):
        CachedSpecMutation(result={}, cached_at=0.0, extra=True)  # type: ignore[call-arg]


def test_cached_spec_mutation_rejects_a_negative_timestamp() -> None:
    """``cached_at`` is a monotonic reading, so below zero is out of range."""
    with pytest.raises(ValidationError):
        CachedSpecMutation(result={}, cached_at=-1.0)


def test_publish_envelope_sends_on_a_bus_and_moves_the_cursor() -> None:
    """The envelope reaches the bus and becomes the context's last event."""
    bus = _RecordingBus()
    ctx = _ctx(bus=bus)
    envelope = _Envelope("SPEC-abc123")
    publish_envelope(ctx, envelope)
    assert bus.published == [envelope]
    assert ctx.last_event_id == "SPEC-abc123"


def test_publish_envelope_without_a_bus_still_moves_the_cursor() -> None:
    """Boundary: a daemonless context has no bus, and the write still counts."""
    ctx = _ctx(bus=None)
    publish_envelope(ctx, _Envelope("SPEC-def456"))
    assert ctx.last_event_id == "SPEC-def456"


def test_publish_envelope_tolerates_a_bus_without_publish() -> None:
    """A legacy bus object missing ``publish`` must not raise mid-mutation."""
    ctx = _ctx(bus=_BusWithoutPublish())
    publish_envelope(ctx, _Envelope("SPEC-ghi789"))
    assert ctx.last_event_id == "SPEC-ghi789"


def test_validate_post_sync_rejects_a_schema_invalid_payload() -> None:
    """An unparseable payload is a ``DaemonValidationError``, not a crash."""
    with pytest.raises(DaemonValidationError, match="post-mutation schema invalid"):
        validate_post_sync({"not": "a state document"})


def test_validate_post_sync_rejects_an_empty_payload() -> None:
    """Empty boundary: ``{}`` carries no schema version and is refused."""
    with pytest.raises(DaemonValidationError, match="post-mutation schema invalid"):
        validate_post_sync({})
