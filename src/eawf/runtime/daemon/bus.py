"""Subscription bus + drop-oldest backpressure for the daemon event stream.

The bus implements a **drop-oldest sliding window** per subscriber. Each
subscriber owns a bounded :class:`collections.deque` (``maxlen=1024`` by
default); when a new envelope arrives and the queue is full, the oldest
envelope is evicted and a :class:`StoreKind.SUBSCRIPTION_LAG` envelope is
appended to the same queue *behind* the incoming envelope. The subscriber
sees the lag notice on its next poll and may reconnect with
``since_event_id=<last dropped id>`` to backfill the gap from the
persistent ``event.jsonl``.

The producer (daemon mutator thread) never blocks on a slow subscriber
— ``publish`` is O(subscribers) and non-blocking aside from the per-
subscriber ``asyncio.Event.set()`` notification.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import orjson

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)


#: Default per-subscriber queue length.
DEFAULT_QUEUE_SIZE: Final[int] = 1024

#: Maximum number of events the catch-up reader will replay before
#: raising :class:`CatchUpTooLargeError`. Subscribers exceeding the
#: bound MUST refresh from a state snapshot first.
CATCH_UP_MAX: Final[int] = 10000


class CatchUpTooLargeError(RuntimeError):
    """Raised when :func:`catch_up` would replay more than ``CATCH_UP_MAX`` events.

    The JSON-RPC layer maps this to error code ``-32008
    catch_up_too_large``. The subscriber must fetch a state snapshot
    before reconnecting.
    """


@dataclass
class Subscriber:
    """One live subscription against the bus.

    Attributes:
        connection_id: Stable identifier for the owning connection.
            Used in log lines and to look up the subscriber on
            unregister. Logs use ``connection=<id>`` (no ``_id``
            suffix in log keys per AGENTS rule 17).
        scope_filter: Optional ``scope_id`` filter; when set, only
            envelopes whose ``Envelope.scope_id`` matches are pushed.
        kind_filter: Optional list of :class:`StoreKind` values; when
            set, only envelopes whose ``Envelope.kind`` is in the list
            are pushed.
        since_event_id: Optional starting point for the catch-up phase;
            the bus passes this to :func:`catch_up` once before live
            push begins.
        queue: Bounded sliding window of envelopes awaiting delivery.
            On overflow the oldest envelope is dropped and a
            ``SUBSCRIPTION_LAG`` envelope is appended after the
            triggering envelope.
        dropped_count: Total number of envelopes evicted since the
            last lag notice was queued. Reset to 0 when the next lag
            notice is emitted (cumulative across the lifetime of the
            subscriber otherwise).
        last_dropped_id: ``Envelope.id`` of the most recently evicted
            envelope; carried into the next ``subscription_lag``
            payload.
        event: asyncio event set whenever a new envelope is queued so
            the receiver coroutine wakes up.
        loop: The event loop the subscriber's receiver runs on, captured at
            :meth:`EventBus.register`. :meth:`EventBus.publish` wakes the
            subscriber through it with ``call_soon_threadsafe`` so a publish
            from ANOTHER loop's thread -- e.g. the fleet dispatch's worker-
            thread loop streaming ``agent.output.chunk`` -- still wakes the
            main-loop receiver (a bare ``asyncio.Event.set()`` from a foreign
            thread does not schedule the wake on the owning loop). ``None`` when
            the subscriber registered outside a running loop (unit tests), which
            falls back to a direct set.
        closed: True once :meth:`EventBus.unregister` has run; the
            receiver coroutine breaks its loop on this flag.
    """

    connection_id: str
    scope_filter: str | None = None
    kind_filter: list[StoreKind] | None = None
    since_event_id: str | None = None
    queue: deque[Envelope] = field(default_factory=lambda: deque(maxlen=DEFAULT_QUEUE_SIZE))
    dropped_count: int = 0
    last_dropped_id: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    loop: asyncio.AbstractEventLoop | None = None
    closed: bool = False

    def wake(self) -> None:
        """Wake the subscriber's receiver, safe across event loops.

        The wake schedules :meth:`asyncio.Event.set` on the loop that owns the
        subscriber's ``event`` so a cross-thread producer (the fleet dispatch's
        worker-thread loop) still wakes the main-loop receiver. A subscriber
        with no captured loop (or whose loop has closed) falls back to a direct
        set -- correct for same-loop unit tests, best-effort at shutdown.
        """
        loop = self.loop
        if loop is None:
            self.event.set()
            return
        try:
            loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:
            # The owning loop is closed (daemon shutdown) -- best-effort set.
            self.event.set()

    def matches(self, envelope: Envelope) -> bool:
        """Return True when *envelope* satisfies the subscriber's filters.

        Args:
            envelope: Candidate envelope from the producer.

        Returns:
            True when the envelope should be pushed to this subscriber.
        """
        if self.scope_filter is not None and envelope.scope_id != self.scope_filter:
            return False
        return not (self.kind_filter is not None and envelope.kind not in self.kind_filter)


def _now() -> datetime:
    return datetime.now(UTC)


def _build_lag_envelope(dropped_count: int, last_event_id: str) -> Envelope:
    """Construct the inline ``subscription_lag`` envelope.

    Args:
        dropped_count: Number of envelopes evicted since the last lag
            notice was delivered.
        last_event_id: ``Envelope.id`` of the most-recently evicted
            envelope.

    Returns:
        A ``SUBSCRIPTION_LAG`` envelope ready to enqueue inline.
    """
    now = _now()
    return Envelope(
        id=f"LAG-{int(now.timestamp() * 1000)}-{last_event_id}",
        kind=StoreKind.SUBSCRIPTION_LAG,
        scope_id=None,
        created_at=now,
        summary=f"subscription_lag dropped={dropped_count}",
        payload={"dropped_count": dropped_count, "last_event_id": last_event_id},
    )


class EventBus:
    """Process-internal subscription bus shared by every connection.

    The bus owns no I/O of its own — it is a fan-out plus per-
    subscriber bounded queue. Producers call :meth:`publish` after
    persisting an envelope; receivers run
    :meth:`iter_subscriber_pushes` to drain a subscriber's queue.

    Catch-up reads are handled by :func:`catch_up` (a module-level
    helper that streams from ``event.jsonl``); the bus exposes
    :meth:`register` and :meth:`unregister` to manage subscriber
    lifecycle.
    """

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        """Initialise an empty bus.

        Args:
            queue_size: ``maxlen`` for each subscriber's deque.
                Production code should keep the default; tests
                shrink the queue to drive the drop-oldest path.
        """
        self._subscribers: dict[str, Subscriber] = {}
        self._queue_size = queue_size

    @property
    def active_subscriptions(self) -> int:
        """Return the number of live subscribers — backs ``daemon.status``."""
        return sum(1 for sub in self._subscribers.values() if not sub.closed)

    def register(
        self,
        *,
        connection_id: str,
        scope_filter: str | None = None,
        kind_filter: list[StoreKind] | None = None,
        since_event_id: str | None = None,
    ) -> Subscriber:
        """Register a new subscriber against *connection_id*.

        Args:
            connection_id: Unique identifier for the owning connection.
                Re-registering the same id raises.
            scope_filter: Optional ``scope_id`` filter.
            kind_filter: Optional kind whitelist.
            since_event_id: Optional starting point passed through to
                :func:`catch_up`.

        Returns:
            The created :class:`Subscriber`.

        Raises:
            ValueError: When *connection_id* is already registered.
        """
        if connection_id in self._subscribers:
            raise ValueError(f"connection already registered: {connection_id!r}")
        sub = Subscriber(
            connection_id=connection_id,
            scope_filter=scope_filter,
            kind_filter=kind_filter,
            since_event_id=since_event_id,
            queue=deque(maxlen=self._queue_size),
        )
        # Capture the loop the receiver will run on so a cross-thread publish
        # (the fleet dispatch's worker-thread loop) wakes it safely. Registering
        # outside a running loop (a unit test) leaves it None -> direct set.
        try:
            sub.loop = asyncio.get_running_loop()
        except RuntimeError:
            sub.loop = None
        self._subscribers[connection_id] = sub
        logger.debug(
            f"register connection={connection_id!r} scope={scope_filter!r} kinds={kind_filter}"
        )
        return sub

    def unregister(self, connection_id: str) -> None:
        """Remove the subscriber registered against *connection_id*.

        Idempotent — unregistering an unknown id is a no-op so
        connection teardown paths can call this unconditionally.

        Args:
            connection_id: Identifier supplied to :meth:`register`.
        """
        sub = self._subscribers.pop(connection_id, None)
        if sub is None:
            return
        sub.closed = True
        sub.queue.clear()
        sub.event.set()
        logger.debug(f"unregister connection={connection_id!r}")

    def publish(self, envelope: Envelope) -> None:
        """Push *envelope* to every matching subscriber.

        On per-subscriber overflow (queue length == ``maxlen``) the
        oldest envelope is popped left, the dropped counter on the
        subscriber is incremented, the *incoming* envelope is appended,
        and a ``SUBSCRIPTION_LAG`` envelope is appended right after it.
        The producer never blocks on any subscriber.

        Args:
            envelope: The envelope to publish.
        """
        for sub in self._subscribers.values():
            if sub.closed or not sub.matches(envelope):
                continue
            queue = sub.queue
            maxlen = queue.maxlen
            if maxlen is not None and len(queue) >= maxlen:
                dropped = queue.popleft()
                sub.dropped_count += 1
                sub.last_dropped_id = dropped.id
                queue.append(envelope)
                lag = _build_lag_envelope(sub.dropped_count, dropped.id)
                # The lag envelope itself can evict another oldest
                # entry when the queue is still at capacity; that is
                # the spec'd sliding-window behaviour.
                if len(queue) >= maxlen:
                    second_dropped = queue.popleft()
                    sub.dropped_count += 1
                    sub.last_dropped_id = second_dropped.id
                queue.append(lag)
                logger.info(
                    f"publish drop-oldest connection={sub.connection_id!r} "
                    f"dropped_id={dropped.id!r} dropped_count={sub.dropped_count}"
                )
            else:
                queue.append(envelope)
            sub.wake()

    async def iter_subscriber_pushes(self, subscriber: Subscriber) -> AsyncIterator[Envelope]:
        """Yield queued envelopes for *subscriber* until it is closed.

        Awaits the subscriber's :attr:`Subscriber.event` flag, drains
        every envelope currently queued, clears the flag, and loops.
        Exits when the subscriber is unregistered.

        Args:
            subscriber: Subscriber returned by :meth:`register`.

        Yields:
            One :class:`Envelope` per push, in FIFO order.
        """
        while not subscriber.closed:
            await subscriber.event.wait()
            while subscriber.queue:
                yield subscriber.queue.popleft()
            if subscriber.closed:
                return
            subscriber.event.clear()


def _iter_envelopes(path: Path) -> Iterable[Envelope]:
    """Yield envelopes from a JSONL file.

    Args:
        path: Path to a ``*.jsonl`` store file.

    Yields:
        One :class:`Envelope` per parsed line.
    """
    if not path.exists():
        return
    with path.open("rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            yield Envelope.model_validate(orjson.loads(line))


def catch_up(
    subscriber: Subscriber,
    event_path: Path,
    *,
    max_events: int = CATCH_UP_MAX,
) -> list[Envelope]:
    """Replay envelopes from *event_path* that the subscriber missed.

    Walks ``event.jsonl`` from the top; skips every envelope up to and
    including the one whose ``id`` equals ``subscriber.since_event_id``
    (when set); yields the rest, filtered by the subscriber's
    ``scope_filter`` and ``kind_filter``.

    Args:
        subscriber: Live subscriber returned by :meth:`EventBus.register`.
        event_path: Path to ``event.jsonl`` (typically resolved via
            :func:`eawf.kernel.store.paths.store_path`).
        max_events: Hard bound on the number of envelopes the caller
            is willing to replay. Default is :data:`CATCH_UP_MAX`.

    Returns:
        List of envelopes the subscriber missed, in append order.

    Raises:
        CatchUpTooLargeError: When more than *max_events* envelopes
            match — the caller maps this to ``-32008 catch_up_too_large``.
    """
    since = subscriber.since_event_id
    seen_since = since is None
    collected: list[Envelope] = []
    for env in _iter_envelopes(event_path):
        if not seen_since:
            if env.id == since:
                seen_since = True
            continue
        if not subscriber.matches(env):
            continue
        if len(collected) >= max_events:
            raise CatchUpTooLargeError(
                f"catch_up exceeded max_events={max_events} connection={subscriber.connection_id!r}"
            )
        collected.append(env)
    return collected
