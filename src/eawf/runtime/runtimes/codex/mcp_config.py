"""How Codex is told about one Run's semantic tool server.

Codex takes its whole configuration as TOML, spelled here as the
``-c <dotted.key>=<toml value>`` overrides codex itself accepts. The
launcher folds them into the config file of the Run's private Codex home,
so the operator's own ``config.toml`` is neither read nor written.

Codex has no per-tool allowlist for MCP tools, so the positive grant is
carried by the server itself: it publishes exactly the Run's granted
tools and the daemon refuses anything else whatever a provider asks for.
What the lane configuration adds is the registration and the ambient
denial -- every codex setting that withholds a vendor tool, from
:func:`~eawf.runtime.runtimes.codex.adapter.codex_tool_denials`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.runtime.mcp.semantic_stdio import (
    RunServerBinding,
    RunServerConfig,
    ambient_tool_denial,
    mcp_tool_name,
)
from eawf.runtime.runtimes.codex.adapter import codex_tool_denials

logger = logging.getLogger(__name__)


#: The lane this module configures.
RUNTIME_ID = "codex"

#: The dotted config namespace one MCP server is registered under. Codex
#: keys a server by a bare TOML name, so the hyphen in the wire-level
#: server name is spelled as an underscore here.
SERVER_CONFIG_KEY = "mcp_servers.eawf_semantic"


def render_run_server_config(
    binding: RunServerBinding,
    *,
    config_dir: Path,
    server_command: tuple[str, ...],
    capsule: AuthorityCapsule | None = None,
) -> RunServerConfig:
    """Return the Codex configuration of one Run's server.

    Args:
        binding: The Run the server answers for.
        config_dir: Unused by this lane, whose file the launcher writes.
            It is accepted so every lane renderer has one signature.
        server_command: The argv that starts the per-Run stdio server.
        capsule: The Run's sealed capsule, read from *binding* when not
            supplied.

    Returns:
        The lane configuration: ``-c`` overrides only, and the tool names
        the grant resolves to.

    Raises:
        ValueError: *server_command* is empty, so the override would
            register a server with no program to run.
        OSError: The capsule file cannot be read.
        pydantic.ValidationError: The capsule is not sealed.
    """
    if not server_command:
        raise ValueError("a per-run server configuration names the program that serves it")
    resolved = capsule if capsule is not None else binding.capsule()
    exposed = tuple(mcp_tool_name(tool) for tool in resolved.semantic_tools)
    overrides: list[str] = [
        "-c",
        f"{SERVER_CONFIG_KEY}.command={json.dumps(server_command[0])}",
        "-c",
        f"{SERVER_CONFIG_KEY}.args={json.dumps(list(server_command[1:]))}",
        "-c",
        f"{SERVER_CONFIG_KEY}.enabled=true",
        # A headless Run runs under approval_policy "never", where codex
        # rejects any MCP call it would otherwise prompt for. The server
        # already publishes only the granted tools and the daemon refuses
        # the rest, so its tools are approved up front.
        "-c",
        f'{SERVER_CONFIG_KEY}.default_tools_approval_mode="approve"',
    ]
    overrides.extend(codex_tool_denials(ambient_tool_denial()))
    logger.info(f"render_run_server_config runtime={RUNTIME_ID} tools={len(exposed)}")
    return RunServerConfig(
        runtime_id=RUNTIME_ID,
        argv_flags=tuple(overrides),
        exposed_tools=exposed,
    )


__all__ = ["RUNTIME_ID", "SERVER_CONFIG_KEY", "render_run_server_config"]
