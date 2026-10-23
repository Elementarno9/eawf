"""``eawf local`` Typer sub-app: upkeep of the repository's machine-local store.

CLI dispatch only: the handler resolves the repository, delegates planning and
removal to :mod:`eawf.platform.local_gc` and formats the result.

Verbs:

- ``eawf local gc [--dry-run]`` removes each ``.ea/local`` directory whose
  ``.retention`` date has passed; ``--dry-run`` lists them and removes nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from eawf.platform.local_gc import apply_local_gc, plan_local_gc
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

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
    repo = (flags.workspace or Path.cwd()).resolve()
    plan = plan_local_gc(repo / ".ea" / "local", today=date.today())
    if not dry_run:
        apply_local_gc(plan)
    expired = [entry.relative_to(repo).as_posix() for entry in plan.expired]
    unreadable = [entry.relative_to(repo).as_posix() for entry in plan.unreadable]
    verb = "would remove" if dry_run else "removed"
    lines = [f"local gc: {verb} {len(expired)} expired entr{'y' if len(expired) == 1 else 'ies'}"]
    lines += [f"  {path}" for path in expired]
    lines += [f"  kept {path}: its .retention holds no YYYY-MM-DD date" for path in unreadable]
    emit_json_or_text(
        {"dry_run": dry_run, "expired": expired, "unreadable": unreadable},
        "\n".join(lines),
        flags=flags,
    )
