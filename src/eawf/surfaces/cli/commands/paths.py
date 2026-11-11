"""``eawf paths`` Typer sub-app: where an agent may write in this repository.

CLI dispatch only: the handler resolves the main checkout the way a hook does,
delegates the path to :mod:`eawf.kernel.layout` and prints it.

Verbs:

- ``eawf paths local --kind <kind> [--slug <slug>]`` prints the local folder
  of *kind*, or the dated ``<YYYY-MM-DD>-<slug>.md`` file inside it. Exit 2:
  the kind is unknown or eawf's own, or the slug is malformed. Exit 3: no
  eawf-managed repository holds the working directory.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from eawf.kernel.layout import (
    EA_DIRNAME,
    LocalKindError,
    dated_local_path,
    local_root,
    parse_local_kind,
)
from eawf.runtime.hooks.context import resolve_hook_context
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.scope import NOT_MANAGED_MESSAGE

paths_app = typer.Typer(
    name="paths",
    help="Print the folders an agent may write in this repository.",
    no_args_is_help=True,
    add_completion=False,
)


@paths_app.command("local")
def paths_local(
    ctx: typer.Context,
    kind: Annotated[
        str,
        typer.Option(
            "--kind", help="The local folder: research, spikes, release, reports or archive."
        ),
    ],
    slug: Annotated[
        str | None,
        typer.Option("--slug", help="Print the dated <YYYY-MM-DD>-<slug>.md file in the folder."),
    ] = None,
) -> None:
    """Print the absolute path of one agent-writable ``.ea/local`` folder."""
    flags: GlobalFlags = ctx.obj
    try:
        local_kind = parse_local_kind(kind)
    except LocalKindError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(exit_codes.VALIDATION_ERROR) from None
    context = resolve_hook_context((flags.workspace or Path.cwd()).resolve())
    if context.kind != "managed":
        # Exit 3, not the USER_ERROR other verbs refuse with: a skill reads it
        # as "no write root here", apart from a mistyped kind.
        typer.echo(f"error: {NOT_MANAGED_MESSAGE}", err=True)
        raise typer.Exit(exit_codes.STATE_CONFLICT)
    tree_root = context.main / EA_DIRNAME
    if slug is None:
        typer.echo(str(local_root(tree_root, local_kind)))
        return
    try:
        typer.echo(str(dated_local_path(tree_root, local_kind, slug, date.today())))
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(exit_codes.VALIDATION_ERROR) from None
