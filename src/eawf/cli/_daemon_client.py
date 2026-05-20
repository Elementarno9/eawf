"""CLI-side JSON-RPC client for the eawfd daemon.

Combines :func:`eawf.daemon.spawn.auto_spawn_daemon` (cold-spawn the
daemon if needed) with a thin newline-delimited JSON-RPC transport
over the UDS / named pipe surface. Used by W09 to wire ``state.mutate``
through the daemon and by every later wave that needs typed
JSON-RPC. W08 ships only the transport — wiring lives downstream.

The cold-spawn is silent: stdout/stderr stay clean unless the operator
opts in with ``EAWF_VERBOSE=1``. The client itself never prints; only
the spawn helper does.

The client is intentionally synchronous — every CLI call site is
single-shot ``open → send → receive → close`` and there is no
benefit to forcing an asyncio event loop on the CLI boundary.
Subscribers (W06's streaming surface) use the asyncio listener
directly; the synchronous client targets request/response only.
"""

from __future__ import annotations

import contextlib
import logging
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
from eawf.state.mutations import Mutation

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

    The context-manager spawns the daemon on enter (silent unless
    ``EAWF_VERBOSE=1``) and tears down the connection cleanly on exit.
    The connection is a single socket; multiple ``client.call``
    invocations multiplex over it sequentially (one in-flight request
    at a time per the JSON-RPC 2.0 ordering contract).
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
            raise NotImplementedError("windows-pipe transport pending W09 wiring")
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
            raise RuntimeError("daemon client not connected; use as a context manager")
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

    def state_mutate(
        self,
        mutation: Mutation,
        *,
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a :class:`Mutation` through the daemon's ``state.mutate`` RPC.

        Thin convenience wrapper around :meth:`call` — serialises the
        mutation to a JSON-mode dict, forwards the optional idempotency
        key + repo anchor alongside it, and unwraps the result dict.
        Used by :mod:`eawf.cli._mutation` for the W09 proxy callsite
        rewire.

        Args:
            mutation: Typed mutation to dispatch.
            idempotency_key: Optional caller-supplied retry key. Shadows
                :attr:`Mutation.idempotency_key` when both are set.
            repo_root: Optional absolute path of the repo whose
                ``state.json`` the mutation targets. The daemon is one
                per user — passing the caller's repo root keeps a
                cross-repo invocation from being mis-routed against the
                daemon's boot-time cwd anchor. Omitting falls back to
                the daemon's boot-time ``state_path`` with a one-shot
                ``daemon_anchor_fallback`` warning logged on the daemon
                side.

        Returns:
            Dict matching
            :class:`eawf.daemon.methods.state.MutateResult` — the event
            envelope plus ``before_version`` / ``after_version`` digests
            and the ``idempotent_replay`` flag.

        Raises:
            DaemonRpcError: When the daemon returns a JSON-RPC error
                envelope (e.g. ``-32002 validation_failed``).
            RuntimeError: When the client was never entered or the
                socket is closed.
        """
        params: dict[str, Any] = {"mutation": mutation.model_dump(mode="json")}
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        elif mutation.idempotency_key is not None:
            params["idempotency_key"] = mutation.idempotency_key
        if repo_root is not None:
            params["repo_root"] = repo_root
        return self.call("state.mutate", params)

    def config_set_layer_value(
        self,
        *,
        layer: str,
        key_path: list[str],
        value: Any,
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a layered-config write through ``config.set_layer_value`` (W10).

        Args:
            layer: Canonical writable-layer label.
            key_path: Dotted-key as a list of segments.
            value: Typed value to write.
            idempotency_key: Optional retry key.
            repo_root: Optional absolute path of the repo whose layered
                config YAML the write targets. Required when the daemon
                is one-per-user but the caller is one of many repos —
                without it the daemon resolves the layer against its
                own boot-time ``state_path``, which can be a different
                repo entirely. Omitting falls back to that boot-time
                anchor with a one-shot ``daemon_anchor_fallback``
                warning on the daemon side.

        Returns:
            Dict matching
            :class:`eawf.daemon.methods.config.SetLayerValueResult`.

        Raises:
            DaemonRpcError: When the daemon returns a JSON-RPC error
                envelope (e.g. ``-32602 invalid_params``).
        """
        params: dict[str, Any] = {
            "layer": layer,
            "key_path": list(key_path),
            "value": value,
        }
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        if repo_root is not None:
            params["repo_root"] = repo_root
        return self.call("config.set_layer_value", params)

    def registry_update(
        self,
        *,
        operation: str,
        repo_id: str,
        fields: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a registry mutation through ``registry.update`` (W10).

        Args:
            operation: One of ``add`` / ``remove`` / ``rename``.
            repo_id: Project-code-shape identifier the op targets.
            fields: Operation-specific extras (e.g. ``path`` + ``title``
                for ``add``; ``new_code`` for ``rename``).
            idempotency_key: Optional retry key.

        Returns:
            Dict matching :class:`eawf.daemon.methods.registry.UpdateResult`.
        """
        params: dict[str, Any] = {
            "operation": operation,
            "repo_id": repo_id,
            "fields": dict(fields) if fields else {},
        }
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        return self.call("registry.update", params)

    def spec_init(
        self,
        *,
        scope_id: str,
        title: str,
        repo_code: str,
        repo_root: str | None = None,
        idempotency_key: str | None = None,
        cache_dir: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a ``spec.init`` call through the daemon."""
        params: dict[str, Any] = {
            "scope_id": scope_id,
            "title": title,
            "repo_code": repo_code,
        }
        if repo_root is not None:
            params["repo_root"] = repo_root
        if cache_dir is not None:
            params["cache_dir"] = cache_dir
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        return self.call("spec.init", params)

    def spec_validate(
        self,
        *,
        scope_id: str,
        repo_code: str,
        repo_root: str | None = None,
        cache_dir: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a ``spec.validate`` call through the daemon (P25-W03)."""
        params: dict[str, Any] = {
            "scope_id": scope_id,
            "repo_code": repo_code,
        }
        if repo_root is not None:
            params["repo_root"] = repo_root
        if cache_dir is not None:
            params["cache_dir"] = cache_dir
        return self.call("spec.validate", params)

    def spec_promote(
        self,
        *,
        scope_id: str,
        repo_code: str,
        target_status: str,
        repo_root: str | None = None,
        idempotency_key: str | None = None,
        cache_dir: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a ``spec.promote`` call through the daemon (P25-W03)."""
        params: dict[str, Any] = {
            "scope_id": scope_id,
            "repo_code": repo_code,
            "target_status": target_status,
        }
        if repo_root is not None:
            params["repo_root"] = repo_root
        if cache_dir is not None:
            params["cache_dir"] = cache_dir
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        return self.call("spec.promote", params)

    def spec_archive(
        self,
        *,
        scope_id: str,
        repo_code: str,
        repo_root: str | None = None,
        idempotency_key: str | None = None,
        cache_dir: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a ``spec.archive`` call through the daemon (P25-W03)."""
        params: dict[str, Any] = {
            "scope_id": scope_id,
            "repo_code": repo_code,
        }
        if repo_root is not None:
            params["repo_root"] = repo_root
        if cache_dir is not None:
            params["cache_dir"] = cache_dir
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        return self.call("spec.archive", params)


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DaemonClient",
    "DaemonRpcError",
]
