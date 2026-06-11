"""``eawf track sync`` Typer verb -- recompute a Track's outcome standings.

The Track ``add`` / ``switch`` setup verbs live in
:mod:`eawf.surfaces.cli.commands.lifecycle_iter` (attached to the shared
``track_app`` Typer app in :mod:`eawf.surfaces.cli.commands.lifecycle`). This
module attaches one more verb to that same app:

- ``eawf track sync [<track-id>]`` -> asks the daemon to recompute every
  measured outcome status for the Track from its samples (the same
  :func:`eawf.workflow.evidence.outcome.sync_track_outcomes` reducer the
  wave-close hook fires), then reports which outcome statuses moved.

The reducer also runs automatically on every wave close (the daemon wave-close
hook), so closing work that moves a metric updates the Track standings without
this verb. ``track sync`` is the on-demand surface onto the same canonical
daemon writer (AGENTS rule 4) for the operator who wants to re-derive the
standings by hand.

The verb routes through the daemon (the sole canonical state writer), so it
honours the mutating-verb escalation rule: ``--daemonless`` /
``EAWF_DAEMONLESS=1`` is rejected and a daemon is auto-spawned when none is up.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import track_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)

#: ``track.sync`` recomputes a Track's measured outcome statuses from their
#: samples. Single-sourced here so the verb handler and any future caller key
#: off one spelling.
_SYNC_RPC: str = "track.sync"


def _sync_track(*, track_id: str | None, flags: GlobalFlags) -> dict[str, Any]:
    """Call the ``track.sync`` RPC and return the daemon's result dict.

    Mirrors the daemon-calling convention the dispatch verbs use: apply the
    mutating-verb escalation rule (reject ``--daemonless`` and auto-spawn a
    daemon), open a :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`, call
    ``track.sync`` with the optional track id + the per-request repo anchor, and
    map any JSON-RPC error onto its typed
    :class:`~eawf.surfaces.cli.errors.CliError` so the verb handler surfaces a
    proper error envelope rather than leaking a raw ``DaemonRpcError``.

    Args:
        track_id: The Track to sync, or ``None`` to sync the Track under the
            ``current.track_id`` cursor.
        flags: Resolved global flags (``--daemonless`` source + the workspace
            anchor forwarded as ``repo_root``).

    Returns:
        The daemon's ``track.sync`` result dict (a JSON-mode
        :class:`~eawf.runtime.daemon.methods.state.TrackSyncRpcResult`),
        carrying the ids of the outcomes whose status moved.

    Raises:
        UserError: When ``--daemonless`` / ``EAWF_DAEMONLESS=1`` was requested
            (the sync is daemon-only; ``data.kind="InvalidInput"``).
        ValidationError: When the daemon rejects with ``-32002``.
        DaemonUnreachable: When the daemon is unreachable / drops the
            connection / times out.
        CliError: When the daemon returns any other JSON-RPC error envelope
            (mapped via :func:`~eawf.surfaces.cli.errors.cli_error_for_rpc`).
    """
    from eawf.surfaces.cli import _dispatch
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    repo_root = str((flags.workspace or Path.cwd()).resolve())
    params: dict[str, Any] = {"repo_root": repo_root, "track_id": track_id}
    try:
        _dispatch.escalate_mutation("track sync", flags=flags)
        with DaemonClient() as client:
            result: dict[str, Any] = client.call(_SYNC_RPC, params)
    except DaemonRpcError as exc:
        if exc.code == cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.ValidationError(exc.message) from exc
        raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(
            f"daemon unavailable for {_SYNC_RPC}: {exc}"
        ) from exc
    changed = int(result.get("changed", 0))
    logger.info(f"_sync_track track={track_id!r} changed={changed}")
    return result


@track_app.command("sync")
def track_sync_cmd(
    ctx: typer.Context,
    track_id: str = typer.Argument(
        None,
        help="Track id to sync; defaults to the active track (current.track_id).",
    ),
) -> None:
    """Recompute a Track's measured outcome statuses from their samples.

    Routes through the daemon's ``track.sync`` RPC (the canonical state writer)
    so an operator can re-derive the Track standings on demand. The same reducer
    runs automatically on every wave close, so this verb is for the case where
    the operator wants to refresh the standings by hand. Reports which outcome
    statuses moved (empty when nothing changed or no Track was in focus).
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _sync_track(track_id=track_id, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    resolved = result.get("track_id")
    changed_ids = list(result.get("changed_outcome_ids", []))
    changed = int(result.get("changed", 0))
    emit_json_or_text(
        {
            "track_id": resolved,
            "changed_outcome_ids": changed_ids,
            "changed": changed,
        },
        f"track sync {resolved} (changed {changed} outcome statuses)",
        flags=flags,
    )


__all__ = [
    "track_sync_cmd",
]
