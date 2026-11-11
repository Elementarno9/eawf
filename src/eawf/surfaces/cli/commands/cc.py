"""``eawf cc`` Typer subapp — Claude Code adapter surface.

Three surfaces today:

- ``eawf cc statusline`` — read Claude stdin JSON, render one line of
  statusline text to stdout. Honors the ``--theme`` flag, ``NO_COLOR``,
  ``EAWF_STATUSLINE_THEME`` and the ``statusline`` config section. The
  line is cached for each Claude session id, stale-while-revalidate: a
  hit is printed at once (a stale one also starts a background refresh),
  a miss renders and caches.
  The console entry point (:mod:`eawf.__main__`) answers the plain
  invocation, hit or miss, without importing this app; this command
  handles every other form (``--workspace``, a TTY stdin, Windows).
- ``eawf cc statusline prewarm`` — render, write the line to the
  session's cache entry, exit ``0``. The background refresh runs this.
- ``eawf cc statusline install`` — hidden alias of ``eawf statusline
  install`` (:mod:`eawf.surfaces.cli.commands.statusline`).

The render commands always exit ``0`` from a successful pipeline; they
never raise on a malformed Claude payload (decode/empty stdin → empty
dict; modules degrade independently).
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from eawf.surfaces.cli._stdin import require_piped_stdin
from eawf.surfaces.cli.commands.statusline import statusline_install
from eawf.surfaces.cli.flags import GlobalFlags

logger = logging.getLogger(__name__)


cc_app = typer.Typer(
    name="cc",
    help="Claude Code adapter (statusline, plugin, hooks).",
    no_args_is_help=True,
)


statusline_app = typer.Typer(
    name="statusline",
    help="Render the Eä statusline for Claude Code.",
    no_args_is_help=False,
    invoke_without_command=True,
)


@statusline_app.callback(
    invoke_without_command=True,
    help=(
        "Render the Eä statusline for Claude Code "
        "(reads JSON envelope from stdin). At a TTY with no piped data the "
        "command exits 2 with a hint instead of hanging."
    ),
)
def statusline_root(
    ctx: typer.Context,
    theme: Annotated[
        str | None,
        typer.Option(
            "--theme",
            help=(
                "Theme: ansi, catppuccin-latte, catppuccin-frappe, catppuccin-macchiato, "
                "catppuccin-mocha or none (dark and light name mocha and latte). Wins over "
                "NO_COLOR, EAWF_STATUSLINE_THEME and statusline.theme, in that order."
            ),
        ),
    ] = None,
) -> None:
    """Render the statusline rows from a Claude JSON payload on stdin.

    ``--theme`` wins over ``NO_COLOR``, ``EAWF_STATUSLINE_THEME`` and the
    ``statusline.theme`` config leaf. Exit code is always ``0``: a segment
    that cannot be sourced is left out of the rows.
    """
    if ctx.invoked_subcommand is not None:
        return
    from eawf.runtime.runtimes.claude import statusline as statusline_orchestrator

    require_piped_stdin("eawf cc statusline")
    flags: GlobalFlags = ctx.obj
    line = statusline_orchestrator.run_with_cache(
        workspace=flags.workspace,
        theme_name=theme,
    )
    # ``color=True`` overrides Click's TTY-detection so the colour codes
    # survive a pipe to Claude Code's statusline reader.
    typer.echo(line, color=True)


@statusline_app.command(
    name="prewarm",
    help=(
        "Render once and cache the line per Claude session id "
        "(reads JSON envelope from stdin). At a TTY with no piped data the "
        "command exits 2 with a hint instead of hanging."
    ),
)
def statusline_prewarm(
    ctx: typer.Context,
    theme: Annotated[
        str | None,
        typer.Option(
            "--theme",
            help="Theme, as for eawf cc statusline (wins over EAWF_STATUSLINE_THEME).",
        ),
    ] = None,
    payload_at: Annotated[
        float | None,
        typer.Option(
            "--payload-at",
            hidden=True,
            help="Unix time the spawning redraw received the payload; a later entry is kept.",
        ),
    ] = None,
) -> None:
    """Render once and write the line to the per-session cache file."""
    from eawf.runtime.runtimes.claude import statusline as statusline_orchestrator

    require_piped_stdin("eawf cc statusline prewarm")
    flags: GlobalFlags = ctx.obj
    statusline_orchestrator.prewarm(
        workspace=flags.workspace,
        theme_name=theme,
        payload_at=payload_at,
    )


statusline_app.command(
    name="install", hidden=True, help="Alias of eawf statusline install, kept for one release."
)(statusline_install)

cc_app.add_typer(statusline_app, name="statusline")


__all__ = [
    "cc_app",
]
