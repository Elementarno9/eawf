"""asyncio JSON-RPC 2.0 server for eawfd.

Each accepted connection runs in its own asyncio task that reads
newline-delimited JSON frames, dispatches the method through
:mod:`eawf.runtime.daemon.methods`, and writes a response frame back. The frame
format is one JSON object per ``\\n``-terminated line.

The server is process-internal — the entry point in :mod:`eawf.runtime.daemon.main`
binds the Unix domain socket and wires the :class:`MethodContext`.
Tests that exercise the framing layer drive
:func:`handle_connection` directly with paired ``asyncio.StreamReader`` /
``asyncio.StreamWriter`` halves.

W06 adds the subscription bus: every accepted connection participates
in fan-out via :class:`eawf.runtime.daemon.bus.EventBus` carried on
``ctx.bus``. The connection handler intercepts ``event.subscribe`` /
``state.subscribe`` frames before normal dispatch and switches into a
streaming loop that pushes ``event.push`` notifications down the wire
until the peer disconnects or the subscriber is unregistered.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from typing import Any, cast

import orjson

# Import to ensure handlers register before dispatch runs.
import eawf.runtime.daemon.methods.agent
import eawf.runtime.daemon.methods.config  # registers config.read / set_layer_value / list_layers
import eawf.runtime.daemon.methods.daemon
import eawf.runtime.daemon.methods.event
import eawf.runtime.daemon.methods.evidence  # registers evidence.append (P28-I01-W04)
import eawf.runtime.daemon.methods.fleet  # registers fleet.drive (P30-I12-W01)
import eawf.runtime.daemon.methods.needs_user  # registers needs_user.{raise,resolve,park}
import eawf.runtime.daemon.methods.registry  # registers registry.read / registry.update (W10)
import eawf.runtime.daemon.methods.research  # registers research.create_campaign (P29-I09-W07)
import eawf.runtime.daemon.methods.spec  # registers spec.{init,validate,promote,archive} (P25-W03)
import eawf.runtime.daemon.methods.spec_convert  # registers spec.convert_legacy (P30-I23-W24)
import eawf.runtime.daemon.methods.state  # registers state.read / state.mutate / state.digest
import eawf.runtime.daemon.methods.state_subscribe  # noqa: F401  — registers (state|event).subscribe
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.auth import UnauthorizedError, verify_peer_credential
from eawf.runtime.daemon.bus import CatchUpTooLargeError, EventBus, Subscriber
from eawf.runtime.daemon.methods import (
    VALIDATION_FAILED,
    DaemonValidationError,
    MethodContext,
    MethodNotFoundError,
    dispatch,
)
from eawf.runtime.daemon.methods.event import subscribe as run_subscribe
from eawf.runtime.daemon.methods.state_subscribe import SUBSCRIBE_METHODS
from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

logger = logging.getLogger(__name__)

# Owner-only perms for the bound Unix socket. ``asyncio.start_unix_server``
# creates the socket node under the prevailing umask, which may leave it
# group/world-writable; the peer-credential check gates *who* may issue
# RPCs, but the filesystem node itself should also be unreachable by other
# local users. POSIX-only — Windows uses the named-pipe transport.
SOCKET_FILE_MODE: int = 0o600


# JSON-RPC 2.0 error codes (reserved range -32768..-32000) plus the
# daemon-specific extensions in the -32000..-32099 server-error band.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32000
CATCH_UP_TOO_LARGE = -32008
DAEMON_SHUTTING_DOWN = -32009
#: A live dispatch bound a report whose verdict is not close-ready (FAIL /
#: BLOCKED). This is a legitimate agent outcome, not a server fault, so it maps
#: to its own typed code (with the verdict + reasons in ``error.data``) rather
#: than the generic -32603 internal error a raised exception would otherwise get.
DISPATCH_CLOSE_BLOCKED = -32011


def _frame(obj: dict[str, Any]) -> bytes:
    """Serialise *obj* as a single line-delimited JSON frame.

    Args:
        obj: Mapping to encode.

    Returns:
        UTF-8 bytes terminated by ``\\n``.
    """
    return orjson.dumps(obj) + b"\n"


def _error(
    req_id: str | int | None,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC error response payload.

    Args:
        req_id: Echo of the request id; ``None`` when the request could
            not be parsed.
        code: Numeric JSON-RPC error code.
        message: Short human description.
        data: Optional forensic / structured payload attached as
            ``error.data`` per JSON-RPC 2.0. Used by the ``-32000``
            unauthorized envelope to carry the platform + expected vs.
            actual credentials.

    Returns:
        JSON-RPC error envelope.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": error,
    }


def _success(req_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response payload.

    Args:
        req_id: Echo of the request id.
        result: Handler result mapping.

    Returns:
        JSON-RPC success envelope.
    """
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _push_frame(envelope: Envelope) -> bytes:
    """Build a single ``event.push`` notification frame.

    Args:
        envelope: Envelope to wrap into the notification.

    Returns:
        Newline-terminated JSON-RPC notification frame.
    """
    notification = {
        "jsonrpc": "2.0",
        "method": "event.push",
        "params": {"event": envelope.model_dump(mode="json")},
    }
    return _frame(notification)


def _parse_frame(line: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Decode and validate a single JSON-RPC request frame.

    Args:
        line: Raw bytes for one frame (no trailing newline).

    Returns:
        Tuple ``(payload, error)``. Exactly one entry is non-None; on
        decode/validation failure the error dict is a ready-to-send
        JSON-RPC error envelope.
    """
    try:
        payload = orjson.loads(line)
    except orjson.JSONDecodeError:
        return None, _error(None, PARSE_ERROR, "parse error")
    if not isinstance(payload, dict):
        return None, _error(None, INVALID_REQUEST, "frame is not a JSON object")
    req_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0":
        return None, _error(req_id, INVALID_REQUEST, "missing jsonrpc=2.0")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return None, _error(req_id, INVALID_REQUEST, "missing method")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return None, _error(req_id, INVALID_PARAMS, "params must be an object")
    return payload, None


async def _process_frame(line: bytes, ctx: MethodContext) -> dict[str, Any]:
    """Decode + dispatch a single JSON-RPC frame.

    Refreshes :attr:`MethodContext.last_activity` for every dispatched
    method EXCEPT those in :data:`SUBSCRIBE_METHODS`; subscribers keep
    the daemon alive via the live-subscriber gate inside the idle
    watchdog, so counting them as RPC activity would double-credit a
    quiescent connection.

    Args:
        line: Raw bytes for one frame (without trailing newline).
        ctx: Server context shared across handlers.

    Returns:
        JSON-RPC response envelope (success or error).
    """
    payload, error = _parse_frame(line)
    if error is not None:
        return error
    assert payload is not None
    req_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params", {}) or {}
    if method not in SUBSCRIBE_METHODS:
        ctx.touch_activity()
    try:
        result = await dispatch(method, ctx, params)
    except MethodNotFoundError:
        return _error(req_id, METHOD_NOT_FOUND, f"method not found: {method!r}")
    except DaemonValidationError as exc:
        # A lifecycle-guard / post-invariant rejection — the param shape
        # was syntactically fine, the mutation was semantically refused.
        # Emit -32002 so the CLI client maps it to ValidationError
        # (exit 2), matching the in-process fallback for the same case.
        return _error(req_id, VALIDATION_FAILED, str(exc))
    except ValueError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
    except DispatchCloseBlockedError as exc:
        # A FAIL / BLOCKED report verdict is a legitimate agent outcome, not a
        # server fault. The report is already persisted; only the close-path
        # advance was refused. Surface a typed error (verdict + reasons in
        # ``error.data``) so the client can distinguish it from a real internal
        # error rather than seeing a generic -32603.
        return _error(
            req_id,
            DISPATCH_CLOSE_BLOCKED,
            str(exc),
            data={
                "wave_id": exc.wave_id,
                "verdict": exc.result.verdict.value,
                "reasons": list(exc.result.reasons),
            },
        )
    except Exception as exc:
        logger.exception(f"_process_frame method={method!r} unhandled")
        return _error(req_id, INTERNAL_ERROR, f"internal error: {exc}")
    return _success(req_id, result)


async def process_frame_bytes(payload: bytes, ctx: MethodContext) -> bytes:
    """Decode one frame, dispatch, and return the response as a frame.

    Wrapper exposed for transports that hand the dispatcher raw bytes
    rather than a stream — chiefly the Windows pipe bridge which
    couples a blocking listener thread to the asyncio dispatcher via
    ``loop.call_soon_threadsafe``.

    Args:
        payload: Raw bytes for one frame; trailing newline stripped
            on the caller's behalf.
        ctx: Server context shared across handlers.

    Returns:
        Newline-terminated JSON-RPC response frame.
    """
    response = await _process_frame(payload.rstrip(b"\n"), ctx)
    return _frame(response)


async def _watch_reader_eof(
    reader: asyncio.StreamReader,
    subscriber: Subscriber,
    bus: EventBus,
    connection_id: str,
) -> None:
    """Watch *reader* for EOF and close the subscriber when the peer leaves.

    The subscribe path stops reading frames after the initial request,
    so peer-disconnect must be observed independently. This coroutine
    runs in parallel with :func:`_stream_subscriber`; when
    ``reader.readline()`` returns empty (EOF) the subscriber is
    unregistered, which sets its ``event`` so the streamer loop exits.

    Args:
        reader: Reader half of the connection.
        subscriber: Live subscriber.
        bus: Event bus owning *subscriber*.
        connection_id: Connection id registered with the bus.
    """
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            # Extra frames after subscribe are not part of the W06
            # contract; ignore them quietly rather than fault the
            # connection.
            logger.debug(f"_watch_reader_eof unexpected-frame connection={connection_id!r}")
    finally:
        bus.unregister(connection_id)


async def _stream_subscriber(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    bus: EventBus,
    subscriber: Subscriber,
    backlog: list[Envelope],
    connection_id: str,
) -> None:
    """Flush *backlog* then live-stream pushes until the subscriber closes.

    Args:
        reader: Reader half of the connection (used to watch for EOF).
        writer: Writer half of the connection.
        bus: Event bus owning *subscriber*.
        subscriber: Subscriber returned by
            :func:`eawf.runtime.daemon.methods.event.subscribe`.
        backlog: Catch-up envelopes the subscriber missed; flushed in
            order before live push begins.
        connection_id: Connection id registered with the bus.
    """
    eof_task = asyncio.create_task(_watch_reader_eof(reader, subscriber, bus, connection_id))
    try:
        for env in backlog:
            writer.write(_push_frame(env))
            try:
                await writer.drain()
            except ConnectionResetError, BrokenPipeError:
                return
        async for env in bus.iter_subscriber_pushes(subscriber):
            writer.write(_push_frame(env))
            try:
                await writer.drain()
            except ConnectionResetError, BrokenPipeError:
                return
    finally:
        eof_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await eof_task


async def _handle_subscribe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ctx: MethodContext,
    payload: dict[str, Any],
    connection_id: str,
) -> Subscriber | None:
    """Register a subscriber and stream pushes for the rest of the connection.

    Args:
        reader: Reader half of the connection (used to watch for EOF
            so the streamer exits when the peer disconnects).
        writer: Writer half of the connection.
        ctx: Server context; ``ctx.bus`` MUST be a live
            :class:`EventBus`.
        payload: Parsed JSON-RPC request payload.
        connection_id: Stable identifier for the owning connection.

    Returns:
        The registered subscriber on success (caller unregisters on
        teardown) or ``None`` when subscribe failed and an error was
        already written to *writer*.
    """
    req_id = payload.get("id")
    params = payload.get("params", {}) or {}
    if not isinstance(ctx.bus, EventBus):
        writer.write(_frame(_error(req_id, INTERNAL_ERROR, "event bus not configured")))
        await writer.drain()
        return None
    try:
        sub, backlog = run_subscribe(
            ctx.bus,
            connection_id=connection_id,
            params=params,
            event_path=ctx.event_path,
        )
    except CatchUpTooLargeError as exc:
        writer.write(_frame(_error(req_id, CATCH_UP_TOO_LARGE, str(exc))))
        await writer.drain()
        return None
    except ValueError as exc:
        writer.write(_frame(_error(req_id, INVALID_PARAMS, str(exc))))
        await writer.drain()
        return None
    writer.write(_frame(_success(req_id, {"ok": True, "backlog_count": len(backlog)})))
    await writer.drain()
    await _stream_subscriber(reader, writer, ctx.bus, sub, backlog, connection_id)
    return sub


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ctx: MethodContext,
    *,
    expected_uid: int | None = None,
) -> None:
    """Per-connection task: read frames, dispatch, write responses.

    Args:
        reader: Asyncio stream reader for the connection.
        writer: Asyncio stream writer for the connection.
        ctx: Server context shared across handlers.
        expected_uid: Daemon's own UID. When non-None the peer-cred
            check runs before any frames are read; mismatch closes the
            connection with a ``-32000 unauthorized`` envelope.
    """
    peer = writer.get_extra_info("socket")
    # ``asyncio.start_unix_server`` exposes the accepted endpoint as an
    # :class:`asyncio.trsock.TransportSocket` — structurally compatible
    # with :class:`socket.socket` for the ``getsockopt`` + ``fileno`` API
    # the peer-cred check uses.
    if expected_uid is not None and peer is not None:
        try:
            verify_peer_credential(cast(socket.socket, peer), expected_uid=expected_uid)
        except UnauthorizedError as exc:
            logger.warning(f"handle_connection reject reason={exc!s}")
            writer.write(_frame(_error(None, UNAUTHORIZED, "unauthorized", data=exc.forensics)))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        except NotImplementedError:
            # Platform without a peer-cred recipe (e.g. Windows pipes
            # come through the dedicated pywin32 listener — not this
            # UDS path). Fall through to accept; the upper layer must
            # already gate access via DACL / SID.
            logger.debug("handle_connection skip peer-cred unsupported-platform")
    connection_id = uuid.uuid4().hex
    subscriber: Subscriber | None = None
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            payload, error = _parse_frame(line.rstrip(b"\n"))
            if error is not None:
                writer.write(_frame(error))
                await writer.drain()
                continue
            assert payload is not None
            method = payload["method"]
            if method in SUBSCRIBE_METHODS:
                subscriber = await _handle_subscribe(reader, writer, ctx, payload, connection_id)
                # Subscribe streams until the peer disconnects, so we
                # fall through to the cleanup block; further frames on
                # this connection are not expected.
                return
            response = await _process_frame(line.rstrip(b"\n"), ctx)
            writer.write(_frame(response))
            await writer.drain()
    except ConnectionResetError, BrokenPipeError:
        logger.debug("handle_connection peer-disconnect")
    finally:
        if subscriber is not None and isinstance(ctx.bus, EventBus):
            ctx.bus.unregister(connection_id)
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.wait_closed()


async def serve_unix(
    socket_path: str,
    ctx: MethodContext,
    *,
    expected_uid: int | None = None,
) -> asyncio.Server:
    """Start a Unix-domain JSON-RPC server bound to *socket_path*.

    Args:
        socket_path: Filesystem path for the UDS bind address.
        ctx: Server context shared across handlers.
        expected_uid: Daemon's own UID for peer-cred enforcement;
            pass ``None`` to disable (tests + uds-internal callers).

    Returns:
        The running :class:`asyncio.Server` instance. Caller owns
        ``server.close()`` + ``await server.wait_closed()``.
    """

    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, ctx, expected_uid=expected_uid)

    server = await asyncio.start_unix_server(_on_connect, path=socket_path)
    # ``start_unix_server`` creates the socket node under the prevailing
    # umask, which may leave it group/world-accessible. Tighten it to
    # owner-only before any peer connects so the filesystem node is
    # unreachable by other local users (the peer-cred check gates *who*
    # may issue RPCs; this gates *who can reach the node at all*). The
    # holding directory is hardened at its creation point in
    # :func:`eawf.runtime.daemon.runtime_dir.ensure_runtime_dir`, not here —
    # ``serve_unix`` does not own the socket's parent (tests + operators
    # may bind under a shared dir such as ``$TMPDIR``).
    if os.name != "nt":
        os.chmod(socket_path, SOCKET_FILE_MODE)
    logger.info(f"serve_unix bound socket={socket_path!r}")
    return server
