"""``eawf daemon`` — operator surface for the eawfd background process.

W01 ships five verbs:

- ``run --foreground`` — boot the daemon in the foreground (used by
  systemd / launchd / interactive testing).
- ``ping`` — issue ``daemon.ping`` against the local UDS.
- ``status`` — issue ``daemon.status`` against the local UDS.
- ``stop`` — issue ``daemon.shutdown`` against the local UDS.
- ``logs`` — print recent lines from the daemon's log file.

The CLI is dispatch only (rule 1); all socket framing + JSON-RPC
machinery lives under :mod:`eawf.daemon`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import uuid
from typing import Annotated, Any

import orjson
import typer

from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.main import run as run_daemon
from eawf.daemon.runtime_dir import log_path, socket_path

logger = logging.getLogger(__name__)


daemon_app = typer.Typer(
    name="daemon",
    help="Manage the eawfd background daemon (run, ping, status, stop, logs).",
    no_args_is_help=True,
    add_completion=False,
)


async def _rpc_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Issue a single JSON-RPC call against the local daemon socket.

    Args:
        method: JSON-RPC method name.
        params: Method params object.

    Returns:
        The parsed response envelope (success or error).

    Raises:
        ConnectionRefusedError: When the socket is missing or the
            daemon refuses the connection.
    """
    sock_path = socket_path()
    if not sock_path.exists():
        raise ConnectionRefusedError(f"daemon socket missing: {sock_path}")
    reader, writer = await asyncio.open_unix_connection(path=str(sock_path))
    try:
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
            "protocol_version": PROTOCOL_VERSION,
        }
        writer.write(orjson.dumps(request) + b"\n")
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise ConnectionResetError("daemon closed connection without response")
        payload = orjson.loads(line)
        if not isinstance(payload, dict):
            raise RuntimeError(f"daemon returned non-object frame: {payload!r}")
        return payload
    finally:
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
            await writer.wait_closed()


def _run_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Synchronous wrapper around :func:`_rpc_call` for Typer handlers.

    Args:
        method: JSON-RPC method name.
        params: Method params object.

    Returns:
        The parsed response envelope.
    """
    return asyncio.run(_rpc_call(method, params))


@daemon_app.command("run")
def run_cmd(
    ctx: typer.Context,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground",
            help="Run the daemon attached to the current TTY (logs to stderr).",
        ),
    ] = False,
) -> None:
    """Boot the daemon process.

    Args:
        ctx: Typer context (global flags carried on ``ctx.obj``).
        foreground: When True, stay in the foreground and log to stderr;
            otherwise log to the runtime log file. W04 wires the detach
            + service-style boot path.
    """
    if sys.platform.startswith("win"):
        typer.echo("eawf daemon run is not supported on windows yet (W02)", err=True)
        raise typer.Exit(code=2)
    flags: GlobalFlags = ctx.obj
    if flags.json_output:
        # The daemon is a long-running process; JSON envelope output
        # only makes sense for the short-lived sibling verbs.
        typer.echo(
            "--json is not meaningful for `eawf daemon run`; "
            "use `eawf daemon status --json` after start instead",
            err=True,
        )
        raise typer.Exit(code=2)
    rc = run_daemon(foreground=foreground)
    raise typer.Exit(code=rc)


def _emit_or_fail(
    response: dict[str, Any],
    text_template: str,
    flags: GlobalFlags,
) -> None:
    """Format a JSON-RPC response for the operator or exit non-zero.

    Args:
        response: Parsed response envelope.
        text_template: Format string applied to ``response['result']``
            when ``--json`` is off. The template receives the result
            dict via ``**`` expansion.
        flags: Resolved global CLI flags.
    """
    if "error" in response:
        err = response["error"]
        typer.echo(
            f"daemon error: code={err.get('code')} message={err.get('message')!r}",
            err=True,
        )
        raise typer.Exit(code=1)
    result = response.get("result", {})
    emit_json_or_text(dict(result), text_template.format(**result), flags=flags)


@daemon_app.command("ping")
def ping_cmd(ctx: typer.Context) -> None:
    """Probe daemon liveness and report version + PID."""
    flags: GlobalFlags = ctx.obj
    try:
        response = _run_rpc("daemon.ping", {})
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        typer.echo(f"daemon not reachable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _emit_or_fail(
        response,
        "daemon ok pid={pid} version={version} uptime={uptime_seconds:.1f}s",
        flags,
    )


@daemon_app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Print operational counters from the running daemon."""
    flags: GlobalFlags = ctx.obj
    try:
        response = _run_rpc("daemon.status", {})
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        typer.echo(f"daemon not reachable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _emit_or_fail(
        response,
        (
            "daemon pid={pid} version={version} uptime={uptime_seconds:.1f}s "
            "subs={active_subscriptions} in_flight={in_flight_mutations}"
        ),
        flags,
    )


@daemon_app.command("stop")
def stop_cmd(
    ctx: typer.Context,
    no_drain: Annotated[
        bool,
        typer.Option(
            "--no-drain",
            help="Exit immediately without waiting for in-flight mutations.",
        ),
    ] = False,
    timeout: Annotated[
        int,
        typer.Option("--timeout", help="Drain window in seconds (1-600)."),
    ] = 30,
) -> None:
    """Request graceful daemon shutdown."""
    flags: GlobalFlags = ctx.obj
    params: dict[str, Any] = {"drain": not no_drain, "timeout_seconds": timeout}
    try:
        response = _run_rpc("daemon.shutdown", params)
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        typer.echo(f"daemon not reachable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _emit_or_fail(
        response,
        "daemon shutting down at={shutdown_at} drained={drained}",
        flags,
    )


@daemon_app.command("logs")
def logs_cmd(
    ctx: typer.Context,
    tail: Annotated[
        int,
        typer.Option("--tail", help="Number of trailing lines to print (1-10000)."),
    ] = 200,
) -> None:
    """Print the trailing window of the daemon log file."""
    flags: GlobalFlags = ctx.obj
    if tail < 1 or tail > 10_000:
        typer.echo("--tail must be between 1 and 10000", err=True)
        raise typer.Exit(code=2)
    log_file = log_path()
    if not log_file.exists():
        typer.echo(f"no daemon log at {log_file}", err=True)
        raise typer.Exit(code=1)
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[-tail:]
    text = "\n".join(selected)
    emit_json_or_text({"path": str(log_file), "lines": selected}, text, flags=flags)
