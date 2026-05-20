"""``eawf cc`` Typer subapp — Claude Code adapter surface (Phase 4 W06).

Two surfaces today:

- ``eawf cc statusline`` — read Claude stdin JSON, render one line of
  statusline text to stdout. Honors ``--theme`` flag and the
  ``EAWF_STATUSLINE_THEME`` env var.
- ``eawf cc statusline prewarm`` — run the same render pipeline, write
  the rendered line to ``~/.claude/statusline-cache/<session-id>.json``,
  exit ``0``. The cache is consulted by subsequent ``statusline`` runs
  when the same ``session_id`` is on stdin.

Both commands always exit ``0`` from a successful pipeline; they never
raise on a malformed Claude payload (decode/empty stdin → empty dict;
modules degrade independently per the W06 contract).
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from eawf.cli._stdin import require_piped_stdin
from eawf.cli.flags import GlobalFlags

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
            help="Theme name from templates/themes.yaml (default, powerline, ascii-fallback).",
        ),
    ] = None,
) -> None:
    """Render one line of statusline text from a Claude JSON payload on stdin.

    Honors ``--theme`` and ``EAWF_STATUSLINE_THEME`` (flag wins). Exit
    code is always ``0`` — degraded modules render a fallback string per
    the W06 contract.
    """
    if ctx.invoked_subcommand is not None:
        return
    from eawf.runtimes.claude import statusline as statusline_orchestrator

    require_piped_stdin("eawf cc statusline")
    flags: GlobalFlags = ctx.obj
    line = statusline_orchestrator.run_with_cache(
        workspace=flags.workspace,
        theme_name=theme,
    )
    # ``color=True`` overrides Click's TTY-detection so the ASCII colour
    # codes survive a pipe to Claude Code's statusline reader. The
    # ``ascii-fallback`` theme yields zero color bytes when colour is
    # unwanted.
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
            help="Theme name (overrides EAWF_STATUSLINE_THEME).",
        ),
    ] = None,
) -> None:
    """Render once and write the line to the per-session cache file."""
    from eawf.runtimes.claude import statusline as statusline_orchestrator

    require_piped_stdin("eawf cc statusline prewarm")
    flags: GlobalFlags = ctx.obj
    statusline_orchestrator.prewarm(
        workspace=flags.workspace,
        theme_name=theme,
    )


cc_app.add_typer(statusline_app, name="statusline")


__all__ = [
    "cc_app",
]
