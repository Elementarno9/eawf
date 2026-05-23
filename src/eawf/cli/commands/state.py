"""``eawf state`` — state introspection + dev-mode raw RPC passthrough.

Subcommands:

- ``eawf state resolve [--workspace <path>]`` — print the resolved
  ``state.json`` path and the *reason* it was selected
  (``env`` / ``workspace_flag`` / ``pwd_upward``).
- ``eawf state show [--workspace <path>]`` — read-only ``state.json``
  view. Honours the daemon-bypass carve-out: with ``--daemonless``
  (or ``EAWF_DAEMONLESS=1``) it reads the file directly and never spawns
  the daemon.
- ``eawf state rpc <method> [--params <json>]`` — **dev-mode only** raw
  JSON-RPC passthrough to the daemon. Hidden unless ``--debug`` (or
  ``EAWF_DEBUG=1``) is set; it is the developer escape hatch for poking
  the daemon directly and is never exposed in normal operation. Domain
  verbs (``wave claim``, ``phase open``, ...) are the supported mutation
  surface (``state mutate`` is hidden entirely).

The resolver itself lives in :mod:`eawf.state.resolve` so other waves
(e.g. ``status``, ``store compact``) reuse it without depending on the
CLI layer. ``resolve`` and ``show`` never mutate state and never acquire
a lock. ``rpc`` is the only mutating-capable verb here and routes
through the §5.5 escalation path (auto-spawn; refuses ``--daemonless``
when the method is a mutator).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli._dispatch import (
    daemonless_requested,
    dev_mode_enabled,
    ensure_daemon,
    reject_daemonless_on_mutating,
)
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.state.resolve import resolve_with_reason

logger = logging.getLogger(__name__)


#: JSON-RPC methods that mutate daemon-owned state. A raw ``state rpc``
#: call targeting one of these refuses ``--daemonless`` per the §5.5
#: mutating-verb rule; everything else (``daemon.ping``, ``state.read``,
#: ``state.digest``, ...) is treated as read-only and may bypass.
_RAW_RPC_MUTATING_PREFIXES: tuple[str, ...] = (
    "state.mutate",
    "config.set_layer_value",
    "registry.update",
    "spec.init",
    "spec.promote",
    "spec.archive",
)


state_app = typer.Typer(
    name="state",
    help="Read-only state introspection (resolve, show) + dev-mode raw RPC.",
    no_args_is_help=True,
    add_completion=False,
)


@state_app.command(name="resolve")
def resolve_cmd(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root to anchor the resolver (overrides pwd-upward).",
        ),
    ] = None,
) -> None:
    """Print the resolved ``state.json`` path and the reason for selection."""
    flags: GlobalFlags = ctx.obj
    effective_ws = workspace if workspace is not None else flags.workspace
    path, reason = resolve_with_reason(workspace=effective_ws)
    payload: dict[str, str] = {"path": str(path), "reason": reason}
    emit_json_or_text(
        dict(payload),
        f"{path}\nreason: {reason}",
        flags=flags,
    )


@state_app.command(name="show")
def show_cmd(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root to anchor the resolver (overrides pwd-upward).",
        ),
    ] = None,
) -> None:
    """Print a read-only view of ``state.json``.

    This is a read-only (R) verb. Per the escalation table it honours
    the daemon-bypass carve-out: when ``--daemonless`` (or
    ``EAWF_DAEMONLESS=1``) is set it reads the resolved file directly and
    never spawns the daemon. Without the carve-out a future wave will
    route through the daemon ``state.read`` RPC for cache freshness; this
    wave keeps the direct read as the daemonless path and surfaces the
    summary fields the operator needs.

    Raises:
        NotFound: When the resolved ``state.json`` does not exist.
    """
    flags: GlobalFlags = ctx.obj
    effective_ws = workspace if workspace is not None else flags.workspace
    path, _reason = resolve_with_reason(workspace=effective_ws)
    if not path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"state file not found: {path}", kind="NotFound"),
            flags=flags,
            data={"path": str(path)},
        )
    payload = orjson.loads(path.read_bytes())
    project = payload.get("project") or {}
    summary: dict[str, Any] = {
        "path": str(path),
        "schema_version": payload.get("schema_version"),
        "project_code": project.get("code"),
        "phase_count": len(payload.get("phases") or {}),
        "iter_count": len(payload.get("iters") or {}),
        "wave_count": len(payload.get("waves") or {}),
    }
    text = (
        f"{summary['path']}\n"
        f"schema_version: {summary['schema_version']}\n"
        f"project: {summary['project_code']}\n"
        f"phases={summary['phase_count']} "
        f"iters={summary['iter_count']} "
        f"waves={summary['wave_count']}"
    )
    emit_json_or_text(summary, text, flags=flags)


@state_app.command(name="rpc", hidden=True)
def rpc_cmd(
    ctx: typer.Context,
    method: Annotated[
        str,
        typer.Argument(help="Dotted JSON-RPC method name (e.g. daemon.ping)."),
    ],
    params: Annotated[
        str | None,
        typer.Option(
            "--params",
            help="JSON object passed verbatim as the RPC params (default {}).",
        ),
    ] = None,
) -> None:
    """Issue a raw JSON-RPC call against the daemon (dev-mode only).

    Hidden unless dev-mode is on (``--debug`` flag or ``EAWF_DEBUG=1``).
    This is the developer escape hatch for poking the daemon directly —
    there is **no** supported operator use; domain verbs (``wave
    claim``, ``phase open``, ...) are the production mutation surface
    (``state mutate`` is hidden entirely).

    Escalation: when *method* names a mutating RPC, the verb
    refuses ``--daemonless`` (mutating verbs are daemon-only) and
    auto-spawns the daemon if none is running. Read-only methods
    (``daemon.ping``, ``state.read``, ...) honour the bypass — but a
    raw RPC still needs a live socket, so the helper always ensures one.

    Args:
        ctx: Typer context (global flags carried on ``ctx.obj``).
        method: Dotted JSON-RPC method name.
        params: Optional JSON object string; defaults to ``{}``.

    Raises:
        UserError: When dev-mode is off (the verb is gated), when
            *params* is not a JSON object, or when ``--daemonless`` is
            passed alongside a mutating method.
        DaemonUnreachable: When the daemon auto-spawn failed.
    """
    flags: GlobalFlags = ctx.obj
    if not dev_mode_enabled(flags):
        cli_errors.emit_error(
            cli_errors.UserError(
                "state rpc is a dev-mode-only verb; re-run with --debug or set EAWF_DEBUG=1"
            ),
            flags=flags,
            data={"kind": "InvalidInput", "verb": "state rpc"},
        )
    rpc_params = _parse_rpc_params(params, flags=flags)
    is_mutating = method.startswith(_RAW_RPC_MUTATING_PREFIXES)
    if is_mutating and daemonless_requested(flags):
        try:
            reject_daemonless_on_mutating("state rpc")
        except cli_errors.UserError as exc:
            cli_errors.emit_error(
                exc,
                flags=flags,
                data={"kind": "InvalidInput", "verb": "state rpc", "method": method},
            )
    ensure_daemon()
    from eawf.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient() as client:
            result = client.call(method, rpc_params)
    except DaemonRpcError as exc:
        # No ``kind`` override here: the specific kind threaded by
        # ``cli_error_for_rpc`` (LockConflict / NotFound / ...) must reach
        # the envelope; supplying ``kind`` would mask it (explicit wins).
        cli_errors.emit_error(
            cli_errors.cli_error_for_rpc(exc.code, exc.message),
            flags=flags,
            data={"method": method, "rpc_code": exc.code},
        )
    text = f"rpc ok method={method} result={result}"
    emit_json_or_text({"method": method, "result": result}, text, flags=flags)


def _parse_rpc_params(raw: str | None, *, flags: GlobalFlags) -> dict[str, Any]:
    """Parse the ``--params`` JSON string into a params object.

    Args:
        raw: The raw ``--params`` string, or ``None`` for the empty
            default.
        flags: Resolved global flags (drives the error envelope branch).

    Returns:
        The parsed params object. ``None`` maps to ``{}``.

    Raises:
        UserError: When *raw* is not valid JSON or decodes to a
            non-object (the daemon params contract is an object).
    """
    if raw is None:
        return {}
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        cli_errors.emit_error(
            cli_errors.UserError(f"--params is not valid JSON: {raw!r}"),
            flags=flags,
            data={"kind": "InvalidInput"},
        )
    if not isinstance(parsed, dict):
        cli_errors.emit_error(
            cli_errors.UserError(f"--params must be a JSON object, got: {raw!r}"),
            flags=flags,
            data={"kind": "InvalidInput"},
        )
    return parsed
