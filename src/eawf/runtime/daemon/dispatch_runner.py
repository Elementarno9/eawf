"""Store-only event producers for spawned and forwarded session output.

Every event here goes through the canonical ``event.jsonl`` writer
(:func:`eawf.kernel.store.append.append_envelope`, under the per-file
portalock and fsync) and never through a bus push: the callers hold an
event-store path but no daemon context, and the Watch tail's store poll
surfaces the rows either way.

Two producers live here:

* the live ``agent.output.chunk`` rows a spawn streams
  (:func:`persist_agent_output_chunk`) or an external orchestrator forwards
  for its session (:func:`persist_forwarded_output_chunk`);
* the typed ``session_closed`` row the session store emits once a session
  row turns terminal (:func:`emit_session_closed`).

A typed payload is validated through :data:`C09EventPayloadUnion` before
it is folded into the generic :class:`eawf.kernel.store.envelope.Envelope`
``payload`` dict, so a body that does not match its ``event_type``
discriminator fails at emit time rather than at projection time.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args

from pydantic import TypeAdapter

from eawf.kernel.state.enums import AgentSessionStatus, StoreKind
from eawf.kernel.state.ids import RE_WAVE
from eawf.kernel.state.models import AgentSession, State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.events import (
    AgentOutputChunkPayload,
    C09EventPayloadUnion,
    SessionClosedPayload,
)
from eawf.kernel.store.kinds.events.base import RuntimeTriple, runtime_triple_label
from eawf.observability.telemetry.models import EndMarker

logger = logging.getLogger(__name__)

#: Adapter used to validate a typed payload through the discriminated
#: union before it is folded into the envelope ``payload`` dict. Building
#: the adapter once at import keeps the per-emit cost to a dict round-trip.
_PAYLOAD_ADAPTER: TypeAdapter[Any] = TypeAdapter(C09EventPayloadUnion)


#: Stdout lines the live-chunk producer buffers before flushing one chunk event.
#: Batching bounds ``event.jsonl`` growth (a chatty spawn emits hundreds of
#: lines); a final flush at spawn end empties any partial batch.
_CHUNK_BATCH_LINES: int = 20

#: Wall-clock budget (seconds) after which a partially-filled chunk buffer is
#: flushed even below :data:`_CHUNK_BATCH_LINES`. Codex streams its output
#: in bursts at turn boundaries, so a sub-batch burst would otherwise sit
#: unpersisted -- and thus invisible to the Watch tail, which reads chunks off
#: the event store -- until the next burst fills the count batch. Bounding the
#: hold time keeps a slow turn's output flowing to the live tail.
_CHUNK_FLUSH_INTERVAL_S: float = 1.5


def _chunk_should_flush(*, buffered: int, elapsed_s: float) -> bool:
    """Return whether a partially-filled chunk buffer should flush now.

    Flushes when the buffer reached the line-count batch (:data:`_CHUNK_BATCH_LINES`)
    OR the wall-clock hold budget elapsed (:data:`_CHUNK_FLUSH_INTERVAL_S`), so a
    sub-batch codex burst still persists to the event store -- and thus the Watch
    tail -- within the hold budget instead of waiting for the next burst to fill
    the count batch.

    Args:
        buffered: Lines currently buffered (after appending the newest line).
        elapsed_s: Seconds since the last flush.

    Returns:
        ``True`` when either the count or the time threshold is met.
    """
    return buffered >= _CHUNK_BATCH_LINES or elapsed_s >= _CHUNK_FLUSH_INTERVAL_S


#: Ring-buffer cap on the number of raw output lines one spawned session fans to
#: the live tail. A spawn can emit a very large answer; capping the lines
#: the producer publishes bounds the event payload + the App-side
#: ``live_output_buffer`` so the tail never grows unbounded -- the operator reads
#: the freshest output, the oldest scrolls off. The TAIL of the output is kept
#: (the most recent lines) since that is what an operator watching a live run
#: cares about.
AGENT_OUTPUT_LINE_CAP: int = 200

#: Max characters of any single captured output line the producer publishes, so a
#: pathological no-newline blob cannot blow the event payload.
_AGENT_OUTPUT_LINE_MAX_CHARS: int = 2000


def capture_output_lines(text: str, *, cap: int = AGENT_OUTPUT_LINE_CAP) -> list[str]:
    """Split a spawn's captured *text* into the bounded tail of output lines.

    Splits the captured output into non-empty lines and keeps
    the LAST *cap* of them (the freshest tail the operator watches), each bounded
    to :data:`_AGENT_OUTPUT_LINE_MAX_CHARS` so a no-newline blob cannot blow the
    payload. Blank lines are dropped so the tail carries signal, not whitespace.

    Args:
        text: The spawn's captured output text.
        cap: Maximum lines to keep (the ring-buffer tail cap).

    Returns:
        The bounded tail of non-empty output lines, oldest-first.
    """
    lines = [line[:_AGENT_OUTPUT_LINE_MAX_CHARS] for line in text.splitlines() if line.strip()]
    return lines[-cap:] if cap > 0 else []


def persist_agent_output_chunk(
    events_path: Path,
    *,
    scope_id: str,
    session_id: str | None,
    seq: int,
    text: str,
    trace_request_id: str | None = None,
) -> str | None:
    """Persist one LIVE stdout batch off a bus-less, context-less spawn path.

    For spawn paths that hold an ``event.jsonl`` :class:`~pathlib.Path` but no
    :class:`~eawf.runtime.daemon.methods.MethodContext` -- the auditor / jury
    spawn factory, whose ``ctx`` is severed at the close-gate boundary (only
    paths cross into :func:`eawf.workflow.verify.oracle.run_oracle`). It builds
    the same union-validated :class:`AgentOutputChunkPayload` envelope keyed on
    *scope_id* and appends it through the canonical event writer
    (:func:`eawf.kernel.store.append.append_envelope`) WITHOUT the bus push: the
    Watch tail's store-poll timer surfaces the row within the poll
    interval, so the bus push is only a latency bonus the close-gate path can
    forgo. Empty output is a no-op (no empty event).

    The envelope ``scope_id`` is the caller-supplied *scope_id* (the auditor
    session scope, e.g. ``{wave_id}::audit``) rather than a bare wave id, so the
    chunk routes to the auditor's roster row -- the tail filters on ``scope_id``
    alone. The payload ``wave_id`` is set to the same scope so the render path
    (shared with the terminal ``agent.output`` event) stays consistent.

    Args:
        events_path: Path to ``event.jsonl`` for the canonical append.
        scope_id: The scope the chunk routes to (the auditor session scope the
            Watch tail + store backfill filter on).
        session_id: Runtime session id of the spawn, or ``None`` when unknown.
        seq: Per-spawn monotonic chunk index (0-based) so the chunk order is
            reconstructible from the persisted rows.
        text: The batched output text for this chunk (one or more stdout lines).
        trace_request_id: Optional daemon RPC request id for the correlation
            chain.

    Returns:
        The id of the appended envelope, or ``None`` when there was no output to
        persist.
    """
    lines = capture_output_lines(text)
    if not lines:
        return None
    now = datetime.now(UTC)
    joined = "\n".join(lines)
    summary = f"agent_output_chunk scope={scope_id} seq={seq} lines={len(lines)}"
    payload = AgentOutputChunkPayload(
        timestamp=now,
        wave_id=scope_id,
        session_id=session_id,
        seq=seq,
        lines=joined,
        trace_request_id=trace_request_id,
        trace_wave_id=scope_id,
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(events_path, envelope)
    logger.info(
        f"persist_agent_output_chunk scope={scope_id} seq={seq} lines={len(lines)} "
        f"envelope_id={envelope.id!r}"
    )
    return envelope.id


@dataclass(frozen=True)
class ForwardedOutputChunk:
    """One externally forwarded output chunk after ingestion.

    Attributes:
        scope_id: The scope the chunk was routed to -- the session row's own
            scope, which is the key the Watch tail filters on.
        runtime_session_id: The runtime session id stamped on the persisted
            chunk, or ``None`` when the session row carries none.
        envelope_id: The appended envelope id, or ``None`` when the forwarded
            text held no renderable line (a no-op rather than an empty row).
    """

    scope_id: str
    runtime_session_id: str | None
    envelope_id: str | None


def persist_forwarded_output_chunk(
    events_path: Path,
    *,
    state: State,
    session_id: str,
    seq: int,
    text: str,
) -> ForwardedOutputChunk:
    """Persist one output chunk an external orchestrator forwarded for its session.

    The ingestion half of the streaming seam. A session the daemon did not spawn
    has no in-process stdout pipe behind it, so its Watch row renders honest
    empty for the whole run unless whoever owns that process forwards the
    output. This lands one forwarded batch as the very same
    ``agent.output.chunk`` row the spawn path writes (through
    :func:`persist_agent_output_chunk`), so the Watch reader cannot tell a
    forwarded chunk from a spawned one.

    Routing identity is read from the session row rather than taken from the
    forwarder: the Watch tail filters chunks on scope AND runtime session id, so
    resolving both from state is what guarantees a forwarded chunk reaches the
    reader instead of landing in a scope nothing reads.

    Args:
        events_path: Path to ``event.jsonl`` for the canonical append.
        state: Validated state carrying the session row (read-only).
        session_id: Id of the :class:`~eawf.kernel.state.models.AgentSession`
            the forwarded chunk belongs to.
        seq: Per-session monotonic chunk index (0-based) so the chunk order is
            reconstructible from the persisted rows.
        text: The forwarded output text for this chunk.

    Returns:
        The resolved routing identity plus the appended envelope id (``None``
        when the forwarded text held nothing renderable).

    Raises:
        KeyError: When *session_id* names no session in *state*.
    """
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise KeyError(f"unknown agent session: {session_id}")
    envelope_id = persist_agent_output_chunk(
        events_path,
        scope_id=session.scope_id,
        session_id=session.runtime_session_id,
        seq=seq,
        text=text,
    )
    logger.info(
        f"persist_forwarded_output_chunk session={session_id} scope={session.scope_id} "
        f"seq={seq} envelope_id={envelope_id!r}"
    )
    return ForwardedOutputChunk(
        scope_id=session.scope_id,
        runtime_session_id=session.runtime_session_id,
        envelope_id=envelope_id,
    )


#: How a terminal :class:`AgentSessionStatus` reads as a telemetry
#: :data:`~eawf.observability.telemetry.models.EndMarker`. A session that is not
#: in this map is not terminal, so it has nothing to close-report; membership
#: doubles as the terminality test in :func:`emit_session_closed`.
_SESSION_END_MARKERS: dict[AgentSessionStatus, EndMarker] = {
    AgentSessionStatus.CLOSED: "clean_stop",
    AgentSessionStatus.STALE: "away",
    AgentSessionStatus.FAILED: "other",
}

#: The closed runtime set the telemetry surface keys on. ``AgentSession.runtime``
#: is an open ``str``, so a row naming a runtime outside this set has no
#: projectable spelling and is reported rather than coerced.
_RUNTIME_TRIPLES: frozenset[str] = frozenset(get_args(RuntimeTriple))


def _session_log_handle(session: AgentSession) -> str:
    """Return the opaque session-log handle stamped on a close event.

    Mirrors the adapter URN shape (``urn:eawf:v1:session-log:<runtime>:<id>``)
    so a handle minted here resolves through the same daemon-side map as one
    an adapter minted. It is a handle rather than a path because the event
    store is committed to version control (AGENTS rule 16).

    Args:
        session: The session row the handle identifies.

    Returns:
        The URN-shaped handle; falls back to the eawf session id when the
        runtime never reported one of its own, since the payload field is
        required and non-empty.
    """
    return f"urn:eawf:v1:session-log:{session.runtime}:{session.runtime_session_id or session.id}"


def _session_wave_id(session: AgentSession) -> str | None:
    """Return the wave a session served, or ``None`` for a non-wave session.

    A claimed wave is authoritative; a session that never claimed one still
    binds a wave when it was opened *under* a wave scope, which is how the
    fleet lanes and the executor dispatch open their sessions. Anything else
    (a project- or iter-scoped interactive session) has no wave.

    Args:
        session: The session row to resolve.

    Returns:
        The wave id, or ``None``.
    """
    if session.claimed_wave_ids:
        return session.claimed_wave_ids[0]
    return session.scope_id if RE_WAVE.match(session.scope_id) else None


def emit_session_closed(events_path: Path, *, session: AgentSession) -> str | None:
    """Emit the typed ``session_closed`` event for a terminalized session.

    The producer behind the telemetry session projection: every terminal
    session path funnels through the session store, which calls this once the
    row is terminal, so a rebuild over the canonical event store reports the
    sessions that actually ran instead of an empty table.

    The event is self-contained -- it carries the open instant alongside the
    close instant -- so an incremental tail scan that sees only this line still
    projects a complete row with the right duration.

    Token tallies stay at zero and ``model`` at ``None``: an
    :class:`~eawf.kernel.state.models.AgentSession` row carries neither. The
    session row contributes identity and duration.

    Args:
        events_path: Path to ``event.jsonl`` for the canonical append.
        session: The terminalized session row, with ``ended_at`` stamped.

    Returns:
        The id of the appended envelope, or ``None`` when *session* has no
        projectable close: it is not in a terminal status, carries no
        ``ended_at``, names a runtime outside the closed telemetry set, or
        ends before it opened (a reversed pair projects a negative duration
        that every downstream percentile silently drops).

    Raises:
        eawf.kernel.state.errors.StateConflict: When the append lock on
            *events_path* cannot be acquired.
    """
    marker = _SESSION_END_MARKERS.get(session.status)
    if marker is None or session.ended_at is None:
        logger.debug(
            f"emit_session_closed skip reason=not-terminal session={session.id!r} "
            f"status={session.status.value}"
        )
        return None
    runtime = runtime_triple_label(session.runtime)
    if runtime not in _RUNTIME_TRIPLES:
        logger.warning(
            f"emit_session_closed skip reason=unprojectable-runtime session={session.id!r} "
            f"runtime={session.runtime!r}"
        )
        return None
    if session.ended_at < session.started_at:
        logger.warning(
            f"emit_session_closed skip reason=ended-before-started session={session.id!r} "
            f"started_at={session.started_at.isoformat()} ended_at={session.ended_at.isoformat()}"
        )
        return None
    wave_id = _session_wave_id(session)
    payload = SessionClosedPayload(
        timestamp=session.ended_at,
        opened_at=session.started_at,
        session_id=session.id,
        runtime=cast("RuntimeTriple", runtime),
        session_log_handle=_session_log_handle(session),
        wave_id=wave_id,
        end_marker=marker,
        trace_wave_id=wave_id,
    )
    summary = f"session_closed session={session.id} runtime={runtime} end_marker={marker}"
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=session.scope_id,
        created_at=session.ended_at,
        updated_at=None,
        summary=summary,
        payload=_PAYLOAD_ADAPTER.validate_python(payload).model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(events_path, envelope)
    logger.info(
        f"emit_session_closed session={session.id!r} runtime={runtime} "
        f"end_marker={marker} envelope_id={envelope.id!r}"
    )
    return envelope.id
