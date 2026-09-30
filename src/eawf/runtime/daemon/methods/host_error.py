"""The ``runtime.host.error.observe`` verb: a host tool call that failed, on its Run's stream.

A host harness reports a tool call that failed through its post-tool hook. The Run the
call belongs to would otherwise carry no trace of it, and a transcript that shows the
call's request with nothing after it reads as work still going. This verb binds the
failure to the Run on the host's session and states ``error_observed`` there: a code
naming the tool and how it failed, what a retry would take, and the host's own words,
cut to a block's length and withheld whole when they carry a path, an address or a
token shape.

The verb is idempotent on the host's call: a retried hook repeats the same event id,
which the append answers with the line already standing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from eawf.kernel.runtime.events import ErrorPayload, RunEventKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.host_subagent import HARNESS_ACTORS
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.daemon.methods.run import append_run_event
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.run_events import RunEventAppend
from eawf.runtime.runtimes.host_transcript import HostHarness, scrubbed_words

logger = logging.getLogger(__name__)

#: The verb a failed host tool call is stated through.
HOST_ERROR_OBSERVE_METHOD: Final = "runtime.host.error.observe"

_HostText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
_Words = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)]


class HostToolError(BaseModel):
    """Params of :data:`HOST_ERROR_OBSERVE_METHOD`, as the host's hook reports them.

    Attributes:
        harness: The host harness whose call failed.
        host_session_id: The host's own id of the session, or of the subagent, calling.
        tool_use_id: The host's id of the failed call.
        tool_name: The host tool the call invoked.
        error: What the host said went wrong.
        interrupted: Whether the operator interrupted the call rather than the tool
            failing it.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostText
    tool_use_id: _HostText
    tool_name: _HostText
    error: _Words
    interrupted: StrictBool = False


class HostErrorAnswer(BaseModel):
    """What the verb answers with.

    Attributes:
        run_ref: The Run the failure was stated on.
        event_ref: The stream line it was stated as.
        disposition: Whether the line was appended, or already stood.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    event_ref: str
    disposition: str


def _tool_slug(tool_name: str) -> str:
    """Return the host tool's name as a code segment: lower snake case, letter first."""
    slug = re.sub(r"[^a-z0-9_]", "_", tool_name.lower())
    return (slug if slug[0].isalpha() else f"tool_{slug}")[:64]


def tool_error_payload(args: HostToolError, *, diagnostic_ref: str) -> ErrorPayload:
    """Return the error a failed host call states.

    An interrupted call may be retried as it stands, in the same Run; a call the tool
    failed needs its input changed first, since the host retried nothing.

    Args:
        args: The failure the host reported.
        diagnostic_ref: The stored content the host's full error text was filed as,
            which the block unfolds to as its trace.
    """
    tool = _tool_slug(args.tool_name)
    words = scrubbed_words(args.error) or "no words"
    return ErrorPayload(
        code=f"{tool}.{'interrupted' if args.interrupted else 'failed'}",
        retry_class="transient_same_run" if args.interrupted else "after_input_change",
        message=f"{args.tool_name} {'was interrupted' if args.interrupted else 'failed'}: {words}"[
            :500
        ],
        diagnostic_ref=diagnostic_ref,
    )


def observe_host_error(
    context: Epoch2RootContext, authority: RootAuthority, args: HostToolError, *, now: datetime
) -> HostErrorAnswer:
    """State a failed host tool call on the Run on the host's session.

    Raises:
        DaemonValidationError: The host session is on no one live Run.
    """
    run = host_run(authority, args.host_session_id)
    # the full error text is the trace, filed bounded and scrubbed before the line names it
    with context.session([run]) as session:
        trace = file_content(session, run_ref=run, text=args.error, now=now)
    body = f"{args.host_session_id}:{args.tool_use_id}:error"
    event_ref = f"EVT-{hashlib.sha256(body.encode()).hexdigest()[:32]}"
    answer = append_run_event(
        context,
        RunEventAppend(
            urn=run,
            event_ref=event_ref,
            run_sequence=1,
            event_kind=RunEventKind.ERROR_OBSERVED,
            provenance="provider_native",
            payload=tool_error_payload(args, diagnostic_ref=trace),
            actor=HARNESS_ACTORS[args.harness],
        ),
        now=now,
        at_tail=True,
    )
    logger.info(f"observe_host_error run={run.entity_key} disposition={answer.disposition}")
    return HostErrorAnswer(run_ref=str(run), event_ref=event_ref, disposition=answer.disposition)


@native_mutator(HOST_ERROR_OBSERVE_METHOD)
async def _observe_host_error(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """State a failed host tool call on the Run on the host's session."""
    args = native_params(HostToolError, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(
        observe_host_error, context, authority, args, now=datetime.now(UTC)
    )
    return answer.model_dump(mode="json")


__all__ = [
    "HOST_ERROR_OBSERVE_METHOD",
    "HostErrorAnswer",
    "HostToolError",
    "observe_host_error",
    "tool_error_payload",
]
