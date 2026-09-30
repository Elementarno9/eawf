"""The ``eawf mcp`` per-Run semantic tool server: ``serve`` and ``run-config``.

Split out of :mod:`eawf.surfaces.cli.commands.mcp`, whose :data:`mcp_app`
these commands attach to. Neither writes a tree record: ``serve`` forwards
each tool call to the daemon and ``run-config`` renders a lane's config.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.mcp import mcp_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.runtime.mcp.semantic_stdio import RunServerBinding
    from eawf.surfaces.cli._daemon_client import DaemonClient

logger = logging.getLogger(__name__)


#: The canonical lane ids ``run-config`` renders a per-Run server for.
#: These are the adapter ids, not the short installer names above: the
#: per-Run server is configured for a dispatch lane, and the lane is what
#: :func:`eawf.runtime.runtimes.selector.adapter_for` is keyed by.
_RUN_SERVER_RUNTIMES: tuple[str, ...] = ("claude-code", "codex", "opencode")


class _DaemonTransport:
    """The per-Run server's one way of reaching the daemon.

    A thin binding rather than a class with behaviour: the server owns
    what a call means and this owns only how it travels, which is what
    lets a test drive the same server over an in-process dispatcher.
    """

    def __init__(self, client: DaemonClient) -> None:
        """Bind the transport to an already-connected client."""
        self._client = client

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        """Forward one request and return the daemon's result."""
        return self._client.call(method, dict(params))


def _run_server_binding(path: Path) -> RunServerBinding:
    """Return the validated per-Run server binding filed at *path*.

    Raises:
        cli_errors.UserError: The file is absent or is not a binding.
    """
    from eawf.runtime.mcp.semantic_stdio import RunServerBinding

    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise cli_errors.UserError(str(error), kind="NotFound") from error
    except json.JSONDecodeError as error:
        raise cli_errors.UserError(f"{path} is not JSON: {error}") from error
    try:
        return RunServerBinding.model_validate(body)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise cli_errors.UserError(
            f"{path} is not a run server binding; check {', '.join(fields)}"
        ) from error


@mcp_app.command(name="serve")
def serve_cmd(
    ctx: typer.Context,
    binding_path: Annotated[
        Path,
        typer.Option("--binding", help="Per-Run server binding written by the dispatcher."),
    ],
) -> None:
    """Serve one Run's granted semantic tools over MCP stdio.

    The provider process starts this command; it publishes exactly the
    tools the Run's sealed capsule grants and forwards every call to the
    daemon. Nothing but MCP frames is written to stdout, because stdout
    is the protocol channel.
    """
    from eawf.runtime.mcp.semantic_stdio import SemanticStdioServer, serve_semantic_stdio
    from eawf.surfaces.cli._daemon_client import DaemonClient

    flags: GlobalFlags = ctx.obj
    try:
        binding = _run_server_binding(binding_path)
        capsule = binding.capsule()
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    except (OSError, ValidationError) as err:
        cli_errors.emit_error(cli_errors.UserError(str(err)), flags=flags)
        return
    runtime_dir = Path(binding.runtime_dir) if binding.runtime_dir else None
    with DaemonClient(runtime_dir=runtime_dir) as client:
        server = SemanticStdioServer(
            binding=binding, capsule=capsule, transport=_DaemonTransport(client)
        )
        serve_semantic_stdio(server, stdin=sys.stdin, stdout=sys.stdout)


@mcp_app.command(name="run-config")
def run_config_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="Dispatch lane: claude-code | codex | opencode."),
    ],
    binding_path: Annotated[
        Path,
        typer.Option("--binding", help="Per-Run server binding written by the dispatcher."),
    ],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directory the lane's config files are written to."),
    ],
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the lane's config files to --config-dir."),
    ] = True,
) -> None:
    """Render how one lane is told about a Run's semantic tool server.

    The dispatcher runs this before spawning a provider: the rendered
    flags and environment go onto the spawn, and the written files carry
    the server registration. The exposed tool names are the Run's grant
    as the lane will see it, so a reviewer reads the grant off the spawn.
    """
    from eawf.runtime.mcp.semantic_stdio import (
        ServerTableError,
        materialize_run_server_config,
        run_server_config_for,
    )

    flags: GlobalFlags = ctx.obj
    try:
        if runtime not in _RUN_SERVER_RUNTIMES:
            raise cli_errors.UserError(
                f"unknown lane {runtime!r}; expected one of {list(_RUN_SERVER_RUNTIMES)}"
            )
        binding = _run_server_binding(binding_path)
        config = run_server_config_for(
            runtime,
            binding=binding,
            config_dir=config_dir.resolve(),
            server_command=("eawf", "mcp", "serve", "--binding", str(binding_path.resolve())),
        )
        written = materialize_run_server_config(config) if write else ()
        emit_json_or_text(
            payload={
                "runtime_id": config.runtime_id,
                "argv_flags": list(config.argv_flags),
                "env": dict(config.env),
                "exposed_tools": list(config.exposed_tools),
                "files_written": list(written),
            },
            text=(
                f"mcp run-config: {config.runtime_id} "
                f"tools={len(config.exposed_tools)} files={len(written)}"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except (OSError, ValidationError, ServerTableError) as err:
        cli_errors.emit_error(cli_errors.UserError(str(err)), flags=flags)
