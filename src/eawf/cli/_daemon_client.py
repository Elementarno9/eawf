"""CLI-side JSON-RPC client for the eawfd daemon.

Combines :func:`eawf.daemon.spawn.auto_spawn_daemon` (cold-spawn the
daemon if needed) with a thin newline-delimited JSON-RPC transport
over the UDS / named pipe surface. Used by W09 to wire ``state.mutate``
through the daemon and by every later wave that needs typed
JSON-RPC. W08 ships only the transport — wiring lives downstream.

Per D15 the cold-spawn is silent: stdout/stderr stay clean unless the
operator opts in with ``EAWF_VERBOSE=1``. The client itself never
prints; only the spawn helper does.

The client is intentionally synchronous — every CLI call site is
single-shot ``open → send → receive → close`` and there is no
benefit to forcing an asyncio event loop on the CLI boundary.
Subscribers (W06's streaming surface) use the asyncio listener
directly; the synchronous client targets request/response only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any

import orjson

from eawf.daemon.runtime_dir import runtime_dir as default_runtime_dir
from eawf.daemon.spawn import auto_spawn_daemon

logger = logging.getLogger(__name__)


#: Wire timeout for a single JSON-RPC request/response on the
#: synchronous CLI client. Subscribers use the asyncio surface
#: directly; this only bounds the round-trip RPC.
DEFAULT_CALL_TIMEOUT_SECONDS: float = 30.0


class DaemonRpcError(RuntimeError):
    """Raised when the daemon returns a JSON-RPC error envelope.

    Attributes:
        code: JSON-RPC numeric error code (e.g. ``-32601`` for
            method-not-found, ``-32000`` for unauthorized).
        message: Short human description from the error envelope.
        data: Optional structured payload — used by the unauthorized
            envelope to carry forensics + by validation failures to
            carry the field path.
    """

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"daemon rpc error code={code} message={message!r}")


class DaemonClient:
    """Synchronous JSON-RPC client over the daemon's UDS / named pipe.

    Usage::

        with DaemonClient() as client:
            info = client.call("daemon.ping")

    The context-manager spawns the daemon on enter (silent per D15)
    and tears down the connection cleanly on exit. The connection is
    a single socket; multiple ``client.call`` invocations multiplex
    over it sequentially (one in-flight request at a time per the
    JSON-RPC 2.0 ordering contract).
    """

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if runtime_dir is None:
            runtime_dir = default_runtime_dir()
        self._runtime_dir = runtime_dir
        self._call_timeout_seconds = float(call_timeout_seconds)
        self._sock: socket.socket | None = None
        self._reader: Any = None  # makefile("rb") for newline-framed reads
        self._pid: int = 0

    @property
    def pid(self) -> int:
        """Return the PID of the daemon this client is attached to.

        Returns:
            Integer PID set by :func:`auto_spawn_daemon` during enter.
            Zero before the context manager has been entered.
        """
        return self._pid

    def __enter__(self) -> DaemonClient:
        self._pid = auto_spawn_daemon(self._runtime_dir)
        if sys.platform == "win32":
            # Windows named-pipe transport — the synchronous helper
            # is owned by the windows_pipe bridge in a later wave.
            # W08 ships the POSIX path + the spawn contract; W09
            # wires the windows-pipe client side.
            raise NotImplementedError("DaemonClient windows-pipe transport pending W09 wiring")
        sock_path = self._runtime_dir / "eawfd.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._call_timeout_seconds)
        sock.connect(str(sock_path))
        self._sock = sock
        self._reader = sock.makefile("rb")
        logger.debug(f"__enter__ connected pid={self._pid} runtime={self._runtime_dir.name!r}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._reader is not None:
            with contextlib.suppress(OSError):
                self._reader.close()
            self._reader = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result dict.

        Args:
            method: Dotted JSON-RPC method name
                (e.g. ``daemon.ping``).
            params: Optional params object; ``None`` is normalised
                to ``{}`` on the wire.
            idempotency_key: Optional idempotency key carried as
                ``params["idempotency_key"]`` for the W09 mutator
                surface. Ignored when *params* already carries one.

        Returns:
            The ``result`` field of the JSON-RPC success envelope.

        Raises:
            DaemonRpcError: When the daemon responded with a JSON-RPC
                error envelope.
            RuntimeError: When the client was never entered or the
                socket is closed.
            TimeoutError: When the daemon did not respond within
                ``call_timeout_seconds``.
        """
        if self._sock is None or self._reader is None:
            raise RuntimeError("DaemonClient is not connected; use as a context manager")
        payload_params = dict(params) if params is not None else {}
        if idempotency_key is not None and "idempotency_key" not in payload_params:
            payload_params["idempotency_key"] = idempotency_key
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": payload_params,
        }
        line = orjson.dumps(request) + b"\n"
        deadline = time.monotonic() + self._call_timeout_seconds
        self._sock.sendall(line)
        response_line = self._reader.readline()
        if not response_line:
            raise RuntimeError(f"daemon closed connection during call method={method!r}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"daemon call exceeded timeout method={method!r}")
        response = orjson.loads(response_line)
        if not isinstance(response, dict):
            raise RuntimeError(f"daemon returned non-object response: {response!r}")
        if "error" in response and response["error"] is not None:
            error = response["error"]
            raise DaemonRpcError(
                code=int(error.get("code", -32000)),
                message=str(error.get("message", "")),
                data=error.get("data"),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            # The daemon's success contract is a result OBJECT; any
            # other shape is a wire-level violation.
            raise RuntimeError(f"daemon returned non-object result: {result!r}")
        return result


def _is_verbose() -> bool:
    """Return True when ``EAWF_VERBOSE=1`` is set.

    Mirrors :func:`eawf.daemon.spawn._is_verbose` but lives in the
    CLI module so callers do not reach across packages for a single
    environment lookup.
    """
    return os.environ.get("EAWF_VERBOSE", "") == "1"


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DaemonClient",
    "DaemonRpcError",
]
