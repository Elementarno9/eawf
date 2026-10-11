"""``runtime.delivery.close_landed``: close every Batch whose work has landed.

A Batch closes on its own when the Run that readies its last Task completes. This verb
is the operator's way to ask for the same close: after a cherry-pick put a ready Task's
commit on the phase branch, or for a Batch that waited on another one holding its
repository's active slot. The close itself is
:func:`~eawf.runtime.daemon.batch_close.close_landed_batches`; the verb answers one line
per Batch it tried, closed or not and why.

The verb never waits on a proof. A Batch whose criteria are proved by receipts already
filed closes in the request; one with a criterion still to run answers ``proving`` and
the daemon's sweep closes it once the background proof settles, because a proof may run
for minutes and a request is answered in seconds. A criterion whose receipt at the head
already failed is answered as unmet, naming it, rather than proved again; asked with
``rerun_failed``, it is run again, so a flaky failure can be retried.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import BatchKey, PrincipalKey
from eawf.runtime.daemon.batch_close import BatchClosure, close_landed_batches
from eawf.runtime.daemon.epoch2_recovery import publish_projection
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.native_guard import native_mutator, native_params

#: The verb that closes each ready Batch whose work is on its phase branch.
DELIVERY_CLOSE_LANDED_METHOD: Final = "runtime.delivery.close_landed"


class CloseLandedParams(BaseModel):
    """Params of :data:`DELIVERY_CLOSE_LANDED_METHOD`.

    Attributes:
        actor: Who asked; every edge of every close is attributed to them.
        batch_keys: The Batches to close. Empty, every open Batch of every ACTIVE
            Milestone.
        rerun_failed: Prove again a criterion whose receipt at the head failed;
            otherwise the failed receipt is answered as the reason the Batch is open.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    batch_keys: tuple[BatchKey, ...] = ()
    rerun_failed: bool = False


class CloseLandedAnswer(BaseModel):
    """What one close-landed request answers with.

    Attributes:
        closures: One line per Batch attempted, in attempt order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    closures: tuple[BatchClosure, ...]


@native_mutator(DELIVERY_CLOSE_LANDED_METHOD)
async def _close_landed(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Close each named, or each open, Batch whose Tasks are ready and landed."""
    args = native_params(CloseLandedParams, params)
    context = ctx.native_root_context(authority.root)
    closures, envelopes = await asyncio.to_thread(
        close_landed_batches,
        context,
        args.batch_keys,
        actor=args.actor,
        now=datetime.now(UTC),
        rerun_failed=args.rerun_failed,
    )
    for envelope in envelopes:
        publish_projection(ctx.bus, envelope)
    return CloseLandedAnswer(closures=closures).model_dump(mode="json")


__all__ = ["DELIVERY_CLOSE_LANDED_METHOD", "CloseLandedAnswer", "CloseLandedParams"]
