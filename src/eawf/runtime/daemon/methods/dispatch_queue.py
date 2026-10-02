"""The dispatch queue read, and the operator's pause, drain and resume requests.

``runtime.dispatch.queue.read`` answers with the queue the ``unattended`` route renders:
its Runs with their elapsed-time inputs and sealed budgets, the running verification legs
with their progress, the concurrency plan derived at read time, and where the control
stands. ``runtime.dispatch.control.request`` records one pause, drain or resume under the
id and verb it was sent with, so a request sent again answers with what the first one did.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.runtime.dispatch_queue import DispatchControlFact, DispatchVerb
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.admission import EconomicsPolicyError, load_economics
from eawf.runtime.daemon.dispatch_queue import dispatch_queue_view, request_dispatch_control
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.memory import project_subject
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_guard import native_mutator, native_params, require_native_call
from eawf.runtime.verification.progress import list_progress_manifests

logger = logging.getLogger(__name__)

#: The read that states the dispatch queue.
DISPATCH_QUEUE_READ_METHOD: Final = "runtime.dispatch.queue.read"

#: The request that pauses, drains or resumes dispatch.
DISPATCH_CONTROL_REQUEST_METHOD: Final = "runtime.dispatch.control.request"


class DispatchQueueRead(BaseModel):
    """A read of the addressed tree's dispatch queue; it takes no parameter."""

    model_config = ConfigDict(extra="forbid")


class DispatchControlParams(BaseModel):
    """Params of :data:`DISPATCH_CONTROL_REQUEST_METHOD`.

    Attributes:
        verb: What is asked of the scheduler.
        actor: The principal asking.
        request_ref: The id the request is sent under.
    """

    model_config = ConfigDict(extra="forbid")

    verb: DispatchVerb
    actor: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    request_ref: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,120}$")]


class DispatchControlAnswer(BaseModel):
    """What became of one control request.

    Attributes:
        fact: The fact as recorded.
        disposition: The control outcome the console reads: ``confirmed`` when the hold
            is recorded, ``rejected`` when it was refused.
        reason: Why it was refused; ``None`` otherwise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact: DispatchControlFact
    disposition: str
    reason: str | None = None


def _read(authority: RootAuthority, *, now: datetime) -> dict[str, Any]:
    """Read the queue of one fence-cleared tree."""
    path = document_path(authority)
    try:
        governor = load_economics(authority.root.parent).governor
    except EconomicsPolicyError as error:
        logger.warning(f"dispatch_queue economics unreadable cause={error!s}")
        governor = None
    view = dispatch_queue_view(
        read_document(path),
        read_ledger_records(ledger_path(path, Epoch2Collection.RUN)),
        governor=governor,
        manifests=list_progress_manifests(authority.root / "state.json"),
        now=now,
    )
    return view.model_dump(mode="json")


@register(DISPATCH_QUEUE_READ_METHOD)
async def _read_queue(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Answer with the addressed tree's dispatch queue."""
    authority = require_native_call(ctx, params)
    native_params(DispatchQueueRead, params)
    return await asyncio.to_thread(_read, authority, now=datetime.now(UTC))


@native_mutator(DISPATCH_CONTROL_REQUEST_METHOD)
async def _request_control(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one pause, drain or resume request; no Run is stopped or cancelled."""
    args = native_params(DispatchControlParams, params)
    context = ctx.native_root_context(authority.root)

    def record() -> DispatchControlFact:
        with context.session([project_subject(context)]) as session:
            ledger = session.ledger_path(Epoch2Collection.RUN)
            return request_dispatch_control(
                session,
                read_ledger_records(ledger),
                read_document(document_path(authority)),
                verb=args.verb,
                actor=args.actor,
                request_ref=args.request_ref,
                now=datetime.now(UTC),
            )

    fact = await asyncio.to_thread(record)
    answer = DispatchControlAnswer(fact=fact, disposition=fact.outcome.value, reason=fact.reason)
    return answer.model_dump(mode="json")


__all__ = [
    "DISPATCH_CONTROL_REQUEST_METHOD",
    "DISPATCH_QUEUE_READ_METHOD",
    "DispatchControlAnswer",
    "DispatchControlParams",
    "DispatchQueueRead",
]
