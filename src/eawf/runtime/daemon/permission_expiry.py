"""The expiry sweep: every lapsed provider permission ends in a record.

A provider permission's deadline passes whether or not anybody looks at it,
and the provider denies the held call when it does. The decide verb records
a lapse it arrives after, but a permission nobody answered would otherwise
lapse in silence, which is the failure the record exists to make visible. So
the daemon sweeps every native tree it serves on a short interval and records
each lapse as a provider-decided expiry.

The sweep reads the trees the daemon has attached, which is every tree a
native request has reached since it started; a permission can only have been
recorded through such a request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Final

from eawf.runtime.daemon.methods import MethodContext

logger = logging.getLogger(__name__)

#: How often the sweep runs. A host's decision window is about a minute, so a
#: lapse is recorded within a small fraction of the window it closed.
SWEEP_SECONDS: Final = 5.0


def sweep_once(ctx: MethodContext, *, now: datetime) -> tuple[str, ...]:
    """Record every lapse in every attached tree, once.

    A tree that cannot be swept -- it left epoch 2, or its select is not
    whole -- is logged and passed over, so one broken tree never stops the
    others' lapses from being recorded.

    Args:
        ctx: The daemon context whose attached trees are swept.
        now: The recording clock, which decides what has lapsed.

    Returns:
        The keys of every permission this sweep expired.
    """
    roots = tuple(ctx.native_roots.items())
    if not roots:
        return ()
    # imported on the first tree to sweep: the permission verbs pull in the Run and
    # dispatch stack, which would otherwise delay a cold daemon's first answer
    from eawf.runtime.daemon.methods import permission

    expired: list[str] = []
    for root_id, context in roots:
        try:
            expired.extend(permission.expire_lapsed(context, now=now))
        except Exception as exc:
            logger.warning(f"permission expiry sweep skipped root={root_id} error={exc!r}")
    return tuple(expired)


async def run_expiry_loop(ctx: MethodContext, *, stop_event: asyncio.Event) -> None:
    """Sweep for lapsed permissions every :data:`SWEEP_SECONDS` until stopped.

    Args:
        ctx: The daemon context whose attached trees are swept.
        stop_event: Set when the daemon shuts down.
    """
    while not stop_event.is_set():
        await asyncio.to_thread(sweep_once, ctx, now=datetime.now(UTC))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_SECONDS)


__all__ = ["SWEEP_SECONDS", "run_expiry_loop", "sweep_once"]
