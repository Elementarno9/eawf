"""CLI-side JSON-RPC client for the eawfd daemon.

Combines :func:`eawf.runtime.daemon.spawn.auto_spawn_daemon` (cold-spawn the
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
import re
import socket
import sys
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any

import orjson

from eawf import __version__
from eawf.kernel.state.mutations import Mutation
from eawf.runtime.daemon.runtime_dir import runtime_dir as default_runtime_dir
from eawf.runtime.daemon.spawn import auto_spawn_daemon, daemon_pid_if_ready
from eawf.surfaces.cli.errors import DaemonVersionMismatch

logger = logging.getLogger(__name__)


#: Wire timeout for a single JSON-RPC request/response on the
#: synchronous CLI client. Subscribers use the asyncio surface
#: directly; this only bounds the round-trip RPC.
DEFAULT_CALL_TIMEOUT_SECONDS: float = 30.0


#: The public-release subset of PEP 440 that eawf versions are cut in:
#: ``0.6.8``, ``0.7.0.dev5``, ``0.7.0rc1``, ``0.7.0rc1.dev2``.
_RELEASE_VERSION = re.compile(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?")
_PRE_RELEASE_RANK = {"a": 0, "b": 1, "rc": 2}


def _release_key(version: str) -> tuple[tuple[int, ...], tuple[int, int], tuple[int, int]] | None:
    """Return the PEP 440 sort key of *version*, or ``None`` outside the grammar."""
    match = _RELEASE_VERSION.fullmatch(version)
    if match is None:
        return None
    release_text, pre_phase, pre_number, dev_number = match.groups()
    release = tuple(int(part) for part in release_text.split("."))
    release += (0,) * (4 - len(release))
    if pre_phase is not None:
        pre = (_PRE_RELEASE_RANK[pre_phase], int(pre_number))
    elif dev_number is not None:
        # A bare ``X.Y.Z.devN`` precedes every pre-release of ``X.Y.Z``.
        pre = (-1, 0)
    else:
        pre = (len(_PRE_RELEASE_RANK), 0)
    dev = (0, int(dev_number)) if dev_number is not None else (1, 0)
    return release, pre, dev


def release_order(left: str, right: str) -> int | None:
    """Order two eawf release versions.

    Args:
        left: Version string, e.g. the running daemon's.
        right: Version string, e.g. this CLI's.

    Returns:
        ``-1`` when *left* is older, ``0`` when equal, ``1`` when newer, and
        ``None`` when either is outside the release grammar and so cannot
        be ordered.
    """
    left_key = _release_key(left)
    right_key = _release_key(right)
    if left_key is None or right_key is None:
        return None
    return (left_key > right_key) - (left_key < right_key)


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


class DaemonNotRunningError(ConnectionError):
    """Raised on enter when no daemon answers and the client may not start one.

    A :class:`ConnectionError`, so every caller that already maps an
    ``OSError`` to "daemon unavailable" reports it the same way.
    """


class DaemonClient:
    """Synchronous JSON-RPC client over the daemon's UDS / named pipe.

    Usage::

        with DaemonClient() as client:
            info = client.call("daemon.ping")

    The context-manager spawns the daemon on enter (silent unless
    ``EAWF_VERBOSE=1``) and tears down the connection cleanly on exit.
    A read verb passes ``spawn=False``: inspecting the surface must not be
    what starts a daemon, so enter then only attaches to a running one and
    raises :class:`DaemonNotRunningError` when none answers.
    The connection is a single socket; multiple ``client.call``
    invocations multiplex over it sequentially (one in-flight request
    at a time per the JSON-RPC 2.0 ordering contract).
    """

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        spawn: bool = True,
    ) -> None:
        if runtime_dir is None:
            runtime_dir = default_runtime_dir()
        self._runtime_dir = runtime_dir
        self._call_timeout_seconds = float(call_timeout_seconds)
        self._spawn = spawn
        self._sock: socket.socket | None = None
        self._reader: Any = None  # makefile("rb") for newline-framed reads
        self._pid: int = 0
        # Windows named-pipe path: request/response is connectionless (each
        # ``call`` opens + round-trips + closes a pipe handle via
        # ``pipe_client_call``), so there is no persistent socket. The pipe
        # name is set on enter and signals ``call`` to route over the pipe.
        self._pipe_name: str | None = None
        self._entered: bool = False

    @property
    def pid(self) -> int:
        """Return the PID of the daemon this client is attached to.

        Returns:
            Integer PID set by :func:`auto_spawn_daemon` during enter.
            Zero before the context manager has been entered.
        """
        return self._pid

    def __enter__(self) -> DaemonClient:
        if self._spawn:
            self._pid = auto_spawn_daemon(self._runtime_dir)
        else:
            pid = daemon_pid_if_ready(self._runtime_dir)
            if pid is None:
                raise DaemonNotRunningError(
                    f"no eawfd daemon is running under {self._runtime_dir.name!r}"
                )
            self._pid = pid
        if sys.platform == "win32":
            # Windows named-pipe transport: connectionless request/response.
            # Each ``call`` opens its own pipe handle through
            # ``pipe_client_call`` (the per-user pipe is a singleton
            # listener, so a long-held client handle would block other
            # callers). Resolve the per-user pipe name once here so the
            # client carries it for every call.
            from eawf.runtime.daemon.windows_pipe import default_pipe_name

            self._pipe_name = default_pipe_name()
            self._entered = True
            logger.debug(
                f"__enter__ pipe pid={self._pid} pipe={self._pipe_name!r} "
                f"runtime={self._runtime_dir.name!r}"
            )
            self._ensure_current_daemon()
            return self
        self._connect_socket()
        self._ensure_current_daemon()
        return self

    def _connect_socket(self) -> None:
        """Open the POSIX UDS connection to the daemon at ``self._pid``."""
        sock_path = self._runtime_dir / "eawfd.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._call_timeout_seconds)
        sock.connect(str(sock_path))
        self._sock = sock
        self._reader = sock.makefile("rb")
        self._entered = True
        logger.debug(f"__enter__ connected pid={self._pid} runtime={self._runtime_dir.name!r}")

    def _ensure_current_daemon(self) -> None:
        """Replace a daemon from an older release before any call reaches it.

        A daemon outlives the install that started it, so after an upgrade
        the CLI finds the previous release still serving: it lacks the
        current methods and validates payloads against the old schemas.
        An older daemon is restarted through the same path as
        ``eawf daemon restart``; a newer or unorderable one is refused, as
        replacing it would downgrade whoever started it.

        Raises:
            DaemonVersionMismatch: When the daemon is newer, its version
                cannot be ordered, the restart fails, or the restarted
                daemon still reports another release.
        """
        from eawf.runtime.daemon import lifecycle

        daemon_version = str(self.call("daemon.ping").get("version", ""))
        if daemon_version == __version__:
            return
        order = release_order(daemon_version, __version__)
        if order is None or order > 0:
            self.__exit__(None, None, None)
            raise DaemonVersionMismatch(
                f"the running daemon is eawf {daemon_version}, not this eawf {__version__}; "
                "run `eawf daemon restart` to replace it with this release"
            )
        previous_pid = self._pid
        self.__exit__(None, None, None)
        try:
            result = lifecycle.restart_daemon(runtime_dir=self._runtime_dir)
        except lifecycle.DaemonLifecycleError as exc:
            raise DaemonVersionMismatch(
                f"the running daemon is eawf {daemon_version}, older than this eawf "
                f"{__version__}, and restarting it failed: {exc}; "
                "run `eawf daemon restart`"
            ) from exc
        logger.info(
            f"daemon restarted stale previous_pid={previous_pid} pid={result.pid} "
            f"daemon_version={daemon_version!r} client_version={__version__!r}"
        )
        self._pid = result.pid
        if sys.platform == "win32":
            from eawf.runtime.daemon.windows_pipe import default_pipe_name

            self._pipe_name = default_pipe_name()
            self._entered = True
        else:
            self._connect_socket()
        restarted_version = str(self.call("daemon.ping").get("version", ""))
        if restarted_version != __version__:
            self.__exit__(None, None, None)
            raise DaemonVersionMismatch(
                f"the restarted daemon is eawf {restarted_version}, not this eawf "
                f"{__version__}; its supervisor still starts the old release; "
                "reinstall the daemon service, then run `eawf daemon restart`"
            )

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
        self._pipe_name = None
        self._entered = False

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
        if not self._entered:
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
        if self._pipe_name is not None:
            response_bytes = self._call_over_pipe(line, method=method)
        else:
            response_bytes = self._call_over_socket(line, method=method)
        return self._parse_response(response_bytes)

    def _call_over_socket(self, line: bytes, *, method: str) -> bytes:
        """Round-trip one frame over the POSIX UDS (newline-framed).

        Args:
            line: The newline-terminated request frame.
            method: Method name, for error messages.

        Returns:
            The response frame bytes (one newline-terminated line).

        Raises:
            RuntimeError: When the daemon closed the connection mid-call.
            TimeoutError: When the response did not arrive in time.
        """
        if self._sock is None or self._reader is None:
            raise RuntimeError("daemon client not connected; use as a context manager")
        deadline = time.monotonic() + self._call_timeout_seconds
        self._sock.sendall(line)
        response_line: bytes = self._reader.readline()
        if not response_line:
            raise RuntimeError(f"daemon closed connection during call method={method!r}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"daemon call exceeded timeout method={method!r}")
        return response_line

    def _call_over_pipe(self, line: bytes, *, method: str) -> bytes:
        """Round-trip one frame over the Windows named pipe.

        Connectionless: opens a fresh pipe handle, writes the request, and
        reads the full response (reassembling any frame larger than the
        pipe buffer through the transport's ``ERROR_MORE_DATA`` loop).

        Args:
            line: The newline-terminated request frame.
            method: Method name, for error messages.

        Returns:
            The response frame bytes.

        Raises:
            RuntimeError: When the pipe round-trip fails.
        """
        from eawf.runtime.daemon.windows_pipe import pipe_client_call

        assert self._pipe_name is not None
        wait_ms = int(self._call_timeout_seconds * 1000)
        try:
            response: bytes = pipe_client_call(self._pipe_name, line, wait_ms=wait_ms)
        except OSError as exc:
            raise RuntimeError(f"daemon pipe round-trip failed method={method!r}: {exc}") from exc
        return response

    @staticmethod
    def _parse_response(response_bytes: bytes) -> dict[str, Any]:
        """Parse a JSON-RPC response frame into its ``result`` dict.

        Shared by the socket + pipe transports so the wire-contract checks
        (object response, error envelope -> :class:`DaemonRpcError`, object
        result) live in one place.

        Args:
            response_bytes: One JSON-RPC response frame (trailing newline
                tolerated).

        Returns:
            The ``result`` object of a success envelope.

        Raises:
            DaemonRpcError: When the envelope carries a JSON-RPC error.
            RuntimeError: When the frame is not a well-formed success
                envelope with an object ``result``.
        """
        response = orjson.loads(response_bytes.rstrip(b"\n"))
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
        Used by :mod:`eawf.surfaces.cli._mutation` for the W09 proxy callsite
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
            :class:`eawf.runtime.daemon.methods.state.MutateResult` — the event
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
        """Proxy a layered-config write through ``config.set_layer_value``.

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
            :class:`eawf.runtime.daemon.methods.config.SetLayerValueResult`.

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

    def config_unset_layer_value(
        self,
        *,
        layer: str,
        key_path: list[str],
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a layered-config removal through ``config.unset_layer_value``."""
        params: dict[str, Any] = {
            "layer": layer,
            "key_path": list(key_path),
        }
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        if repo_root is not None:
            params["repo_root"] = repo_root
        return self.call("config.unset_layer_value", params)

    def config_set_layer_values(
        self,
        *,
        layer: str,
        writes: list[dict[str, Any]],
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        """Proxy several leaves of one layer through ``config.set_layer_values``.

        Args:
            layer: Canonical writable-layer label.
            writes: One ``{"key_path": [...], "value": ...}`` or
                ``{"key_path": [...], "unset": True}`` per leaf, written together.
            repo_root: Optional absolute path of the repo whose layer is written.

        Returns:
            Dict matching
            :class:`eawf.runtime.daemon.methods.config.SetLayerValuesResult`.
        """
        params: dict[str, Any] = {"layer": layer, "writes": list(writes)}
        if repo_root is not None:
            params["repo_root"] = repo_root
        return self.call("config.set_layer_values", params)

    def registry_update(
        self,
        *,
        operation: str,
        repo_id: str,
        fields: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        registry_path: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a registry mutation through ``registry.update``.

        Args:
            operation: One of ``add`` / ``remove`` / ``rename``.
            repo_id: Project-code-shape identifier the op targets.
            fields: Operation-specific extras (e.g. ``path`` + ``title``
                for ``add``; ``new_code`` for ``rename``).
            idempotency_key: Optional retry key.
            registry_path: The registry file the daemon mutates. Sent on
                the wire because the daemon is a separate process: an
                override held only in the caller's environment never
                reaches it, and the daemon then writes its default file.

        Returns:
            Dict matching :class:`eawf.runtime.daemon.methods.registry.UpdateResult`.
        """
        params: dict[str, Any] = {
            "operation": operation,
            "repo_id": repo_id,
            "fields": dict(fields) if fields else {},
        }
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        if registry_path is not None:
            params["registry_path"] = registry_path
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
        """Proxy a ``spec.validate`` call through the daemon."""
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
        """Proxy a ``spec.promote`` call through the daemon."""
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
        """Proxy a ``spec.archive`` call through the daemon."""
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
    "DaemonNotRunningError",
    "DaemonRpcError",
]
