"""``event.*`` JSON-RPC methods: list / show + the subscribe streamer.

``event.subscribe`` is the live push-notification stream — owned by the
connection-handler in :mod:`eawf.daemon.server` because it produces
multiple frames per call. The streamer below is the building block the
handler invokes once the JSON-RPC framing has dispatched the
``subscribe`` request.

``event.list`` and ``event.show`` are bounded one-shot reads from
``event.jsonl`` and follow the regular request/response pattern.

Per C02 §5.7 (revised 2026-05-18 per audit C02.F50), backpressure is
**drop-oldest sliding window** — the bus owns that policy, not these
handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.daemon.bus import (
    CATCH_UP_MAX,
    CatchUpTooLargeError,
    EventBus,
    Subscriber,
    catch_up,
)
from eawf.daemon.methods import MethodContext, register
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


#: Default page size when ``limit`` is omitted from ``event.list``.
DEFAULT_LIST_LIMIT = 100

#: Hard upper bound for ``event.list`` paging.
MAX_LIST_LIMIT = 10000

#: JSON-RPC error code raised when catch-up exceeds the bound.
CATCH_UP_TOO_LARGE = -32008


class SubscribeParams(BaseModel):
    """Params for :func:`event.subscribe` / :func:`state.subscribe`.

    Attributes:
        scope_id: Optional scope filter. Only envelopes whose
            ``Envelope.scope_id`` matches will be pushed.
        kinds: Optional kind whitelist.
        since_event_id: Optional starting point for catch-up. When set,
            the subscriber receives every envelope after this id from
            ``event.jsonl`` before live push begins.
    """

    model_config = ConfigDict(extra="forbid")
    scope_id: str | None = None
    kinds: list[StoreKind] | None = None
    since_event_id: str | None = None


class ListParams(BaseModel):
    """Params for :func:`event.list`."""

    model_config = ConfigDict(extra="forbid")
    scope_id: str | None = None
    since: str | None = None
    until: str | None = None
    kinds: list[StoreKind] | None = None
    limit: int = Field(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT)


class ListResult(BaseModel):
    """Result of :func:`event.list`."""

    model_config = ConfigDict(extra="forbid")
    events: list[dict[str, Any]]


class ShowParams(BaseModel):
    """Params for :func:`event.show`."""

    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1)


class ShowResult(BaseModel):
    """Result of :func:`event.show`."""

    model_config = ConfigDict(extra="forbid")
    event: dict[str, Any]


def _matches(
    env: Envelope,
    *,
    scope_id: str | None,
    kinds: list[StoreKind] | None,
) -> bool:
    """Return True when *env* matches the supplied filters.

    Args:
        env: Candidate envelope.
        scope_id: Optional ``scope_id`` filter.
        kinds: Optional kind whitelist.

    Returns:
        True when the envelope passes every active filter.
    """
    if scope_id is not None and env.scope_id != scope_id:
        return False
    return not (kinds is not None and env.kind not in kinds)


def _iter_envelopes(path: Path) -> list[Envelope]:
    """Load every envelope in *path* (bounded one-shot read).

    Args:
        path: Path to ``event.jsonl``.

    Returns:
        List of envelopes in append order. Empty when *path* does not
        exist.
    """
    import orjson

    if not path.exists():
        return []
    out: list[Envelope] = []
    with path.open("rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            out.append(Envelope.model_validate(orjson.loads(line)))
    return out


@register("event.list")
async def list_events(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Bounded read from ``event.jsonl``.

    Walks the JSONL file once, applies the supplied filters (``scope_id``,
    ``kinds``, ``since``, ``until`` — both ids), and returns up to
    ``limit`` envelopes (default :data:`DEFAULT_LIST_LIMIT`, cap
    :data:`MAX_LIST_LIMIT`).

    Args:
        ctx: Server context — must carry ``event_path`` (resolved by
            :func:`eawf.store.paths.store_path`).
        params: JSON-RPC params per :class:`ListParams`.

    Returns:
        Dict matching :class:`ListResult` (envelopes as JSON-mode dicts).

    Raises:
        RuntimeError: When ``ctx.event_path`` is not configured.
    """
    args = ListParams.model_validate(params)
    if ctx.event_path is None:
        raise RuntimeError("event_path not configured on daemon context")
    envs = _iter_envelopes(Path(ctx.event_path))
    since_seen = args.since is None
    collected: list[Envelope] = []
    for env in envs:
        if not since_seen:
            if env.id == args.since:
                since_seen = True
            continue
        if not _matches(env, scope_id=args.scope_id, kinds=args.kinds):
            continue
        collected.append(env)
        if env.id == args.until:
            break
        if len(collected) >= args.limit:
            break
    result = ListResult(events=[e.model_dump(mode="json") for e in collected])
    return result.model_dump(mode="json")


@register("event.show")
async def show_event(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one envelope from ``event.jsonl`` by id.

    Args:
        ctx: Server context — must carry ``event_path``.
        params: JSON-RPC params per :class:`ShowParams`.

    Returns:
        Dict matching :class:`ShowResult`.

    Raises:
        ValueError: When ``event_id`` is not present in ``event.jsonl``.
            The server maps this to ``-32602 invalid params``.
        RuntimeError: When ``ctx.event_path`` is not configured.
    """
    args = ShowParams.model_validate(params)
    if ctx.event_path is None:
        raise RuntimeError("event_path not configured on daemon context")
    for env in _iter_envelopes(Path(ctx.event_path)):
        if env.id == args.event_id:
            return ShowResult(event=env.model_dump(mode="json")).model_dump(mode="json")
    raise ValueError(f"unknown event_id: {args.event_id!r}")


def subscribe(
    bus: EventBus,
    *,
    connection_id: str,
    params: dict[str, Any],
    event_path: Path | None,
) -> tuple[Subscriber, list[Envelope]]:
    """Register a subscriber and run the catch-up phase.

    The connection handler in :mod:`eawf.daemon.server` calls this on
    first dispatch of ``event.subscribe`` (or its ``state.subscribe``
    alias). The returned subscriber is then drained by the per-
    connection task via :meth:`EventBus.iter_subscriber_pushes` while
    the catch-up envelopes are flushed first.

    Args:
        bus: Daemon-wide event bus.
        connection_id: Stable identifier for the owning connection.
        params: Raw params from the JSON-RPC frame; validated against
            :class:`SubscribeParams`.
        event_path: ``event.jsonl`` path for catch-up. Pass ``None``
            when running without an on-disk store (unit tests).

    Returns:
        Tuple of the registered :class:`Subscriber` and the list of
        envelopes the subscriber missed since ``since_event_id``.

    Raises:
        CatchUpTooLargeError: When the catch-up window exceeds
            :data:`~eawf.daemon.bus.CATCH_UP_MAX`. The handler maps
            this to ``-32008 catch_up_too_large``.
    """
    args = SubscribeParams.model_validate(params)
    sub = bus.register(
        connection_id=connection_id,
        scope_filter=args.scope_id,
        kind_filter=args.kinds,
        since_event_id=args.since_event_id,
    )
    backlog: list[Envelope] = []
    if args.since_event_id and event_path is not None:
        try:
            backlog = catch_up(sub, event_path, max_events=CATCH_UP_MAX)
        except CatchUpTooLargeError:
            bus.unregister(connection_id)
            raise
    logger.info(
        f"subscribe connection={connection_id!r} "
        f"scope={args.scope_id!r} kinds={args.kinds} "
        f"since={args.since_event_id!r} backlog={len(backlog)}"
    )
    return sub, backlog
