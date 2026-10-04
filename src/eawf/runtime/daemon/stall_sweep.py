"""The stall sweep: every Run that went quiet past its interval gets a stall fact.

A Run stops producing whether or not anybody is watching it, and a stall that is only
computed when someone reads the Run's stream is a stall nobody hears about. So the daemon
sweeps every native tree it serves on a fixed cadence and raises the typed stall fact for
each running Run silent past its interval. The sweep only observes: it never moves a Run,
so a stalled Run stays running until a principal's control ends it.

The sweep reads the trees the daemon has attached, which is every tree a native request
has reached since it started; a Run can only have been dispatched through such a request.

The same pass opens the one budget notice a running Run earns by passing its estimate
with no progress past the notice policy's grace period.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Final

from eawf.runtime.daemon.methods import MethodContext

logger = logging.getLogger(__name__)

#: How often the sweep runs. The shortest useful stall interval is minutes, so a stall is
#: raised within a small fraction of the interval it crossed.
SWEEP_SECONDS: Final = 15.0


def sweep_once(ctx: MethodContext, *, now: datetime) -> tuple[str, ...]:
    """Raise every due stall, and open every due estimate notice, in every attached tree, once.

    A tree that cannot be swept -- it left epoch 2, or its select is not whole -- is
    logged and passed over, so one broken tree never stops the others' stalls.

    Args:
        ctx: The daemon context whose attached trees are swept.
        now: The recording clock, which decides what has gone quiet.

    Returns:
        The keys of every Run this sweep raised a stall for.
    """
    roots = tuple(ctx.native_roots.items())
    if not roots:
        return ()
    # imported on the first tree to sweep: the liveness reads pull in the Run and dispatch
    # stack, which would otherwise delay a cold daemon's first answer
    from eawf.runtime.daemon.methods import run_liveness

    raised: list[str] = []
    for root_id, context in roots:
        try:
            raised.extend(run_liveness.detect_stalls(context, now=now))
        except Exception as exc:
            logger.warning(f"stall sweep skipped root={root_id} error={exc!r}")
        # an estimate notice is its own observation, so a failed stall pass never withholds it
        try:
            run_liveness.detect_estimate_crossings(context, now=now)
        except Exception as exc:
            logger.warning(f"estimate sweep skipped root={root_id} error={exc!r}")
    return tuple(raised)


async def run_stall_loop(ctx: MethodContext, *, stop_event: asyncio.Event) -> None:
    """Sweep for stalled Runs every :data:`SWEEP_SECONDS` until stopped.

    Args:
        ctx: The daemon context whose attached trees are swept.
        stop_event: Set when the daemon shuts down.
    """
    while not stop_event.is_set():
        await asyncio.to_thread(sweep_once, ctx, now=datetime.now(UTC))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_SECONDS)


__all__ = ["SWEEP_SECONDS", "run_stall_loop", "sweep_once"]
