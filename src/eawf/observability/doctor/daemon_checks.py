"""Daemon transport and version checks for the doctor surface."""

from __future__ import annotations

import json
import logging
import socket
import sys
from typing import TYPE_CHECKING

from eawf.observability.doctor.models import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# Doctor is interactive and must never hang behind a dead runtime socket.
DAEMON_VERSION_PROBE_TIMEOUT_SECONDS = 0.2


def _daemon_ping_request() -> bytes:
    """Return a newline-framed read-only daemon ping request."""
    payload = {
        "jsonrpc": "2.0",
        "id": "doctor-version-probe",
        "method": "daemon.ping",
        "params": {},
    }
    return json.dumps(payload).encode("utf-8") + b"\n"


def _version_from_ping_response(response_bytes: bytes) -> str | None:
    """Extract a daemon version from one JSON-RPC response frame."""
    try:
        response = json.loads(response_bytes.rstrip(b"\n").decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    result = response.get("result") if isinstance(response, dict) else None
    version = result.get("version") if isinstance(result, dict) else None
    return version if isinstance(version, str) and version else None


def probe_running_daemon_version() -> str | None:
    """Read an existing daemon's version without starting or restarting it."""
    from eawf.runtime.daemon.runtime_dir import runtime_dir

    request = _daemon_ping_request()
    if sys.platform == "win32":
        from eawf.runtime.daemon.windows_pipe import default_pipe_name, pipe_client_call

        try:
            response = pipe_client_call(
                default_pipe_name(),
                request,
                wait_ms=max(1, int(DAEMON_VERSION_PROBE_TIMEOUT_SECONDS * 1000)),
            )
        except Exception:
            return None
        return _version_from_ping_response(response)

    sock_path = runtime_dir() / "eawfd.sock"
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe_socket:
            probe_socket.settimeout(DAEMON_VERSION_PROBE_TIMEOUT_SECONDS)
            probe_socket.connect(str(sock_path))
            probe_socket.sendall(request)
            reader = probe_socket.makefile("rb")
            try:
                response = reader.readline()
            finally:
                reader.close()
    except OSError:
        return None
    return _version_from_ping_response(response) if response else None


def check_cli_daemon_version(
    *,
    probe_version: Callable[[], str | None],
) -> CheckResult:
    """Compare installed CLI and running daemon versions without spawning."""
    from eawf import __version__

    name = "cli_daemon_version"
    try:
        daemon_version = probe_version()
    except Exception as exc:
        return CheckResult(
            name=name,
            status="warn",
            detail=f"running daemon version probe failed: {exc}",
        )
    if daemon_version is None:
        return CheckResult(
            name=name,
            status="ok",
            detail=f"CLI {__version__}; no running daemon version available",
        )
    if daemon_version != __version__:
        return CheckResult(
            name=name,
            status="warn",
            detail=(
                f"version skew: CLI {__version__}, running daemon {daemon_version}; "
                "restart daemon after install"
            ),
        )
    return CheckResult(
        name=name,
        status="ok",
        detail=f"CLI and running daemon both {__version__}",
    )


__all__ = [
    "DAEMON_VERSION_PROBE_TIMEOUT_SECONDS",
    "check_cli_daemon_version",
    "probe_running_daemon_version",
]
