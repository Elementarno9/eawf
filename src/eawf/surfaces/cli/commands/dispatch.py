"""``eawf dispatch`` Typer sub-app — pause / resume the dispatch loop.

Two parameterless verbs toggle the durable
:attr:`~eawf.kernel.state.models.State.dispatch_paused` flag through the
daemon's ``agent.pause`` / ``agent.resume`` RPCs:

- ``eawf dispatch pause``  -> persists ``dispatch_paused = True`` so the next
  :func:`eawf.workflow.lifecycle.wave.claim_wave` is rejected.
- ``eawf dispatch resume`` -> persists ``dispatch_paused = False`` so claims
  are accepted again.

Until now those RPCs were wired only to the TUI autopilot ``space`` key, which
is a catch-22 when the TUI is frozen mid-run: there was no headless way to
clear a stuck pause. These verbs give the operator a CLI surface onto the same
canonical daemon writer (AGENTS rule 4) so dispatch can be unpaused without the
TUI. Both report the resulting persisted flag.

Both verbs mutate ``state.json`` (through the daemon, which is the canonical
writer), so they honour the mutating-verb escalation rule:
``--daemonless`` / ``EAWF_DAEMONLESS=1`` is rejected (the toggle is
daemon-only) and a daemon is auto-spawned when none is up.
"""

from __future__ import annotations

import logging
from typing import Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


#: ``agent.pause`` persists ``dispatch_paused = True`` (a deliberate operator
#: stop that blocks the next claim); ``agent.resume`` clears it. Single-sourced
#: here so the two verb handlers and any future caller key off one spelling.
_PAUSE_RPC: str = "agent.pause"
_RESUME_RPC: str = "agent.resume"


dispatch_app = typer.Typer(
    name="dispatch",
    help="Pause and resume the dispatch loop (headless toggle of dispatch_paused).",
    no_args_is_help=True,
)


def _toggle_dispatch(*, method: str, verb: str, flags: GlobalFlags) -> bool:
    """Call the *method* pause/resume RPC and return the persisted flag.

    Mirrors the daemon-calling convention the worktree mutators use: apply
    the mutating-verb escalation rule (reject ``--daemonless`` and auto-spawn
    a daemon), open a :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`,
    call the parameterless RPC, and map any JSON-RPC error onto its typed
    :class:`~eawf.surfaces.cli.errors.CliError` so the verb handler surfaces a
    proper error envelope rather than leaking a raw ``DaemonRpcError``.

    Args:
        method: The RPC method name (``agent.pause`` or ``agent.resume``).
        verb: Operator-facing verb name for the ``--daemonless`` rejection
            envelope (e.g. ``"dispatch resume"``).
        flags: Resolved global flags (``--daemonless`` source).

    Returns:
        The persisted :attr:`~eawf.kernel.state.models.State.dispatch_paused`
        value the daemon returned (``True`` after pause, ``False`` after
        resume).

    Raises:
        UserError: When ``--daemonless`` was requested (the toggle is
            daemon-only; ``data.kind="InvalidInput"``).
        ValidationError: When the daemon rejects with ``-32002``.
        DaemonUnreachable: When the daemon is unreachable / drops the
            connection / times out.
        CliError: When the daemon returns any other JSON-RPC error envelope
            (mapped via :func:`~eawf.surfaces.cli.errors.cli_error_for_rpc`).
    """
    from eawf.surfaces.cli import _dispatch
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        _dispatch.escalate_mutation(verb, flags=flags)
        with DaemonClient() as client:
            result: dict[str, Any] = client.call(method, {})
    except DaemonRpcError as exc:
        if exc.code == cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.ValidationError(exc.message) from exc
        raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc
    paused = bool(result.get("paused"))
    logger.info(f"_toggle_dispatch method={method!r} paused={paused}")
    return paused


@dispatch_app.command("resume")
def dispatch_resume(ctx: typer.Context) -> None:
    """Resume the dispatch loop by clearing ``dispatch_paused`` via ``agent.resume``.

    Routes through the daemon (the canonical state writer) so dispatch can be
    unpaused without the TUI. Reports the resulting ``dispatch_paused`` flag
    (``false`` on success). Idempotent — resuming an already-running state
    re-writes the same flag.
    """
    flags: GlobalFlags = ctx.obj
    try:
        paused = _toggle_dispatch(method=_RESUME_RPC, verb="dispatch resume", flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_json_or_text(
        {"dispatch_paused": paused},
        f"dispatch resumed (dispatch_paused={paused})",
        flags=flags,
    )


@dispatch_app.command("pause")
def dispatch_pause(ctx: typer.Context) -> None:
    """Pause the dispatch loop by setting ``dispatch_paused`` via ``agent.pause``.

    Routes through the daemon (the canonical state writer); while the flag is
    set :func:`eawf.workflow.lifecycle.wave.claim_wave` rejects every claim
    until ``dispatch resume`` clears it. Reports the resulting
    ``dispatch_paused`` flag (``true`` on success). Idempotent — pausing an
    already-paused state re-writes the same flag.
    """
    flags: GlobalFlags = ctx.obj
    try:
        paused = _toggle_dispatch(method=_PAUSE_RPC, verb="dispatch pause", flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_json_or_text(
        {"dispatch_paused": paused},
        f"dispatch paused (dispatch_paused={paused})",
        flags=flags,
    )


__all__ = [
    "dispatch_app",
]
