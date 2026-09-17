"""``state.subscribe`` — vocabulary alias for :func:`event.subscribe`.

``state.subscribe`` stays in the public surface for callers that think
of the live stream as "state changed" rather than "event emitted"; the
two names share one implementation (see
:func:`eawf.runtime.daemon.methods.event.subscribe`).

``projection.subscribe`` is the third name on the same hook. It registers
the same kind of subscriber on the same bus over the same connection, and
differs only in what the streamer writes: keyed projection patches rather
than raw envelopes. Keeping it here rather than giving it a transport of
its own is the point — a console that already holds a push stream and a
poll backstop gains a projection feed without opening a second socket.

The streamer itself runs out of the connection handler in
:mod:`eawf.runtime.daemon.server`; this module exists to mark the method names
as registered so :func:`registered_methods` lists them and the dispatcher
recognises them as known verbs. The actual handler bodies are the no-op
sentinels below — the server detects a subscribe verb *before*
dispatch and routes to the streaming path.
"""

from __future__ import annotations

from typing import Any, Final

from eawf.runtime.daemon.methods import MethodContext, register

#: The RPC name a console subscribes to keyed projection patches by.
PROJECTION_SUBSCRIBE_METHOD: Final = "projection.subscribe"

#: The notification a projection subscriber receives one of per keyed patch,
#: beside the ``event.push`` the other two verbs push raw envelopes as.
PROJECTION_PUSH_METHOD: Final = "projection.patch"

#: The RPC names that route to the subscribe streamer.
SUBSCRIBE_METHODS: frozenset[str] = frozenset(
    {"event.subscribe", "state.subscribe", PROJECTION_SUBSCRIBE_METHOD}
)


@register("state.subscribe")
async def state_subscribe(_ctx: MethodContext, _params: dict[str, Any]) -> dict[str, Any]:
    """Sentinel handler — the streamer is owned by the server.

    The server connection handler intercepts ``state.subscribe`` and
    ``event.subscribe`` before dispatch hits this function; reaching
    this body indicates the streaming hook is mis-wired.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError(
        "state.subscribe must be dispatched via the streaming hook in eawf.runtime.daemon.server"
    )


@register("event.subscribe")
async def event_subscribe(_ctx: MethodContext, _params: dict[str, Any]) -> dict[str, Any]:
    """Sentinel handler — the streamer is owned by the server.

    Mirrors :func:`state_subscribe`; the alias exists so the public
    method registry lists both verbs.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError(
        "event.subscribe must be dispatched via the streaming hook in eawf.runtime.daemon.server"
    )


@register(PROJECTION_SUBSCRIBE_METHOD)
async def projection_subscribe(_ctx: MethodContext, _params: dict[str, Any]) -> dict[str, Any]:
    """Sentinel handler — the streamer is owned by the server.

    Mirrors :func:`state_subscribe`; reaching this body means the
    projection feed was dispatched as a request/response call rather
    than routed onto the one streaming hook it shares with the other
    two subscribe verbs.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError(
        f"{PROJECTION_SUBSCRIBE_METHOD} must be dispatched via the streaming hook in "
        "eawf.runtime.daemon.server"
    )
