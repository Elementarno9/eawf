"""Unit tests for :mod:`eawf.daemon.bus`.

The bus is a process-internal in-memory primitive; tests drive it
directly without an asyncio server, except for the
``iter_subscriber_pushes`` happy path which needs the event-loop to
fire :class:`asyncio.Event` notifications.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.daemon.bus import (
    CATCH_UP_MAX,
    CatchUpTooLargeError,
    EventBus,
    Subscriber,
    catch_up,
)
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope

pytestmark = pytest.mark.unit


def _build_envelope(
    env_id: str,
    *,
    kind: StoreKind = StoreKind.EVENT,
    scope_id: str | None = None,
) -> Envelope:
    return Envelope(
        id=env_id,
        kind=kind,
        scope_id=scope_id,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary=f"summary for {env_id}",
        payload={
            "timestamp": "2026-05-19T12:00:00+00:00",
            "event_type": "test",
            "actor": "test",
            "command": "noop",
            "args_hash": "0",
            "status": "ok",
            "message": "test",
        }
        if kind == StoreKind.EVENT
        else {"k": env_id},
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def test_register_returns_subscriber_with_defaults() -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    assert sub.connection_id == "c-1"
    assert sub.scope_filter is None
    assert sub.kind_filter is None
    assert sub.since_event_id is None
    assert sub.queue.maxlen == 1024
    assert bus.active_subscriptions == 1


def test_register_rejects_duplicate_connection() -> None:
    bus = EventBus()
    bus.register(connection_id="c-1")
    with pytest.raises(ValueError, match="connection already registered"):
        bus.register(connection_id="c-1")


def test_publish_fans_out_in_order_to_one_subscriber() -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    envelopes = [_build_envelope(f"E-{i}") for i in range(3)]
    for env in envelopes:
        bus.publish(env)
    assert [e.id for e in sub.queue] == ["E-0", "E-1", "E-2"]


def test_publish_skips_subscribers_filtered_by_scope() -> None:
    bus = EventBus()
    matching = bus.register(connection_id="c-match", scope_filter="repo:foo")
    other = bus.register(connection_id="c-other", scope_filter="repo:bar")
    bus.publish(_build_envelope("E-0", scope_id="repo:foo"))
    bus.publish(_build_envelope("E-1", scope_id="repo:bar"))
    assert [e.id for e in matching.queue] == ["E-0"]
    assert [e.id for e in other.queue] == ["E-1"]


def test_publish_skips_subscribers_filtered_by_kind() -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1", kind_filter=[StoreKind.EVENT])
    bus.publish(_build_envelope("E-keep"))
    audit_env = Envelope(
        id="A-skip",
        kind=StoreKind.AUDIT,
        scope_id=None,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="audit",
        payload={
            "audit_id": "AUD-1",
            "hypothesis_id": "H01-01",
            "kind": "review",
            "status": "pending",
        },
    )
    bus.publish(audit_env)
    assert [e.id for e in sub.queue] == ["E-keep"]


def test_publish_drop_oldest_emits_lag_envelope() -> None:
    bus = EventBus(queue_size=4)
    sub = bus.register(connection_id="c-1")
    # Fill exactly to maxlen.
    for i in range(4):
        bus.publish(_build_envelope(f"E-{i}"))
    assert len(sub.queue) == 4
    assert sub.dropped_count == 0
    # One more triggers drop-oldest + lag insertion.
    bus.publish(_build_envelope("E-4"))
    # Queue shape after overflow:
    #   evict E-0, append E-4, append LAG; LAG itself evicts E-1.
    ids = [e.id for e in sub.queue]
    assert ids[0] == "E-2"
    assert ids[1] == "E-3"
    assert ids[2] == "E-4"
    assert ids[3].startswith("LAG-")
    assert sub.dropped_count == 2
    assert sub.last_dropped_id == "E-1"
    # Lag payload carries the first-dropped id (E-0), not the second.
    lag = sub.queue[3]
    assert lag.kind == StoreKind.SUBSCRIPTION_LAG
    assert lag.payload == {"dropped_count": 1, "last_event_id": "E-0"}


def test_publish_drop_oldest_under_one_capacity_unit() -> None:
    """Sliding window keeps the most-recent envelope when overflow happens.

    Distinct from the previous test: this exercises the drop-oldest
    path when the queue is sized so the *new* envelope plus the lag
    notice fit without secondary eviction.
    """
    bus = EventBus(queue_size=2)
    sub = bus.register(connection_id="c-1")
    bus.publish(_build_envelope("E-0"))
    bus.publish(_build_envelope("E-1"))
    bus.publish(_build_envelope("E-2"))
    # Eviction sequence:
    #  - queue is [E-0, E-1]; new E-2 arrives.
    #  - drop E-0; append E-2 → [E-1, E-2].
    #  - lag inserted; queue at capacity → drop E-1; append LAG.
    ids = [e.id for e in sub.queue]
    assert ids[0] == "E-2"
    assert ids[1].startswith("LAG-")
    assert sub.queue[1].payload == {"dropped_count": 1, "last_event_id": "E-0"}


def test_unregister_clears_queue_and_marks_closed() -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    bus.publish(_build_envelope("E-0"))
    bus.unregister("c-1")
    assert sub.closed is True
    assert len(sub.queue) == 0
    assert bus.active_subscriptions == 0


def test_unregister_unknown_connection_is_noop() -> None:
    bus = EventBus()
    # Must not raise — connection-teardown calls this unconditionally.
    bus.unregister("does-not-exist")


def test_iter_subscriber_pushes_drains_queue_in_order() -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")

    async def body() -> None:
        envelopes = [_build_envelope(f"E-{i}") for i in range(3)]
        for env in envelopes:
            bus.publish(env)
        received: list[str] = []
        async for env in bus.iter_subscriber_pushes(sub):
            received.append(env.id)
            if len(received) == 3:
                bus.unregister("c-1")
        assert received == ["E-0", "E-1", "E-2"]

    _run(body)


def _write_event_jsonl(path: Path, envelopes: list[Envelope]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for env in envelopes:
            fh.write(orjson.dumps(env.model_dump(mode="json")) + b"\n")


def test_catch_up_returns_events_strictly_after_since(tmp_path: Path) -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1", since_event_id="E-2")
    envelopes = [_build_envelope(f"E-{i}") for i in range(5)]
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, envelopes)
    out = catch_up(sub, event_path)
    assert [e.id for e in out] == ["E-3", "E-4"]


def test_catch_up_with_no_since_returns_every_event(tmp_path: Path) -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    envelopes = [_build_envelope(f"E-{i}") for i in range(3)]
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, envelopes)
    out = catch_up(sub, event_path)
    assert [e.id for e in out] == ["E-0", "E-1", "E-2"]


def test_catch_up_honours_subscriber_filters(tmp_path: Path) -> None:
    bus = EventBus()
    sub = bus.register(
        connection_id="c-1",
        scope_filter="repo:foo",
        since_event_id="E-0",
    )
    envelopes = [
        _build_envelope("E-0"),
        _build_envelope("E-1", scope_id="repo:foo"),
        _build_envelope("E-2", scope_id="repo:bar"),
        _build_envelope("E-3", scope_id="repo:foo"),
    ]
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, envelopes)
    out = catch_up(sub, event_path)
    assert [e.id for e in out] == ["E-1", "E-3"]


def test_catch_up_raises_when_bound_exceeded(tmp_path: Path) -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    envelopes = [_build_envelope(f"E-{i}") for i in range(5)]
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, envelopes)
    with pytest.raises(CatchUpTooLargeError):
        catch_up(sub, event_path, max_events=3)


def test_catch_up_max_default_constant() -> None:
    """Sanity-check the public constant matches the spec (C02 §5.7)."""
    assert CATCH_UP_MAX == 10000


def test_catch_up_returns_empty_for_missing_file(tmp_path: Path) -> None:
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    out = catch_up(sub, tmp_path / "missing.jsonl")
    assert out == []


def test_matches_subscriber_filter_combinations() -> None:
    sub = Subscriber(
        connection_id="c-1",
        scope_filter="repo:foo",
        kind_filter=[StoreKind.EVENT],
    )
    matching = _build_envelope("E-yes", scope_id="repo:foo")
    wrong_scope = _build_envelope("E-no-scope", scope_id="repo:bar")
    wrong_kind = Envelope(
        id="A-no-kind",
        kind=StoreKind.AUDIT,
        scope_id="repo:foo",
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="audit",
        payload={
            "audit_id": "AUD-1",
            "hypothesis_id": "H01-01",
            "kind": "review",
            "status": "pending",
        },
    )
    assert sub.matches(matching) is True
    assert sub.matches(wrong_scope) is False
    assert sub.matches(wrong_kind) is False
