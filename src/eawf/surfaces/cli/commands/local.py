"""``eawf local`` Typer sub-app: upkeep of the repository's machine-local store.

CLI dispatch only: the handlers resolve the repository, delegate planning and
the change to :mod:`eawf.platform.local_gc` or :mod:`eawf.platform.local_tidy`
and format the result.

Verbs:

- ``eawf local gc [--dry-run]`` removes each ``.ea/local`` directory whose
  ``.retention`` date has passed; ``--dry-run`` lists them and removes nothing.
- ``eawf local tidy [--dry-run]`` moves each hand-made ``.ea/local`` entry and
  each orphan ``.ea`` file into ``.ea/local/archive/<YYYY-MM-DD>-tidy/``, and an
  earlier version's ``.ea/local/reflect`` files into
  ``.ea/local/reports/reflect``, recording each move in the archive's
  ``MANIFEST.tsv``; ``--dry-run`` prints the ``from<TAB>to<TAB>bytes`` rows and
  changes nothing. Exit 3: no eawf-managed repository holds the working
  directory, git cannot list its tracked files, or an entry planned to move
  could not be moved.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.platform.local_gc import apply_local_gc, plan_local_gc
from eawf.platform.local_tidy import (
    LocalTidyError,
    apply_local_tidy,
    manifest_line,
    plan_local_tidy,
)
from eawf.runtime.hooks.context import resolve_hook_context
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import NOT_MANAGED_MESSAGE

local_app = typer.Typer(
    name="local",
    help="Upkeep of the machine-local .ea/local store.",
    no_args_is_help=True,
    add_completion=False,
)


@local_app.command("gc")
def local_gc(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List the expired entries and remove nothing."),
    ] = False,
) -> None:
    """Remove each ``.ea/local`` entry whose ``.retention`` date has passed."""
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    repo = state_path.parent.parent.resolve()
    plan = plan_local_gc(repo / ".ea" / "local", today=date.today())
    if not dry_run:
        apply_local_gc(plan)
    expired = [entry.relative_to(repo).as_posix() for entry in plan.expired]
    held = [entry.relative_to(repo).as_posix() for entry in plan.held]
    unreadable = [entry.relative_to(repo).as_posix() for entry in plan.unreadable]
    verb = "would remove" if dry_run else "removed"
    lines = [f"local gc: {verb} {len(expired)} expired entr{'y' if len(expired) == 1 else 'ies'}"]
    lines += [f"  {path}" for path in expired]
    lines += [f"  kept {path}: it holds an entry that is unexpired or unreadable" for path in held]
    lines += [f"  kept {path}: its .retention holds no YYYY-MM-DD date" for path in unreadable]
    emit_json_or_text(
        {"dry_run": dry_run, "expired": expired, "held": held, "unreadable": unreadable},
        "\n".join(lines),
        flags=flags,
    )


@local_app.command("tidy")
def local_tidy(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List the moves and change nothing."),
    ] = False,
) -> None:
    """Move hand-made ``.ea/local`` entries and orphan ``.ea`` files into a dated archive."""
    flags: GlobalFlags = ctx.obj
    context = resolve_hook_context((flags.workspace or Path.cwd()).resolve())
    if context.kind != "managed":
        typer.echo(f"error: {NOT_MANAGED_MESSAGE}", err=True)
        raise typer.Exit(exit_codes.STATE_CONFLICT)
    try:
        plan = plan_local_tidy(context.main, today=date.today())
    except LocalTidyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(exit_codes.STATE_CONFLICT) from None
    result = None if dry_run else apply_local_tidy(plan)
    moves = plan.moves if result is None else result.made
    rows = [manifest_line(plan, move) for move in moves]
    kept = [
        {"path": entry.path.relative_to(plan.repo).as_posix(), "reason": entry.reason}
        for entry in (*plan.kept, *(() if result is None else result.kept))
    ]
    verb = "would move" if dry_run else "moved"
    lines = [f"local tidy: {verb} {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}"]
    lines += [f"  {row}" for row in rows]
    lines += [f"  kept {entry['path']}: {entry['reason']}" for entry in kept]
    emit_json_or_text(
        {
            "dry_run": dry_run,
            "manifest": plan.archive_dir.relative_to(plan.repo).as_posix(),
            "rows": rows,
            "kept": kept,
        },
        "\n".join(lines),
        flags=flags,
    )
    if result is not None and result.kept:
        raise typer.Exit(exit_codes.STATE_CONFLICT)
