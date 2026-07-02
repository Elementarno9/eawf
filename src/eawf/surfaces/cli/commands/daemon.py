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

W04 (P30-I14) adds the one-shot reclaim verb (no running daemon
required; reads the local runtime dir + the resolved ``.ea/`` dir):

- ``reclaim`` — sweep the WAL once via the W01 GC helper (drops
  ``.fsynced.json`` records past the retention window) and trim
  ``state.json.bak.*`` backups beyond a kept-count so a long-lived
  repo cannot accumulate unbounded migration backups.

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

from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.runtime_dir import log_path, runtime_dir, socket_path
from eawf.runtime.daemon.spawn import DaemonSpawnTimeoutError, auto_spawn_daemon
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


daemon_app = typer.Typer(
    name="daemon",
    help="Manage the eawfd background daemon (run, ping, status, stop, logs, reclaim).",
    no_args_is_help=True,
    add_completion=False,
)

#: Default number of ``state.json.bak.*`` backups ``reclaim`` keeps. Mirrors
#: the keep-window convention of :meth:`eawf.platform.backup.store.SnapshotStore.prune`.
_DEFAULT_BACKUP_KEEP: int = 3


async def _rpc_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Issue a single JSON-RPC call against the local daemon socket (POSIX).

    Cold-spawns the daemon when no live socket is present, per the V1
    on-demand spawn contract. Silent unless ``EAWF_VERBOSE=1`` is set.
    Windows routes through :func:`_rpc_call_pipe` instead -- there is no
    UDS on Windows.

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


def _rpc_call_pipe(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Issue a single JSON-RPC call over the Windows named pipe.

    The synchronous counterpart of :func:`_rpc_call`: there is no UDS on
    Windows, so the daemon CLI verbs round-trip through the per-user named
    pipe via the shared :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`
    (which owns the ``ERROR_MORE_DATA`` reassembly + cold-spawn). The
    client raises :class:`DaemonRpcError` on an error envelope; this
    helper re-wraps it into the ``{"error": {...}}`` shape
    :func:`_emit_or_fail` expects so the two transports share one
    formatter.

    Args:
        method: JSON-RPC method name.
        params: Method params object.

    Returns:
        The parsed response envelope (``{"result": ...}`` or
        ``{"error": ...}``).
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient(runtime_dir=runtime_dir()) as client:
            result = client.call(method, params)
        return {"result": result}
    except DaemonRpcError as exc:
        return {"error": {"code": exc.code, "message": exc.message, "data": exc.data}}


def _run_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Synchronous wrapper around the per-platform RPC transport.

    Routes through the Windows named pipe (:func:`_rpc_call_pipe`) on
    win32 and the POSIX UDS (:func:`_rpc_call`) elsewhere, so the Typer
    handlers stay transport-agnostic.

    Args:
        method: JSON-RPC method name.
        params: Method params object.

    Returns:
        The parsed response envelope.
    """
    if sys.platform == "win32":
        return _rpc_call_pipe(method, params)
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
    from eawf.runtime.daemon.service_install import detect_supervised_agent

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
    # A loaded launchd/systemd agent already owns the daemon lifecycle;
    # booting a second one here would fork a rival that fights for the
    # singleton lock. Defer to the supervisor instead. The supervised
    # process itself execs ``eawfd`` directly (never `eawf daemon run`), so
    # this defer can never deadlock the supervisor's own start.
    report = detect_supervised_agent()
    if report.loaded:
        typer.echo(
            f"deferring to loaded {report.supervisor} agent {report.label!r}; "
            "not forking a rival daemon (stop it with "
            "`eawf daemon stop --evict-service` or remove it with "
            "`eawf daemon service-disable`)",
            err=True,
        )
        raise typer.Exit(code=0)
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


def _handle_supervised_agent_on_stop(*, evict_service: bool) -> None:
    """Detect a supervised eawfd agent and evict-or-warn before the stop RPC.

    A launchd LaunchAgent (macOS) or systemd user unit (Linux) restarts the
    daemon on exit, so a plain ``daemon.shutdown`` is undone the moment it
    lands. When ``--evict-service`` is set we boot the agent out FIRST -- so
    the eviction precedes the shutdown RPC and no KeepAlive / Restart can race
    the stop -- otherwise we warn loudly that the stop will be reversed. A
    daemon that is not under a loaded supervisor is a no-op.

    Args:
        evict_service: When True, boot the loaded agent out before the RPC;
            when False, emit a loud warning and leave the agent loaded.
    """
    from eawf.runtime.daemon.service_install import (
        detect_supervised_agent,
        evict_supervised_agent,
    )

    report = detect_supervised_agent()
    if not report.loaded:
        return
    if evict_service:
        target = evict_supervised_agent()
        typer.echo(
            f"evicted {report.supervisor} agent {target!r} before shutdown",
            err=True,
        )
        return
    typer.echo(
        f"WARNING: {report.supervisor} agent {report.label!r} is loaded and will "
        "restart the daemon after shutdown; pass --evict-service to boot it out "
        "for the stop window",
        err=True,
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
    evict_service: Annotated[
        bool,
        typer.Option(
            "--evict-service",
            help=(
                "Boot out a loaded launchd/systemd agent before the shutdown "
                "RPC so a KeepAlive/Restart cannot immediately undo the stop."
            ),
        ),
    ] = False,
) -> None:
    """Request graceful daemon shutdown."""
    flags: GlobalFlags = ctx.obj
    _handle_supervised_agent_on_stop(evict_service=evict_service)
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


@daemon_app.command("reclaim")
def reclaim_cmd(
    ctx: typer.Context,
    keep: Annotated[
        int,
        typer.Option(
            "--keep",
            help="Number of newest state.json.bak.* backups to retain (default 3).",
        ),
    ] = _DEFAULT_BACKUP_KEEP,
    max_age_seconds: Annotated[
        int | None,
        typer.Option(
            "--max-age-seconds",
            help="WAL .fsynced.json retention window in seconds (default: resolved).",
        ),
    ] = None,
) -> None:
    """Reclaim disk: sweep the WAL once and trim aged state.json backups.

    A one-shot janitor for a long-lived repo. It (1) runs a single WAL-GC
    sweep via the W01 helper (drops ``.fsynced.json`` records past the
    retention window) and (2) deletes ``state.json.bak.*`` backups beyond
    the ``--keep`` newest. Neither step needs a running daemon -- both read
    the local filesystem directly so the verb works post-crash.

    Args:
        ctx: Typer context (global flags carried on ``ctx.obj``).
        keep: Number of most-recent ``state.json.bak.*`` backups to retain.
            Must be ``>= 0``; a negative value surfaces as an exit-code-2
            usage error.
        max_age_seconds: WAL retention window for the sweep. ``None`` falls
            back to the resolved daemon retention window. A negative or
            absurdly large value surfaces as an exit-code-2 usage error.
    """
    from eawf.runtime.daemon.wal import gc_done_records, resolve_wal_retention_seconds

    flags: GlobalFlags = ctx.obj
    if keep < 0:
        typer.echo("--keep must be >= 0", err=True)
        raise typer.Exit(code=2)
    retention = max_age_seconds if max_age_seconds is not None else resolve_wal_retention_seconds()
    if retention < 0 or retention > 30 * 24 * 3600:
        typer.echo("--max-age-seconds must be between 0 and 2592000", err=True)
        raise typer.Exit(code=2)

    wal_dir = _wal_dir()
    swept = gc_done_records(wal_dir, max_age_seconds=retention)
    trimmed = _trim_state_backups(flags.workspace, keep)

    payload = {
        "wal_swept_count": len(swept),
        "wal_swept_paths": [str(p) for p in swept],
        "backups_trimmed_count": len(trimmed),
        "backups_trimmed_paths": [str(p) for p in trimmed],
        "keep": keep,
        "max_age_seconds": retention,
        "wal_dir": str(wal_dir),
    }
    text = (
        f"reclaim swept {len(swept)} WAL record(s) "
        f"and trimmed {len(trimmed)} state backup(s) keep={keep}"
    )
    emit_json_or_text(payload, text, flags=flags)


def _trim_state_backups(workspace: Path | None, keep: int) -> list[Path]:
    """Delete ``state.json.bak.*`` files beyond the *keep* newest by mtime.

    Resolves the active ``.ea/state.json`` path, globs its sibling
    ``state.json.bak.*`` backups, sorts them newest-first by mtime, and
    unlinks everything past the keep window. The backup suffixes vary
    (migration ``.bak.v<from>.v<to>`` vs config-migration
    ``.bak.<marker>.<epoch>``), so mtime is the recency key rather than the
    lexical name.

    Args:
        workspace: Optional workspace root from the global ``-w/--workspace``
            flag, forwarded to the state-path resolver.
        keep: Number of newest backups to retain (``>= 0``).

    Returns:
        Paths unlinked, newest-trimmed first. Empty when the resolver finds
        no state directory or the backup count is within the keep window.
    """
    from eawf.surfaces.cli.scope import resolve_state_path

    try:
        state_path = resolve_state_path(workspace)
    except FileNotFoundError:
        return []
    backups = list(state_path.parent.glob(f"{state_path.name}.bak.*"))
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    doomed = backups[keep:]
    removed: list[Path] = []
    for path in doomed:
        path.unlink(missing_ok=True)
        removed.append(path)
    logger.info(f"_trim_state_backups keep={keep} removed={len(removed)}")
    return removed


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
    """Report the supervisor-level service state plus daemon-health advisories.

    Distinct from ``eawf daemon status``, which issues the W01
    ``daemon.status`` RPC against the running socket. This verb asks the native
    supervisor for its view of the unit (``running`` here requires both
    registration AND an active PID) and folds in the two daemon-service
    advisories the doctor surface also carries: the launchd/systemd agent
    health (loaded / plist-vs-binary drift / a rival daemon) and the
    runtime-dir + ``.ea`` backup footprint that ``eawf daemon reclaim`` trims.
    """
    from eawf.observability.doctor.checks import (
        check_launchd_agent,
        check_runtime_dir_size,
    )
    from eawf.runtime.daemon.service_install import service_status

    flags: GlobalFlags = ctx.obj
    status = service_status()
    agent_row = check_launchd_agent()
    size_row = check_runtime_dir_size(workspace=flags.workspace)
    payload = {
        "platform": sys.platform,
        "status": status.value,
        "launchd_agent": {"status": agent_row.status, "detail": agent_row.detail},
        "runtime_dir_size": {"status": size_row.status, "detail": size_row.detail},
    }
    text = (
        f"daemon service platform={sys.platform} status={status.value}\n"
        f"  launchd-agent [{agent_row.status}] {agent_row.detail}\n"
        f"  runtime-dir-size [{size_row.status}] {size_row.detail}"
    )
    emit_json_or_text(payload, text, flags=flags)
