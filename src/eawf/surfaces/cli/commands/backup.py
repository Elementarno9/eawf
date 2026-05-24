"""``eawf backup`` Typer sub-app — manual snapshot create/list/restore/prune.

CLI dispatch only (AGENTS rule 1): the handlers parse args, resolve the repo's
``.ea/state.json`` path, delegate the on-disk work to :mod:`eawf.backup`, and
route output through :func:`eawf.surfaces.cli.output.emit_json_or_text`. The backup tree
lives under the user-scope home (``~/.eawf/backups/<repo_sha>/``) keyed by
``sha256(repo-absolute-path)[:12]`` so backups never sit inside the committed
repo tree and never collide across repos.

Verbs:

- ``eawf backup create [--note <str>]`` — snapshot ``state.json`` +
  ``config.yaml`` + ``profile.yaml`` into a timestamped dir.
- ``eawf backup list`` — list every snapshot, most-recent first.
- ``eawf backup restore --ts <ISO>`` — restore the named snapshot (writes a
  pre-restore safety copy of the live ``state.json`` first).
- ``eawf backup prune --keep <N>`` — keep the N most-recent snapshots.

Exit codes:

- ``0`` — success.
- ``1`` (``USER_ERROR``) — no state to back up, unknown ``--ts``, or a bad
  ``--keep`` value.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from eawf.backup import (
    BackupError,
    UnknownSnapshotError,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
)
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


backup_app = typer.Typer(
    name="backup",
    help="Snapshot backups of state.json + config.yaml + profile.yaml.",
    no_args_is_help=True,
    add_completion=False,
)


@backup_app.command("create")
def backup_create(
    ctx: typer.Context,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Operator note saved alongside the snapshot."),
    ] = None,
) -> None:
    """Snapshot the repo's ``.ea/`` artifacts into a timestamped backup dir.

    Copies ``state.json`` (mandatory) plus ``config.yaml`` / ``profile.yaml``
    when present into ``~/.eawf/backups/<repo_sha>/<timestamp>/``. Exits
    ``USER_ERROR`` when the repo has no ``state.json`` to snapshot.
    """
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    try:
        snapshot = create_backup(state_path, note=note)
    except BackupError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
        return

    payload: dict[str, object] = {
        "ts": snapshot.ts,
        "path": str(snapshot.path),
        "artifacts": list(snapshot.artifacts),
        "note": snapshot.note,
    }
    text = f"backup create: {snapshot.ts} ({', '.join(snapshot.artifacts)})"
    emit_json_or_text(payload, text, flags=flags)


@backup_app.command("list")
def backup_list(ctx: typer.Context) -> None:
    """List every snapshot for the current repo, most-recent first."""
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    snapshots = list_backups(state_path)

    payload: dict[str, object] = {
        "snapshots": [
            {
                "ts": s.ts,
                "artifacts": list(s.artifacts),
                "note": s.note,
            }
            for s in snapshots
        ],
    }
    if snapshots:
        lines = ["backups:"]
        lines += [
            f"  {s.ts} — {', '.join(s.artifacts)}" + (f" — {s.note}" if s.note else "")
            for s in snapshots
        ]
        text = "\n".join(lines)
    else:
        text = "backups: <none>"
    emit_json_or_text(payload, text, flags=flags)


@backup_app.command("restore")
def backup_restore(
    ctx: typer.Context,
    ts: Annotated[
        str,
        typer.Option("--ts", help="Snapshot timestamp to restore (see `eawf backup list`)."),
    ],
) -> None:
    """Restore ``state.json`` + ``config.yaml`` + ``profile.yaml`` from *ts*.

    Writes a pre-restore safety copy of the live ``state.json`` first, then
    copies the snapshot's artifacts back byte-for-byte. Exits ``USER_ERROR``
    when ``--ts`` does not name an existing snapshot.
    """
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    try:
        result = restore_backup(state_path, ts=ts)
    except UnknownSnapshotError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
        return
    except BackupError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
        return

    payload: dict[str, object] = {
        "ts": result.snapshot.ts,
        "restored": list(result.restored),
        "pre_restore": str(result.pre_restore) if result.pre_restore is not None else None,
    }
    text = f"backup restore: {result.snapshot.ts} ({', '.join(result.restored)})"
    emit_json_or_text(payload, text, flags=flags)


@backup_app.command("prune")
def backup_prune(
    ctx: typer.Context,
    keep: Annotated[
        int,
        typer.Option("--keep", help="Number of most-recent snapshots to retain."),
    ],
) -> None:
    """Keep the N most-recent snapshots; delete older ones.

    Exits ``USER_ERROR`` when ``--keep`` is negative.
    """
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    try:
        removed = prune_backups(state_path, keep=keep)
    except BackupError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
        return

    payload: dict[str, object] = {"keep": keep, "removed": removed}
    if removed:
        text = f"backup prune: kept {keep}, removed {len(removed)} ({', '.join(removed)})"
    else:
        text = f"backup prune: kept {keep}, removed 0"
    emit_json_or_text(payload, text, flags=flags)
