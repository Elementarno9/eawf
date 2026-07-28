"""Operator controls for durable asynchronous wave closure."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

close_app = typer.Typer(
    name="close",
    help="Submit, inspect, follow, resume, or cancel durable wave closure.",
    no_args_is_help=True,
)

_TERMINAL = frozenset({"closed", "blocked", "stale", "failed", "cancelled"})


def call_close_rpc(
    *,
    method: str,
    params: dict[str, Any],
    flags: GlobalFlags,
) -> dict[str, Any]:
    """Call one close RPC with a repository anchor and typed CLI errors."""
    from eawf.runtime.daemon import PROTOCOL_VERSION
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    repo_root = str((flags.workspace or Path.cwd()).resolve())
    payload = {**params, "repo_root": repo_root}
    try:
        with DaemonClient() as client:
            ping = client.call("daemon.ping", {})
            daemon_protocol = ping.get("protocol_version")
            if daemon_protocol is not None and daemon_protocol != PROTOCOL_VERSION:
                raise cli_errors.StateConflict(
                    "close protocol mismatch: restart or reinstall eawf so CLI and "
                    "daemon both run v0.6.2",
                    kind="IntegrityViolation",
                )
            return client.call(method, payload)
    except DaemonRpcError as exc:
        if exc.code == -32601:
            raise cli_errors.StateConflict(
                "close protocol unavailable: restart or reinstall eawf so CLI and "
                "daemon both run v0.6.2",
                kind="IntegrityViolation",
            ) from exc
        if exc.code == cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.ValidationError(exc.message) from exc
        raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
    except (OSError, RuntimeError, TimeoutError) as exc:
        target = params.get("wave_id") or params.get("ref")
        raise cli_errors.DaemonUnreachable(
            f"daemon unavailable for {method} target={target!r}: {exc}"
        ) from exc


def wait_for_close(
    *,
    ref: str,
    flags: GlobalFlags,
    interval_seconds: float = 0.25,
) -> dict[str, Any]:
    """Poll durable status until one terminal attempt state is observed."""
    while True:
        result = call_close_rpc(
            method="close.status",
            params={"ref": ref},
            flags=flags,
        )
        attempt = result["attempt"]
        if attempt["status"] in _TERMINAL:
            return result
        time.sleep(interval_seconds)


def render_close_status(result: dict[str, Any]) -> str:
    """Render one compact human-readable close status."""
    attempt = result["attempt"]
    lines = [
        f"close {attempt['id']} wave={attempt['wave_id']} status={attempt['status']}",
        (f"integration={attempt['integration_id']} commit={attempt['integrated_sha']}"),
        (f"gate receipts={len(attempt['gate_receipt_ids'])}/{len(attempt['required_gate_ids'])}"),
    ]
    if attempt.get("failure_kind"):
        lines.append(
            f"failure={attempt['failure_kind']} "
            f"detail={attempt.get('failure_detail_ref') or 'unavailable'}"
        )
    required_actions = attempt.get("required_operator_actions") or []
    if required_actions:
        lines.append(f"operator action required: {' / '.join(required_actions)}")
    return "\n".join(lines)


def _emit_result(
    result: dict[str, Any],
    *,
    flags: GlobalFlags,
    fail_nonclosed: bool,
) -> None:
    """Emit one attempt and optionally fail for a non-success terminal state."""
    emit_json_or_text(result, render_close_status(result), flags=flags)
    status = result["attempt"]["status"]
    if fail_nonclosed and status in _TERMINAL and status != "closed":
        raise typer.Exit(code=3)


@close_app.command("submit")
def close_submit_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to close.")],
    outcome: Annotated[str, typer.Option("--outcome", help="Outcome description.")],
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Expected integrated commit SHA."),
    ] = None,
    tokens_consumed: Annotated[
        int | None,
        typer.Option("--tokens-consumed", help="Final non-negative token tally."),
    ] = None,
    no_runtime: Annotated[
        bool,
        typer.Option("--no-runtime", help="Accept unavailable runtime capture."),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for a terminal close result."),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Return after durable submission."),
    ] = False,
) -> None:
    """Submit an idempotent exact-revision close attempt."""
    flags: GlobalFlags = ctx.obj
    try:
        if wait and detach:
            raise cli_errors.ValidationError(
                "--wait and --detach are mutually exclusive",
                kind="InvalidInput",
            )
        result = call_close_rpc(
            method="close.submit",
            params={
                "wave_id": wave_id,
                "outcome": outcome,
                "commit": commit,
                "tokens_consumed": tokens_consumed,
                "no_runtime_waiver": no_runtime,
            },
            flags=flags,
        )
        should_wait = wait or (not detach and not (sys.stdin.isatty() and sys.stdout.isatty()))
        if should_wait:
            result = wait_for_close(ref=result["attempt"]["id"], flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _emit_result(result, flags=flags, fail_nonclosed=should_wait)


@close_app.command("status")
def close_status_cmd(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Close-attempt ID or wave ID.")],
) -> None:
    """Show durable close status without waiting."""
    flags: GlobalFlags = ctx.obj
    try:
        result = call_close_rpc(
            method="close.status",
            params={"ref": ref},
            flags=flags,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _emit_result(result, flags=flags, fail_nonclosed=False)


@close_app.command("follow")
def close_follow_cmd(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Close-attempt ID or wave ID.")],
    interval_seconds: Annotated[
        float,
        typer.Option("--interval", min=0.05, help="Polling interval in seconds."),
    ] = 0.25,
) -> None:
    """Follow a close attempt until it reaches a terminal state."""
    flags: GlobalFlags = ctx.obj
    try:
        result = wait_for_close(
            ref=ref,
            flags=flags,
            interval_seconds=interval_seconds,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _emit_result(result, flags=flags, fail_nonclosed=True)


@close_app.command("resume")
def close_resume_cmd(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Close-attempt ID or wave ID.")],
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for a terminal close result."),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Return after durable resumption."),
    ] = False,
) -> None:
    """Resume an interrupted or infrastructure-failed close attempt."""
    flags: GlobalFlags = ctx.obj
    try:
        if wait and detach:
            raise cli_errors.ValidationError(
                "--wait and --detach are mutually exclusive",
                kind="InvalidInput",
            )
        result = call_close_rpc(
            method="close.resume",
            params={"ref": ref},
            flags=flags,
        )
        should_wait = wait or (not detach and not (sys.stdin.isatty() and sys.stdout.isatty()))
        if should_wait:
            result = wait_for_close(ref=result["attempt"]["id"], flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _emit_result(result, flags=flags, fail_nonclosed=should_wait)


@close_app.command("cancel")
def close_cancel_cmd(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Close-attempt ID or wave ID.")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Optional cancellation reason."),
    ] = None,
) -> None:
    """Cancel a close attempt before its APPLYING stage."""
    flags: GlobalFlags = ctx.obj
    try:
        result = call_close_rpc(
            method="close.cancel",
            params={"ref": ref, "reason": reason},
            flags=flags,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _emit_result(result, flags=flags, fail_nonclosed=False)


__all__ = [
    "call_close_rpc",
    "close_app",
    "render_close_status",
    "wait_for_close",
]
