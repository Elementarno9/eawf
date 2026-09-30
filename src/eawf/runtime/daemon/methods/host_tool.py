"""``runtime.host.tool.observe``: a tool call the host harness made, on its Run's transcript.

The host's tool hooks call this verb twice per call: before the call runs, which states
the call as requested, and after it has run, which files its output and a receipt
through the gateway's host-call door and states the call as accepted and ended. The Run
is the one live Run the host session is the vendor session of, resolved exactly as a
held permission resolves it, so a subagent's calls land on the subagent's own Run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.host_calls import observe_host_call
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
    """What the verb answers with.

    Attributes:
        run_ref: The Run the call was accounted to.
        call_id: The gateway call id the call is known by.
        receipt_id: The receipt, once the call has run.
        status: The receipt's status, once the call has run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    call_id: str
    receipt_id: str | None = None
    status: str | None = None


def _observe(
    context: Epoch2RootContext,
    authority: RootAuthority,
    args: HostToolObserve,
    *,
    now: datetime,
) -> HostToolAnswer:
    """Resolve the Run and account for the call on it."""
    run = host_run(authority, args.host_session_id)
    call_id, receipt = observe_host_call(
        context,
        run_ref=run,
        tool_name=args.tool_name,
        host_call_key=args.host_call_key,
        output=args.output if args.phase == "result" else None,
        failed=args.failed,
        actor=HARNESS_ACTORS[args.harness],
        now=now,
    )
    return HostToolAnswer(
        run_ref=str(run),
        call_id=call_id,
        receipt_id=None if receipt is None else receipt.receipt_id,
        status=None if receipt is None else receipt.result.status,
    )


@native_mutator(HOST_TOOL_OBSERVE_METHOD)
async def _observe_host_tool(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Account for one host tool call on the Run its session runs as."""
    args = native_params(HostToolObserve, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_observe, context, authority, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "HOST_OUTPUT_CHARS",
    "HOST_TOOL_OBSERVE_METHOD",
    "HostToolAnswer",
    "HostToolObserve",
]
