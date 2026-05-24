"""``eawf daemon`` — operator surface for the eawfd background process.

W01 ships five verbs:

- ``run --foreground`` — boot the daemon in the foreground (used by
  systemd / launchd / interactive testing).
- ``ping`` — issue ``daemon.ping`` against the local UDS.
- ``status`` — issue ``daemon.status`` against the local UDS.
- ``stop`` — issue ``daemon.shutdown`` against the local UDS.
- ``logs`` — print recent lines from the daemon's log file.

W03 adds the WAL admin verb:

- ``replay-wal --inspect`` — list poisoned WAL records (operator
  reads the local WAL directory; daemon need not be running).
- ``replay-wal --gc`` — drop ``.fsynced.json`` records older than
  the retention window (default 3600 s).

W04 adds the per-OS service-registration verbs (operator-facing, no
running daemon required):

- ``service-enable`` — render the per-OS unit template and ask the
  native supervisor (systemd / launchd / Windows SCM) to install +
  start the daemon.
- ``service-disable`` — stop + uninstall the unit. Idempotent on a
  never-installed state.
- ``service-status`` — query the supervisor for the current state
  (running / enabled / disabled / not-installed). Distinct from
  ``status`` above, which still issues the W01 ``daemon.status`` RPC
  against the running socket.

The CLI is dispatch only (rule 1); all socket framing + JSON-RPC
machinery lives under :mod:`eawf.runtime.daemon`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.runtime_dir import log_path, runtime_dir, socket_path
from eawf.runtime.daemon.spawn import DaemonSpawnTimeoutError, auto_spawn_daemon

logger = logging.getLogger(__name__)


daemon_app = typer.Typer(
    name="daemon",
    help="Manage the eawfd background daemon (run, ping, status, stop, logs).",
    no_args_is_help=True,
    add_completion=False,
)


async def _rpc_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Issue a single JSON-RPC call against the local daemon socket.

    Cold-spawns the daemon when no live socket is present, per the V1
    on-demand spawn contract. Silent unless ``EAWF_VERBOSE=1`` is set.

    Args:
        method: JSON-RPC method name.
        params: Method params object.

    Returns:
        The parsed response envelope (success or error).

    Raises:
        ConnectionRefusedError: When the daemon refuses the connection
            after a spawn attempt.
        DaemonSpawnTimeoutError: When the auto-spawn never produces a
            live socket within the timeout window.
    """
    sock_path = socket_path()
    if not sock_path.exists():
        auto_spawn_daemon(runtime_dir())
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
    from eawf.runtime.daemon.main import run as run_daemon

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
    except (ConnectionRefusedError, FileNotFoundError, DaemonSpawnTimeoutError) as exc:
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
    except (ConnectionRefusedError, FileNotFoundError, DaemonSpawnTimeoutError) as exc:
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
    except (ConnectionRefusedError, FileNotFoundError, DaemonSpawnTimeoutError) as exc:
        typer.echo(f"daemon not reachable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _emit_or_fail(
        response,
        "daemon shutting down at={shutdown_at} drained={drained}",
        flags,
    )


def _wal_dir() -> Path:
    """Return the daemon WAL directory (``<runtime_dir>/wal/``)."""
    return runtime_dir() / "wal"


@daemon_app.command("replay-wal")
def replay_wal_cmd(
    ctx: typer.Context,
    inspect: Annotated[
        bool,
        typer.Option(
            "--inspect",
            help="List poisoned WAL records (no daemon required; reads local WAL dir).",
        ),
    ] = False,
    gc: Annotated[
        bool,
        typer.Option(
            "--gc",
            help="Drop .fsynced.json records older than --max-age-seconds.",
        ),
    ] = False,
    max_age_seconds: Annotated[
        int,
        typer.Option(
            "--max-age-seconds",
            help="Retention window for .fsynced.json records (default 3600).",
        ),
    ] = 3600,
) -> None:
    """Inspect poisoned WAL records or GC the done window.

    Operator-facing surface. Reads the WAL directory directly so the
    verb works when the daemon is down (post-crash forensic flow). The
    two modes are mutually exclusive — pass exactly one of ``--inspect``
    or ``--gc``.

    Args:
        ctx: Typer context (global flags carried on ``ctx.obj``).
        inspect: List ``poisoned/*.poisoned.json`` records with their
            recorded ``poison_reason``.
        gc: Drop ``.fsynced.json`` records older than the retention
            window from the WAL directory.
        max_age_seconds: Retention threshold for ``--gc``. Negative or
            absurdly large values surface as an exit-code-2 usage error.
    """
    flags: GlobalFlags = ctx.obj
    if inspect == gc:
        typer.echo(
            "exactly one of --inspect / --gc must be set",
            err=True,
        )
        raise typer.Exit(code=2)
    if max_age_seconds < 0 or max_age_seconds > 30 * 24 * 3600:
        typer.echo(
            "--max-age-seconds must be between 0 and 2592000",
            err=True,
        )
        raise typer.Exit(code=2)
    wal_dir = _wal_dir()
    if inspect:
        _replay_wal_inspect(wal_dir, flags)
        return
    _replay_wal_gc(wal_dir, max_age_seconds, flags)


def _replay_wal_inspect(wal_dir: Path, flags: GlobalFlags) -> None:
    """Emit the list of poisoned WAL records under *wal_dir*."""
    from eawf.runtime.daemon.wal import list_poisoned, read_record

    paths = list_poisoned(wal_dir)
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            record = read_record(path)
        except (ValueError, OSError) as exc:
            rows.append(
                {
                    "path": str(path),
                    "record_id": path.stem.split(".")[0],
                    "poison_reason": f"unreadable: {exc}",
                    "written_at": None,
                }
            )
            continue
        rows.append(
            {
                "path": str(path),
                "record_id": record.record_id,
                "poison_reason": record.poison_reason,
                "written_at": record.written_at.isoformat(),
            }
        )
    if not rows:
        text = "no poisoned WAL records"
    else:
        text_lines = [f"poisoned WAL records: {len(rows)}"]
        for row in rows:
            text_lines.append(
                f"  record={row['record_id']} reason={row['poison_reason']!r} path={row['path']}"
            )
        text = "\n".join(text_lines)
    emit_json_or_text({"count": len(rows), "records": rows}, text, flags=flags)


def _replay_wal_gc(wal_dir: Path, max_age_seconds: int, flags: GlobalFlags) -> None:
    """GC aged ``.fsynced.json`` records under *wal_dir* and emit the report."""
    from eawf.runtime.daemon.wal import gc_done_records

    removed = gc_done_records(wal_dir, max_age_seconds=max_age_seconds)
    text = (
        f"gc removed {len(removed)} WAL record(s) "
        f"max_age_seconds={max_age_seconds} wal_dir={wal_dir}"
    )
    payload = {
        "removed_count": len(removed),
        "removed_paths": [str(p) for p in removed],
        "max_age_seconds": max_age_seconds,
        "wal_dir": str(wal_dir),
    }
    emit_json_or_text(payload, text, flags=flags)


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


@daemon_app.command("service-enable")
def service_enable_cmd(ctx: typer.Context) -> None:
    """Install + start the eawfd service via the native OS supervisor.

    Renders the per-OS template (systemd unit / launchd plist) or
    invokes the pywin32 service framework on Windows, asks the
    supervisor to start the unit, and waits up to 10 s for the
    daemon to publish its PID file.
    """
    from eawf.runtime.daemon.service_install import ServiceInstallError, enable_service

    flags: GlobalFlags = ctx.obj
    try:
        envelope = enable_service()
    except ServiceInstallError as exc:
        typer.echo(f"service enable failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = {
        "event_type": envelope.event_type,
        "platform": envelope.platform,
        "unit": envelope.unit,
        "pid": envelope.pid,
    }
    text = (
        f"daemon service enabled platform={envelope.platform} "
        f"unit={envelope.unit} pid={envelope.pid}"
    )
    emit_json_or_text(payload, text, flags=flags)


@daemon_app.command("service-disable")
def service_disable_cmd(ctx: typer.Context) -> None:
    """Stop + uninstall the eawfd service. Idempotent."""
    from eawf.runtime.daemon.service_install import ServiceInstallError, disable_service

    flags: GlobalFlags = ctx.obj
    try:
        envelope = disable_service()
    except ServiceInstallError as exc:
        typer.echo(f"service disable failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = {
        "event_type": envelope.event_type,
        "platform": envelope.platform,
        "unit": envelope.unit,
    }
    text = f"daemon service disabled platform={envelope.platform} unit={envelope.unit}"
    emit_json_or_text(payload, text, flags=flags)


@daemon_app.command("service-status")
def service_status_cmd(ctx: typer.Context) -> None:
    """Report the supervisor-level service state (no daemon RPC).

    Distinct from ``eawf daemon status``, which issues the W01
    ``daemon.status`` RPC against the running socket. This verb asks
    the native supervisor for its view of the unit; ``running`` here
    requires both registration AND an active PID.
    """
    from eawf.runtime.daemon.service_install import service_status

    flags: GlobalFlags = ctx.obj
    status = service_status()
    payload = {"platform": sys.platform, "status": status.value}
    text = f"daemon service platform={sys.platform} status={status.value}"
    emit_json_or_text(payload, text, flags=flags)
