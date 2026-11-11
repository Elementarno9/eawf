"""``eawf statusline``: install the Claude Code status line once per user and diagnose it.

- ``eawf statusline install [--scope user|project|local] [--print] [--yes]``
  sets the ``statusLine`` key of one settings file. Exit ``0`` when it is
  written or already the same, ``1`` when another entry is kept (``--yes``
  replaces it), ``6`` when the file cannot be written: the line goes to
  stdout for the operator to add by hand.
- ``eawf statusline doctor [--json]`` reports the winning entry and its
  verdict; exit ``1`` only for ``broken``.

``eawf cc statusline install`` stays as a hidden alias of the install verb.
The library lives in :mod:`eawf.runtime.runtimes.claude.statusline_install`,
imported in-body to keep it off the CLI's import path.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags

statusline_cli_app = typer.Typer(
    name="statusline",
    help="Install the Claude Code status line once per user and diagnose it.",
    no_args_is_help=True,
)


def statusline_install(
    scope: Annotated[
        Literal["user", "project", "local"],
        typer.Option(
            "--scope",
            help=(
                "Settings file: user (~/.claude/settings.json), project "
                "(.claude/settings.json) or local (.claude/settings.local.json)."
            ),
        ),
    ] = "user",
    print_only: Annotated[
        bool,
        typer.Option("--print", help="Print the statusLine line and write nothing."),
    ] = False,
    assume_yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Replace another statusLine entry."),
    ] = False,
) -> None:
    """Set ``statusLine`` to run ``eawf cc statusline``, changing no other key.

    The settings file is written anew from its parsed content: every key
    keeps its order and the file keeps its indentation, but inline arrays
    and objects are laid out one item per line.
    """
    from eawf.runtime.runtimes.claude.statusline_install import (
        SettingsNotWritable,
        install_statusline,
        settings_path,
        statusline_line,
    )

    line = statusline_line()
    if print_only:
        typer.echo(line)
        return
    path = settings_path(scope, cwd=Path.cwd())
    try:
        result = install_statusline(path, replace=assume_yes)
    except SettingsNotWritable as exc:
        typer.echo(line)
        typer.echo(
            f"statusline not installed: {exc.path}: {exc.reason}; add the line above to that file",
            err=True,
        )
        raise typer.Exit(code=exit_codes.NEEDS_OPERATOR) from exc
    if result.status == "differs":
        typer.echo(f"statusLine in {path}: {json.dumps(result.current)}")
        typer.echo("statusline not installed: another entry is set; --yes replaces it", err=True)
        raise typer.Exit(code=exit_codes.USER_ERROR)
    verb = "already installed" if result.status == "already" else "installed"
    typer.echo(f"statusline {verb}: {path}")


def statusline_doctor(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as one JSON object."),
    ] = False,
) -> None:
    """Report which statusLine entry Claude Code uses here and whether it renders."""
    from eawf.runtime.runtimes.claude.statusline_install import diagnose_statusline

    report = diagnose_statusline(cwd=Path.cwd(), render=True)
    fields = dataclasses.asdict(report)
    flags: GlobalFlags = ctx.obj
    if json_output or flags.json_output:
        typer.echo(json.dumps(fields))
    else:
        for key, value in fields.items():
            shown = (
                "-" if value is None else str(value).lower() if isinstance(value, bool) else value
            )
            typer.echo(f"{key}: {shown}")
    if report.verdict == "broken":
        raise typer.Exit(code=exit_codes.USER_ERROR)


statusline_cli_app.command(name="install")(statusline_install)
statusline_cli_app.command(name="doctor")(statusline_doctor)


__all__ = [
    "statusline_cli_app",
    "statusline_doctor",
    "statusline_install",
]
