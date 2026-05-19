"""``daemon.*`` JSON-RPC methods: ping / status / shutdown.

Implements the minimum control surface per C02 §5.3.5. ``daemon.ping``
returns liveness + version; ``daemon.status`` returns operational
counters; ``daemon.shutdown`` signals the server to stop.

Each handler validates its params via a Pydantic v2 model with
``ConfigDict(extra="forbid")`` so unknown fields raise before any
business logic runs (rule 2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.daemon.methods import MethodContext, register

logger = logging.getLogger(__name__)


_BOOT_MONO = time.monotonic()


def _uptime_seconds() -> float:
    return max(0.0, time.monotonic() - _BOOT_MONO)


class PingParams(BaseModel):
    """Params for :func:`ping` — empty by contract."""

    model_config = ConfigDict(extra="forbid")


class PingResult(BaseModel):
    """Result of :func:`ping`."""

    model_config = ConfigDict(extra="forbid")
    pid: int
    version: str
    protocol_version: str
    started_at: str
    uptime_seconds: float


class StatusParams(BaseModel):
    """Params for :func:`status` — empty by contract."""

    model_config = ConfigDict(extra="forbid")


class StatusResult(BaseModel):
    """Result of :func:`status`."""

    model_config = ConfigDict(extra="forbid")
    pid: int
    version: str
    protocol_version: str
    started_at: str
    uptime_seconds: float
    active_subscriptions: int
    in_flight_mutations: int
    last_event_id: str


class ShutdownParams(BaseModel):
    """Params for :func:`shutdown`."""

    model_config = ConfigDict(extra="forbid")
    drain: bool = True
    timeout_seconds: int = Field(default=30, ge=0, le=600)


class ShutdownResult(BaseModel):
    """Result of :func:`shutdown`."""

    model_config = ConfigDict(extra="forbid")
    shutdown_at: str
    drained: bool


@register("daemon.ping")
async def ping(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return daemon liveness + version.

    Args:
        ctx: Server context with pid / version / start time.
        params: JSON-RPC params; must be empty.

    Returns:
        Dict matching :class:`PingResult`.
    """
    PingParams.model_validate(params)
    result = PingResult(
        pid=ctx.pid,
        version=ctx.version,
        protocol_version=ctx.protocol_version,
        started_at=ctx.started_at,
        uptime_seconds=_uptime_seconds(),
    )
    return result.model_dump(mode="json")


@register("daemon.status")
async def status(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return operational counters for ``eawf daemon status``.

    Args:
        ctx: Server context — counters are updated by subsystems
            (subscription bus, mutator) as they land in later waves.
        params: JSON-RPC params; must be empty.

    Returns:
        Dict matching :class:`StatusResult`.
    """
    StatusParams.model_validate(params)
    # W06 wires the live subscriber counter via ``ctx.bus``; older
    # contexts (legacy tests, daemonless paths) fall through to the
    # static field which stays at its default ``0``.
    if ctx.bus is not None and hasattr(ctx.bus, "active_subscriptions"):
        active_subs = int(ctx.bus.active_subscriptions)
    else:
        active_subs = ctx.active_subscriptions
    result = StatusResult(
        pid=ctx.pid,
        version=ctx.version,
        protocol_version=ctx.protocol_version,
        started_at=ctx.started_at,
        uptime_seconds=_uptime_seconds(),
        active_subscriptions=active_subs,
        in_flight_mutations=ctx.in_flight_mutations,
        last_event_id=ctx.last_event_id,
    )
    return result.model_dump(mode="json")


@register("daemon.shutdown")
async def shutdown(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Signal the server to stop accepting new connections + exit.

    Args:
        ctx: Server context — the ``shutdown_event`` asyncio.Event is
            set so the main loop unblocks.
        params: JSON-RPC params per :class:`ShutdownParams`.

    Returns:
        Dict matching :class:`ShutdownResult`.

    Raises:
        RuntimeError: When the context lacks a ``shutdown_event`` —
            indicates the daemon was started without the standard
            asyncio scaffolding.
    """
    args = ShutdownParams.model_validate(params)
    if not isinstance(ctx.shutdown_event, asyncio.Event):
        raise RuntimeError("shutdown event not wired on daemon context")
    drain_window = args.timeout_seconds if args.drain else 0
    logger.info(f"shutdown drain={args.drain} timeout={drain_window}")
    ctx.shutdown_event.set()
    result = ShutdownResult(
        shutdown_at=datetime.now(UTC).isoformat(),
        drained=args.drain,
    )
    return result.model_dump(mode="json")
