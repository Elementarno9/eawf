"""``runtime.host.tool.observe``: a tool call the host harness made, on its Run's transcript.

The host's tool hooks call this verb twice per call: before the call runs, which states
the call as requested, and after it has run, which files its output and a receipt
through the gateway's host-call door and states the call as accepted and ended. The Run
is the one live Run the host session is the vendor session of, resolved exactly as a
held permission resolves it, so a subagent's calls land on the subagent's own Run.

The hook gives up on an answer after a short deadline, and under concurrent load the
document lock alone outlasts it, so the verb answers once the call is queued: the Run
and the call id need no lock, and one drainer per root lands the queued calls in one
document write. A stopping daemon answers only once the call has landed, and awaits
the drainer before it exits.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.host_calls import HostCallObservation, HostCallQueue
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.runtimes.host_transcript import HostHarness

logger = logging.getLogger(__name__)

#: Account for one host tool call on the Run its session runs as.
HOST_TOOL_OBSERVE_METHOD: Final = "runtime.host.tool.observe"

#: The most output characters a hook may carry to the daemon. The store keeps far less;
#: the cap only keeps one request from being as large as the process that wrote it.
HOST_OUTPUT_CHARS: Final = 65_536

_HostText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class HostToolObserve(BaseModel):
    """What a host tool hook reports.

    Attributes:
        harness: The host harness that made the call.
        host_session_id: The session, or the subagent, the call was made in.
        tool_name: The host's name for the tool.
        host_call_key: The host's own id of the call.
        phase: ``requested`` before the call runs, ``result`` once it has.
        output: What the call returned, at ``result``.
        failed: Whether the call that ran failed.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostText
    tool_name: _HostText
    host_call_key: _HostText
    phase: Literal["requested", "result"]
    output: Annotated[str, StringConstraints(strict=True, max_length=HOST_OUTPUT_CHARS)] = ""
    failed: bool = False


class HostToolAnswer(BaseModel):
    """What the verb answers with, once the call is queued.

    Attributes:
        run_ref: The Run the call is accounted to.
        call_id: The gateway call id the call is known by.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    call_id: str


#: Each root's queue, held as long as the daemon holds the root's context.
_QUEUES: Final[weakref.WeakKeyDictionary[Epoch2RootContext, HostCallQueue]] = (
    weakref.WeakKeyDictionary()
)
_QUEUES_LOCK: Final = threading.Lock()


def _queue(ctx: MethodContext, context: Epoch2RootContext) -> HostCallQueue:
    """Return the one queue of the root *context* belongs to."""
    with _QUEUES_LOCK:
        queue = _QUEUES.get(context)
        if queue is None:
            queue = _QUEUES[context] = HostCallQueue(context, stopping=_stopping(ctx))
    return queue


def _stopping(ctx: MethodContext) -> Callable[[], bool]:
    """Return whether the daemon of *ctx* has begun to stop, for the drainer's thread.

    ``daemon.shutdown`` marks the context first; a signal only sets the shutdown
    event, so both are read.
    """

    def stopping() -> bool:
        event = ctx.shutdown_event
        return ctx.stopping or (isinstance(event, asyncio.Event) and event.is_set())

    return stopping


def _starter(loop: asyncio.AbstractEventLoop) -> Callable[[Callable[[], None]], object]:
    """Return what starts a drainer on *loop*'s default executor.

    The executor, unlike a task, is neither cancelled nor abandoned when the
    loop ends: the daemon's loop waits for its threads before the process
    exits, so a drainer started here lands everything queued first. The
    drainer carries the caller's context, so its document lock is logged
    under this method.
    """

    def start(drain: Callable[[], None]) -> object:
        return loop.run_in_executor(None, contextvars.copy_context().run, drain)

    return start


@native_mutator(HOST_TOOL_OBSERVE_METHOD)
async def _observe_host_tool(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Account for one host tool call on the Run its session runs as."""
    args = native_params(HostToolObserve, params)
    context = ctx.native_root_context(authority.root)
    run = await asyncio.to_thread(host_run, authority, args.host_session_id)
    observation = HostCallObservation(
        run_ref=run,
        tool_name=args.tool_name,
        host_call_key=args.host_call_key,
        output=args.output if args.phase == "result" else None,
        failed=args.failed,
        actor=HARNESS_ACTORS[args.harness],
        now=datetime.now(UTC),
    )
    landing = _queue(ctx, context).put(observation, start=_starter(asyncio.get_running_loop()))
    if ctx.stopping:
        # Past this answer the daemon may be gone, so the call is on disk first.
        await asyncio.wrap_future(landing)
    logger.debug(f"_observe_host_tool queued call={observation.call_id} run={run}")
    return HostToolAnswer(run_ref=str(run), call_id=observation.call_id).model_dump(mode="json")


__all__ = [
    "HOST_OUTPUT_CHARS",
    "HOST_TOOL_OBSERVE_METHOD",
    "HostToolAnswer",
    "HostToolObserve",
]
