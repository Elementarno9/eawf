"""``--stream`` output surface for long-running ``eawf`` verbs.

Verbs that subscribe to the daemon event bus (``wave dispatch``,
``wave dispatch-batch``, ``flow run``, ``audit run``, ``skill run``,
``metrics show --watch``, ``daemon logs --follow``) render their progress
through this module when ``--stream`` is passed. ``--stream`` is opt-in and
off by default so CI consumers keep a single deterministic stdout block.

Two wire shapes, picked by ``json_output``:

* **NDJSON** (``--json --stream``) — one complete JSON object per
  ``\\n``-terminated line. The opening line is a ``start`` frame, each
  daemon ``event.push`` becomes an ``event`` frame, and the stream is
  terminated by an ``end`` frame carrying the terminal ``status``. Every
  line is independently parseable; a partial line means a consumer-side
  parser bug or a killed process.
* **Human** (``--stream`` alone) — bracketed ``[HH:MM:SS]`` progress lines
  with a blank terminator line per the EOF semantics.

Frame shapes are typed via :class:`StreamFrame` so the renderer cannot drift
from the spec'd envelope. The module is daemon-agnostic: it consumes an
iterable of already-decoded daemon push-event dicts, so the NDJSON shape is
testable against an in-memory stream without a live socket.

Two flag-combination rejections live here because they gate the ``--stream``
surface:

* ``--md --stream`` — markdown is not round-trippable line-by-line;
  rejected with :class:`UserError` (``data.kind="InvalidInput"``).
* ``--quiet --verbose`` — contradictory output verbosity;
  rejected with :class:`UserError` (``data.kind="InvalidInput"``). A
  bad flag combination maps to operator-fixable input — exit 1
  USER_ERROR — matching the sibling ``--md --stream`` and
  ``--daemonless`` rejections and the ``-32602 → USER_ERROR`` row in
  :mod:`eawf.surfaces.cli.errors`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import IO, Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.errors import UserError

#: Terminal-status string carried by the ``end`` frame. ``ok`` exits 0;
#: ``failed`` exits 5 INTERNAL_ERROR; ``disconnected`` (daemon dropped the
#: subscription) exits 4 DAEMON_UNREACHABLE.
StreamStatus = Literal["ok", "failed", "disconnected"]

#: Exit code for each terminal stream status.
_STATUS_EXIT_CODE: dict[StreamStatus, int] = {
    "ok": exit_codes.OK,
    "failed": exit_codes.INTERNAL_ERROR,
    "disconnected": exit_codes.DAEMON_UNREACHABLE,
}


def _utcnow() -> datetime:
    """Return the current UTC datetime — module-level mockable default."""
    return datetime.now(tz=UTC)


class StreamFrame(BaseModel):
    """One line of ``--stream`` output (NDJSON shape).

    A single model carries all three frame types via the ``type``
    discriminator so the renderer round-trips one shape. Field presence
    varies by type:

    * ``start`` — ``scope_id`` + ``started_at`` (+ optional
      ``correlation_id``).
    * ``event`` — ``kind`` + ``payload`` (+ ``timestamp``); ``line`` carries
      a free-text log line for ``dispatch_log``-style events.
    * ``end`` — ``status`` + ``finished_at`` (+ optional ``correlation_id``).

    Unused fields stay ``None`` and are dropped from the NDJSON line via
    ``exclude_none`` so each line matches the § 5.8 example exactly.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["start", "event", "end"]

    # start / end
    scope_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: StreamStatus | None = None
    correlation_id: str | None = None

    # event
    kind: str | None = None
    payload: dict[str, Any] | None = None
    line: str | None = None
    timestamp: datetime | None = None


def start_frame(
    *,
    scope_id: str,
    started_at: datetime | None = None,
    correlation_id: str | None = None,
) -> StreamFrame:
    """Build the opening ``start`` frame for a stream.

    Args:
        scope_id: Canonical URN of the scope the long-running op targets.
        started_at: Stream open time; defaults to now (UTC).
        correlation_id: JSON-RPC request id joining the stream to daemon
            log entries; ``None`` for daemonless streams.

    Returns:
        A ``type="start"`` :class:`StreamFrame`.
    """
    return StreamFrame(
        type="start",
        scope_id=scope_id,
        started_at=started_at if started_at is not None else _utcnow(),
        correlation_id=correlation_id,
    )


def event_frame(
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    line: str | None = None,
    timestamp: datetime | None = None,
) -> StreamFrame:
    """Build an ``event`` frame from a daemon ``event.push`` notification.

    Args:
        kind: Event kind string (e.g. ``wave_claimed``, ``dispatch_log``).
        payload: Structured event payload; ``None`` for log-line-only
            events.
        line: Free-text log line for ``dispatch_log``-style events.
        timestamp: Event time; defaults to now (UTC).

    Returns:
        A ``type="event"`` :class:`StreamFrame`.
    """
    return StreamFrame(
        type="event",
        kind=kind,
        payload=payload,
        line=line,
        timestamp=timestamp if timestamp is not None else _utcnow(),
    )


def end_frame(
    *,
    status: StreamStatus,
    finished_at: datetime | None = None,
    correlation_id: str | None = None,
) -> StreamFrame:
    """Build the terminating ``end`` frame for a stream.

    Args:
        status: Terminal status — ``ok`` / ``failed`` / ``disconnected``.
        finished_at: Stream close time; defaults to now (UTC).
        correlation_id: JSON-RPC request id matching the ``start`` frame.

    Returns:
        A ``type="end"`` :class:`StreamFrame`.
    """
    return StreamFrame(
        type="end",
        status=status,
        finished_at=finished_at if finished_at is not None else _utcnow(),
        correlation_id=correlation_id,
    )


def render_ndjson_line(frame: StreamFrame) -> str:
    """Render *frame* as a single NDJSON line (no trailing newline).

    The line is a complete JSON object with ``None`` fields dropped so it
    matches the documented NDJSON example. orjson is used for
    byte-stability with the rest of the CLI output surface;
    ``OPT_SORT_KEYS`` keeps the key order deterministic across runs for
    golden diffs. No indent option is set — NDJSON requires exactly one
    object per line.

    Args:
        frame: The stream frame to serialise.

    Returns:
        A single-line JSON string (caller appends the ``\\n``).
    """
    payload = frame.model_dump(mode="json", exclude_none=True)
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS).decode("utf-8")


def _clock(frame: StreamFrame) -> str:
    """Return the ``HH:MM:SS`` clock prefix for a human-shape line.

    Picks the frame's own time field (``started_at`` / ``timestamp`` /
    ``finished_at``) and falls back to now when none is set.
    """
    moment = frame.started_at or frame.timestamp or frame.finished_at or _utcnow()
    return moment.strftime("%H:%M:%S")


def render_human_line(frame: StreamFrame, *, verbose: bool = False) -> str:
    """Render *frame* as a human-readable ``[HH:MM:SS] ...`` line.

    Args:
        frame: The stream frame to render.
        verbose: When False, ``dispatch_log`` lines are truncated to their
            first 80 characters with a ``--verbose`` hint.

    Returns:
        A single human-readable line (caller appends the ``\\n``).
    """
    clock = _clock(frame)
    if frame.type == "start":
        return f"[{clock}] starting for {frame.scope_id}..."
    if frame.type == "end":
        return f"[{clock}] done: {frame.status}"
    # event
    if frame.line is not None:
        # Collapse embedded newlines so one frame stays one human line — a
        # multi-line ``dispatch_log`` body must not fan out into N terminal
        # rows (the NDJSON branch is already safe: JSON escapes ``\n``).
        body = " ".join(frame.line.splitlines())
        if not verbose and len(body) > 80:
            body = f"{body[:80]}... (truncated; pass --verbose for full)"
        return f"[{clock}]   {frame.kind}: {body}"
    return f"[{clock}] {frame.kind}"


def reject_unstreamable_combination(*, md_output: bool, stream: bool) -> None:
    """Reject ``--md --stream`` — markdown is not line-streamable.

    Args:
        md_output: Whether ``--md`` was passed.
        stream: Whether ``--stream`` was passed.

    Raises:
        UserError: When both *md_output* and *stream* are set. The error
            carries no envelope ``data`` itself — the caller passes
            ``data={"kind": "InvalidInput"}`` to :func:`emit_error`.
    """
    if md_output and stream:
        raise UserError("--md is not streamable; use --json --stream")


def reject_quiet_verbose_collision(*, quiet: bool, verbose: bool) -> None:
    """Reject the contradictory ``--quiet --verbose`` pair.

    Args:
        quiet: Whether ``--quiet`` was passed.
        verbose: Whether ``--verbose`` was passed.

    Raises:
        UserError: When both *quiet* and *verbose* are set. Mapped to exit 1
            USER_ERROR with ``data.kind="InvalidInput"`` by the caller —
            the canonical taxonomy treats a bad flag combination as
            operator-fixable input.
    """
    if quiet and verbose:
        raise UserError("--quiet and --verbose are mutually exclusive")


def _coerce_status(raw: Any) -> StreamStatus:
    """Map a daemon-supplied terminal-status string onto :data:`StreamStatus`.

    Unknown / missing values fall back to ``failed`` so an opaque terminal
    frame surfaces as a non-zero exit rather than a silent success.
    """
    for status in _STATUS_EXIT_CODE:
        if raw == status:
            return status
    return "failed"


def stream_events(
    events: object,
    *,
    scope_id: str,
    json_output: bool,
    verbose: bool = False,
    correlation_id: str | None = None,
    out: IO[str] | None = None,
) -> int:
    """Render a daemon push-event stream as ``--stream`` output.

    Consumes *events* — an iterable of decoded daemon ``event.push``
    payloads — and writes a ``start`` frame, one frame per event, and a
    terminating ``end`` frame to *out*. Each daemon event dict may carry a
    ``terminal`` truthy marker plus a ``status`` field to signal the end of
    the long-running op; otherwise the stream ends ``ok`` when the iterable
    is exhausted.

    Args:
        events: Iterable of daemon event payload dicts. Each dict carries at
            least ``kind``; ``payload`` / ``line`` / ``timestamp`` are
            optional. A truthy ``terminal`` key marks the last event and its
            ``status`` drives the terminal frame + exit code.
        scope_id: Canonical URN of the scope the op targets.
        json_output: When True, emit NDJSON; otherwise emit human text.
        verbose: Forwarded to :func:`render_human_line` for log truncation.
        correlation_id: JSON-RPC request id stamped onto start + end frames.
        out: Output stream; defaults to ``sys.stdout``.

    Returns:
        The exit code for the terminal status — 0 (ok), 5 (failed), or 4
        (disconnected).
    """
    sink = out if out is not None else sys.stdout

    def _write(frame: StreamFrame) -> None:
        if json_output:
            sink.write(render_ndjson_line(frame) + "\n")
        else:
            sink.write(render_human_line(frame, verbose=verbose) + "\n")
        # Flush per frame so a block-buffered pipe (``eawf ... | cat``) sees
        # each frame as it lands instead of one dump at process exit.
        sink.flush()

    _write(start_frame(scope_id=scope_id, correlation_id=correlation_id))

    status: StreamStatus = "ok"
    for raw in events:  # type: ignore[attr-defined]
        if not isinstance(raw, dict):
            continue
        _write(
            event_frame(
                kind=str(raw.get("kind", "event")),
                payload=raw.get("payload"),
                line=raw.get("line"),
            )
        )
        if raw.get("terminal"):
            status = _coerce_status(raw.get("status", "ok"))
            break

    # EOF semantics: NDJSON terminates with the ``end`` frame
    # (machine consumers parse it for the terminal status); human output
    # terminates with a single blank ``^$`` marker line and surfaces the
    # status through the terminal event line, not a duplicate ``end`` line.
    if json_output:
        _write(end_frame(status=status, correlation_id=correlation_id))
    else:
        sink.write("\n")
        sink.flush()
    return _STATUS_EXIT_CODE[status]


__all__ = [
    "StreamFrame",
    "StreamStatus",
    "end_frame",
    "event_frame",
    "reject_quiet_verbose_collision",
    "reject_unstreamable_combination",
    "render_human_line",
    "render_ndjson_line",
    "start_frame",
    "stream_events",
]
