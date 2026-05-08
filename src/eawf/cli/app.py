from __future__ import annotations

import typer

from eawf import __version__

app = typer.Typer(
    name="eawf",
    help="Eä Workflow — agent-driven development framework (v0.1 in development).",
    no_args_is_help=False,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the eawf version and exit.",
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(f"eawf {__version__}")
        typer.echo("v0.1 in development; see eawf-v0.1-plan.md")


def main() -> None:
    app()
