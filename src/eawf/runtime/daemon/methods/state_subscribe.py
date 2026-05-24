"""``state.subscribe`` — vocabulary alias for :func:`event.subscribe`.

``state.subscribe`` stays in the public surface for callers that think
of the live stream as "state changed" rather than "event emitted"; the
two names share one implementation (see
:func:`eawf.runtime.daemon.methods.event.subscribe`).

The streamer itself runs out of the connection handler in
:mod:`eawf.runtime.daemon.server`; this module exists to mark the method name
as registered so :func:`registered_methods` lists it and the dispatcher
recognises it as a known verb. The actual handler body is the no-op
sentinel below — the server detects the subscribe pair *before*
dispatch and routes to the streaming path.
"""

from __future__ import annotations

from typing import Any

from eawf.runtime.daemon.methods import MethodContext, register

#: The two RPC names that route to the subscribe streamer.
SUBSCRIBE_METHODS: frozenset[str] = frozenset({"event.subscribe", "state.subscribe"})


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
