"""``eawf cc statusline install`` global-install wizard (B018 W41).

A thin CLI flow that wires the Eä statusline into the operator's global
Claude Code setup by patching ``~/.claude/settings.json`` to invoke
``eawf cc statusline`` on every redraw. All install logic lives in the pure
:mod:`eawf.runtime.runtimes.claude.statusline_install` library; this handler
only resolves the path, gates on a confirmation, and maps a bad settings
file onto the operator-facing ``UserError`` bucket.

The wizard is idempotent: a re-run on an already-installed setup reports the
no-op and exits ``0`` without rewriting. ``--yes`` skips the interactive
confirmation (for non-interactive / scripted installs); declining the
confirmation aborts with a ``UserError`` (user-declined).
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from eawf.runtime.runtimes.claude.statusline_install import (
    global_settings_path,
    install_statusline,
    is_already_installed,
    read_settings,
)
from eawf.surfaces.cli.errors import UserError

logger = logging.getLogger(__name__)


statusline_wizard_app = typer.Typer(
    name="install",
    help="Install the Eä statusline into the global Claude Code setup.",
    no_args_is_help=False,
    invoke_without_command=True,
)


@statusline_wizard_app.callback(invoke_without_command=True)
def statusline_install(
    ctx: typer.Context,
    assume_yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the confirmation prompt (for non-interactive installs).",
        ),
    ] = False,
) -> None:
    """Patch ``~/.claude/settings.json`` to invoke the Eä statusline.

    Reads the current global settings, reports a no-op when the Eä
    statusline command is already wired, otherwise confirms (unless
    ``--yes``) and writes the patched settings.

    Raises:
        UserError: When the operator declines the confirmation
            (``kind="UserDeclined"``) or the existing settings file is not
            valid JSON (``kind="InvalidInput"``).
    """
    if ctx.invoked_subcommand is not None:
        return
    path = global_settings_path()
    try:
        current = read_settings(path)
    except ValueError as exc:
        raise UserError(str(exc), kind="InvalidInput") from exc

    if is_already_installed(current):
        typer.echo(f"statusline already installed in {path}")
        return

    if not assume_yes and not typer.confirm(f"Install the Eä statusline into {path}?"):
        raise UserError("statusline install declined", kind="UserDeclined")

    install_statusline(path)
    typer.echo(f"statusline installed in {path}")


__all__ = [
    "statusline_install",
    "statusline_wizard_app",
]
