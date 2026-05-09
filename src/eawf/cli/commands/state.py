"""``eawf state`` — read-only state introspection.

Currently exposes a single subcommand:

- ``eawf state resolve [--workspace <path>]`` — print the resolved
  ``state.json`` path and the *reason* it was selected
  (``env`` / ``workspace_flag`` / ``pwd_upward``).

The resolver itself lives in :mod:`eawf.state.resolve` so other waves (e.g.
``status``, ``store compact``) reuse it without depending on the CLI layer.
The command never mutates state and never acquires a lock.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.state.resolve import resolve_with_reason

logger = logging.getLogger(__name__)

state_app = typer.Typer(
    name="state",
    help="Read-only state introspection (resolve, info).",
    no_args_is_help=True,
    add_completion=False,
)


@state_app.command(name="resolve")
def resolve_cmd(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root to anchor the resolver (overrides pwd-upward).",
        ),
    ] = None,
) -> None:
    """Print the resolved ``state.json`` path and the reason for selection."""
    flags: GlobalFlags = ctx.obj
    effective_ws = workspace if workspace is not None else flags.workspace
    path, reason = resolve_with_reason(workspace=effective_ws)
    payload: dict[str, str] = {"path": str(path), "reason": reason}
    emit_json_or_text(
        dict(payload),
        f"{path}\nreason: {reason}",
        flags=flags,
    )
