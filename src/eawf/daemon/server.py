"""asyncio JSON-RPC 2.0 server for eawfd.

Each accepted connection runs in its own asyncio task that reads
newline-delimited JSON frames, dispatches the method through
:mod:`eawf.daemon.methods`, and writes a response frame back. The frame
format is one JSON object per ``\\n``-terminated line per C02 §5.2.1.

The server is process-internal — the entry point in :mod:`eawf.daemon.main`
binds the Unix domain socket and wires the :class:`MethodContext`.
Tests that exercise the framing layer drive
:func:`handle_connection` directly with paired ``asyncio.StreamReader`` /
``asyncio.StreamWriter`` halves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any, cast

import orjson

# Import to ensure handlers register before dispatch runs.
import eawf.daemon.methods.daemon  # noqa: F401
from eawf.daemon.auth import UnauthorizedError, verify_peer_credential
from eawf.daemon.methods import (
    MethodContext,
    MethodNotFoundError,
    dispatch,
)

logger = logging.getLogger(__name__)


# Error codes per C02 §5.2.2.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32000
DAEMON_SHUTTING_DOWN = -32009


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
        code: Numeric error code per C02 §5.2.2.
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


async def _process_frame(line: bytes, ctx: MethodContext) -> dict[str, Any]:
    """Decode + dispatch a single JSON-RPC frame.

    Args:
        line: Raw bytes for one frame (without trailing newline).
        ctx: Server context shared across handlers.

    Returns:
        JSON-RPC response envelope (success or error).
    """
    try:
        payload = orjson.loads(line)
    except orjson.JSONDecodeError:
        return _error(None, PARSE_ERROR, "parse error")
    if not isinstance(payload, dict):
        return _error(None, INVALID_REQUEST, "frame is not a JSON object")
    req_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0":
        return _error(req_id, INVALID_REQUEST, "missing jsonrpc=2.0")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return _error(req_id, INVALID_REQUEST, "missing method")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return _error(req_id, INVALID_PARAMS, "params must be an object")
    try:
        result = await dispatch(method, ctx, params)
    except MethodNotFoundError:
        return _error(req_id, METHOD_NOT_FOUND, f"method not found: {method!r}")
    except ValueError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
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
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            response = await _process_frame(line.rstrip(b"\n"), ctx)
            writer.write(_frame(response))
            await writer.drain()
    except ConnectionResetError, BrokenPipeError:
        logger.debug("handle_connection peer-disconnect")
    finally:
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
    logger.info(f"serve_unix bound socket={socket_path!r}")
    return server
