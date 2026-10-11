"""The batch-close sweep: a Batch whose work lands after its Tasks are ready still closes.

A Batch closes when the Run readying its last Task completes, but its work may reach the
phase branch only later, when a lease commit is cherry-picked by hand. So the daemon
sweeps every native tree it serves on a fixed cadence and closes each ready Batch whose
work has since landed, exactly as ``batch close-landed`` would.

The sweep reads the trees the daemon has attached, which is every tree a native request
has reached since it started.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Final

from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.epoch2_recovery import publish_projection
from eawf.runtime.daemon.methods import MethodContext

logger = logging.getLogger(__name__)

#: The principal a close the daemon takes on its own cadence is attributed to.
SWEEP_ACTOR: Final = "DAEMON"

#: How often the sweep runs. A cherry-pick is a human step, so closing within half a
#: minute of it is prompt, and a sweep that finds nothing ready reads no git at all.
SWEEP_SECONDS: Final = 30.0


def sweep_once(ctx: MethodContext, *, now: datetime) -> tuple[Envelope, ...]:
    """Close every ready, landed Batch in every tree the daemon has attached, once.

    A tree that cannot be swept -- it left epoch 2, or its select is not whole -- is
    logged and passed over, so one broken tree never keeps another's Batches open.

    Args:
        ctx: The daemon context whose attached trees are swept.
        now: The stamp every edge shares.

    Returns:
        The envelopes the committed edges produced, each already published.
    """
    roots = tuple(ctx.native_roots.items())
    if not roots:
        return ()
    # imported on the first tree to sweep: the close pulls in the delivery verbs, which
    # would otherwise delay a cold daemon's first answer
    from eawf.runtime.daemon.batch_close import close_landed_batches

    envelopes: list[Envelope] = []
    for root_id, context in roots:
        try:
            _, moved = close_landed_batches(context, (), actor=SWEEP_ACTOR, now=now)
        except Exception as exc:
            logger.warning(f"batch close sweep skipped root={root_id} error={exc!r}")
            continue
        envelopes.extend(moved)
    for envelope in envelopes:
        publish_projection(ctx.bus, envelope)
    return tuple(envelopes)


async def run_batch_close_loop(ctx: MethodContext, *, stop_event: asyncio.Event) -> None:
    """Sweep for landed Batches every :data:`SWEEP_SECONDS` until stopped.

    Args:
        ctx: The daemon context whose attached trees are swept.
        stop_event: Set when the daemon shuts down.
    """
    while not stop_event.is_set():
        await asyncio.to_thread(sweep_once, ctx, now=datetime.now(UTC))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_SECONDS)


__all__ = ["SWEEP_ACTOR", "SWEEP_SECONDS", "run_batch_close_loop", "sweep_once"]
