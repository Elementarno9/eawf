"""The lines a dispatched worker's turn states on its Run's own stream.

A Run the host harness spawned gets its transcript from the host's hooks; a Run Eawf
starts itself has no hook, so the dispatch path is the producer. The launcher hands
each message the worker says to :func:`message_sink` as it arrives, and a worker that
fails to start is stated by :func:`state_spawn_failure`, its full cause filed as the
error's trace. Every line takes the stream's tail, because the Run's hooks write the
same stream, and its id is derived from the attempt, so a replay repeats it.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.events import ErrorPayload, MessageSummaryPayload, RunEventKind
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.run_events import RunEventAppend
from eawf.runtime.runtimes.host_transcript import scrubbed_words

logger = logging.getLogger(__name__)


def _append(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: str,
    event_ref: str,
    kind: RunEventKind,
    payload: MessageSummaryPayload | ErrorPayload,
) -> None:
    """State one line the worker's turn produced on the Run's own stream.

    A line the stream refuses is logged and dropped: the transcript is an observation
    of the turn, and losing one line of it must never stop the worker.
    """
    # the run verbs import the dispatch driver, which imports this module
    from eawf.runtime.daemon.methods.run import append_run_event

    try:
        append_run_event(
            context,
            RunEventAppend(
                urn=urn,
                event_ref=event_ref,
                run_sequence=1,
                event_kind=kind,
                provenance="provider_native",
                payload=payload,
                actor=actor,
            ),
            now=datetime.now(UTC),
            at_tail=True,
        )
    except DaemonValidationError as error:
        logger.warning(f"dispatch_stream refused run={urn.entity_key!r} cause={error}")


def _event_ref(attempt_ref: str, label: str) -> str:
    """Return the id of one stream line of an attempt, the same on every replay."""
    body = f"{attempt_ref}:{label}"
    return f"EVT-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:32]}"


def message_sink(
    context: Epoch2RootContext, urn: QualifiedUrn, *, actor: str, attempt_ref: str
) -> Callable[[MessageSummaryPayload], Awaitable[None]]:
    """Return where the launcher hands each message the worker says, in order.

    Args:
        context: The tree the Run lives in.
        urn: The dispatched Run.
        actor: The principal the dispatch was asked by.
        attempt_ref: The dispatch attempt the worker was started under.

    Returns:
        A sink stating each message as ``message_summarized`` on the Run's stream.
    """
    said = itertools.count()

    async def sink(message: MessageSummaryPayload) -> None:
        await asyncio.to_thread(
            _append,
            context,
            urn,
            actor=actor,
            event_ref=_event_ref(attempt_ref, f"message:{next(said)}"),
            kind=RunEventKind.MESSAGE_SUMMARIZED,
            payload=message,
        )

    return sink


def state_spawn_failure(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: str,
    attempt_ref: str,
    code: str,
    cause: str,
) -> None:
    """State on the Run's stream that its worker failed to start, and why.

    A retry mints a new attempt of the same Run, so the failure is one the same Run
    may get past.

    Args:
        context: The tree the Run lives in.
        urn: The dispatched Run.
        actor: The principal the dispatch was asked by.
        attempt_ref: The dispatch attempt whose worker failed.
        code: The refusal the dispatch answers with.
        cause: What the launcher said went wrong, filed whole as the trace.
    """
    with context.session([urn]) as session:
        trace = file_content(session, run_ref=urn, text=cause, now=datetime.now(UTC))
    _append(
        context,
        urn,
        actor=actor,
        event_ref=_event_ref(attempt_ref, "spawn_failed"),
        kind=RunEventKind.ERROR_OBSERVED,
        payload=ErrorPayload(
            code=code,
            retry_class="transient_same_run",
            message=scrubbed_words(cause) or "the worker exited without saying why",
            diagnostic_ref=trace,
        ),
    )


__all__ = ["message_sink", "state_spawn_failure"]
