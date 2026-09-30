"""State one phase of a tool call on the calling Run's own stream.

A tool call is three lines of the Run's transcript: ``tool_requested`` when the call
arrives, ``tool_accepted`` once it is going to run, and ``tool_result`` naming the
receipt it earned or the error it failed with. Every line is named by the call and the
phase, so a retried call restates the line already standing rather than adding one.
The lines take the stream's tail, because a Run's stream has other writers.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.events import EventProvenance, RunEventKind, ToolPayload
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.run_events import RunEventAppend

logger = logging.getLogger(__name__)

ToolPhase = Literal["requested", "accepted", "result"]

#: The event kind each phase of a tool call is stated under.
TOOL_PHASE_KINDS: Final = MappingProxyType(
    {
        "requested": RunEventKind.TOOL_REQUESTED,
        "accepted": RunEventKind.TOOL_ACCEPTED,
        "result": RunEventKind.TOOL_RESULT,
    }
)


def tool_event_ref(call_id: str, phase: ToolPhase) -> str:
    """Return the event id one phase of one call is stated under."""
    return f"EVT-{hashlib.sha256(f'{call_id}:{phase}'.encode()).hexdigest()[:32]}"


def state_tool_phase(
    session: RootSession,
    *,
    run_ref: QualifiedUrn,
    payload: ToolPayload,
    provenance: EventProvenance,
    actor: str,
    now: datetime,
) -> None:
    """Append one tool phase to the Run's stream inside the session already open.

    Args:
        session: The open session whose locks cover the Run.
        run_ref: The Run that made the call.
        payload: The phase, with its receipt or error at ``result``.
        provenance: Who observed the call.
        actor: The principal the line is attributed to.
        now: The daemon's recording clock.
    """
    # The run verbs reach the MCP grant, which reads the gateway's tables, so the
    # gateway's own import of this module cannot take them at import time.
    from eawf.runtime.daemon.methods.run import append_run_event_in_session

    answer = append_run_event_in_session(
        session,
        RunEventAppend(
            urn=run_ref,
            event_ref=tool_event_ref(payload.call_ref, payload.phase),
            run_sequence=1,
            event_kind=TOOL_PHASE_KINDS[payload.phase],
            provenance=provenance,
            payload=payload,
            actor=actor,
        ),
        now=now,
        at_tail=True,
    )
    logger.debug(
        f"state_tool_phase call={payload.call_ref} phase={payload.phase} "
        f"disposition={answer.disposition}"
    )


__all__ = [
    "TOOL_PHASE_KINDS",
    "ToolPhase",
    "state_tool_phase",
    "tool_event_ref",
]
