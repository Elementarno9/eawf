"""``needs_user.*`` JSON-RPC methods.

The daemon is the canonical mutator for durable pause/resume rows. These
handlers wrap the shared pause-store helpers so CLI/TUI surfaces can raise,
resolve, and list open needs_user pauses without writing ``event.jsonl``
directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.workflow.skills.bodies.user_question import UserQuestion
from eawf.workflow.skills.needs_user import (
    OpenPause,
    list_open_pauses,
    record_pause,
    resolve_pause,
)

#: Field names that only the daemon writer may populate on a pause row: the
#: daemon mints ``pause_urn`` at record time, dates the row from the append
#: itself, and (for a wave-advisory pause) stamps the subject wave. A caller
#: that supplies one of these through ``needs_user.raise`` is not asking the
#: daemon to observe a fresh pause -- it is handing in provenance the daemon
#: never itself observed.
_DAEMON_OWNED_PAUSE_FIELDS = frozenset({"pause_urn", "occurred_at", "wave_id"})


class PauseFabricationError(DaemonValidationError):
    """Raised when ``needs_user.raise`` params carry daemon-owned pause fields.

    ``OpenPause`` is daemon-observed: its urn, occurrence time, and wave
    binding are minted by the daemon's own write path, never handed in by
    the raiser. Refusing here keeps a caller from planting a pre-fabricated
    pause fact instead of asking the daemon to record a fresh one.
    """


class RaiseParams(BaseModel):
    """Params for ``needs_user.raise``."""

    model_config = ConfigDict(extra="forbid")
    scope_id: str = Field(min_length=1)
    session: str = Field(min_length=1)
    question: UserQuestion


class RaiseResult(BaseModel):
    """Result of ``needs_user.raise``."""

    model_config = ConfigDict(extra="forbid")
    pause_urn: str


class ResolveParams(BaseModel):
    """Params for ``needs_user.resolve``."""

    model_config = ConfigDict(extra="forbid")
    pause_urn: str = Field(min_length=1)
    choice: str = Field(min_length=1)


class ResolveResult(BaseModel):
    """Result of ``needs_user.resolve``."""

    model_config = ConfigDict(extra="forbid")
    pause_urn: str
    choice: str
    scope_id: str


class ParkParams(BaseModel):
    """Params for ``needs_user.park``."""

    model_config = ConfigDict(extra="forbid")
    scope_id: str | None = None


class ParkedPause(BaseModel):
    """One open pause returned by ``needs_user.park``."""

    model_config = ConfigDict(extra="forbid")
    pause_urn: str
    scope_id: str
    session: str
    question: UserQuestion


class ParkResult(BaseModel):
    """Result of ``needs_user.park``."""

    model_config = ConfigDict(extra="forbid")
    pauses: list[ParkedPause]


def _state_path(ctx: MethodContext) -> Path:
    """Return daemon state path or raise a method error."""
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    return Path(ctx.state_path)


def _publish(ctx: MethodContext, envelope: Envelope) -> None:
    """Publish *envelope* to live subscribers and update last event id."""
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def _parked_pause(pause: OpenPause) -> ParkedPause:
    """Convert an open pause into wire result shape."""
    return ParkedPause(
        pause_urn=pause.pause_urn,
        scope_id=pause.scope_id,
        session=pause.session,
        question=pause.question,
    )


@register("needs_user.raise")
async def raise_needs_user(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Persist a needs_user pause via the daemon writer."""
    fabricated = sorted(_DAEMON_OWNED_PAUSE_FIELDS & params.keys())
    if fabricated:
        raise PauseFabricationError(
            f"needs_user.raise refuses caller-supplied daemon-owned fields: {fabricated}"
        )
    args = RaiseParams.model_validate(params)
    pause_urn = record_pause(
        _state_path(ctx),
        scope_id=args.scope_id,
        session=args.session,
        question=args.question,
        publish=lambda envelope: _publish(ctx, envelope),
    )
    return RaiseResult(pause_urn=pause_urn).model_dump(mode="json")


@register("needs_user.resolve")
async def resolve_needs_user(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a needs_user pause via the daemon writer."""
    args = ResolveParams.model_validate(params)
    pause = resolve_pause(
        _state_path(ctx),
        pause_urn=args.pause_urn,
        choice=args.choice,
        publish=lambda envelope: _publish(ctx, envelope),
    )
    return ResolveResult(
        pause_urn=pause.pause_urn,
        choice=args.choice,
        scope_id=pause.scope_id,
    ).model_dump(mode="json")


@register("needs_user.park")
async def park_needs_user(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return currently open needs_user pauses."""
    args = ParkParams.model_validate(params)
    pauses = [
        _parked_pause(pause) for pause in list_open_pauses(_state_path(ctx), scope_id=args.scope_id)
    ]
    return ParkResult(pauses=pauses).model_dump(mode="json")


__all__ = [
    "ParkParams",
    "ParkResult",
    "ParkedPause",
    "PauseFabricationError",
    "RaiseParams",
    "RaiseResult",
    "ResolveParams",
    "ResolveResult",
    "park_needs_user",
    "raise_needs_user",
    "resolve_needs_user",
]
