"""How Claude Code is told about one Run's semantic tool server.

Claude takes MCP servers as a JSON document and tool grants as argv
flags, so the lane's configuration is one file plus three flags. The file
registers the per-Run stdio server; ``--strict-mcp-config`` drops every
other MCP source on the host, so a server the operator installed for
themselves is not silently inherited by a Run.

The grant is spelled twice on purpose. The stdio server publishes exactly
the Run's granted tools, which is the enforced half; the allow flag names
the same set to Claude, which is what makes a reviewer able to read the
grant off the spawn without starting the server. The two derive from one
capsule, so they cannot disagree.

Ambient tools are denied whatever the grant is. A Run reaches its
repository through the brokered catalog or not at all, and a checked
grant beside an unchecked shell is not a boundary.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.runtime.mcp.semantic_stdio import (
    SERVER_NAME,
    RunServerBinding,
    RunServerConfig,
    ambient_tool_denial,
    mcp_tool_name,
)

logger = logging.getLogger(__name__)


#: The lane this module configures.
RUNTIME_ID = "claude-code"

#: The file name the per-Run MCP document is written under.
MCP_CONFIG_FILENAME = "mcp.json"


def render_run_server_config(
    binding: RunServerBinding,
    *,
    config_dir: Path,
    server_command: tuple[str, ...],
    capsule: AuthorityCapsule | None = None,
) -> RunServerConfig:
    """Return the Claude Code configuration of one Run's server.

    Args:
        binding: The Run the server answers for.
        config_dir: Where the MCP document is written.
        server_command: The argv that starts the per-Run stdio server.
        capsule: The Run's sealed capsule, read from *binding* when not
            supplied. The parameter exists so a caller that already holds
            the capsule does not re-read it from disk.

    Returns:
        The lane configuration: one MCP document, the flags that bind it,
        and the tool names the grant resolves to.

    Raises:
        ValueError: *server_command* is empty, so the document would
            register a server with no program to run.
        OSError: The capsule file cannot be read.
        pydantic.ValidationError: The capsule is not sealed.
    """
    if not server_command:
        raise ValueError("a per-run server configuration names the program that serves it")
    resolved = capsule if capsule is not None else binding.capsule()
    exposed = tuple(mcp_tool_name(tool) for tool in resolved.semantic_tools)
    document = {
        "mcpServers": {
            SERVER_NAME: {
                "command": server_command[0],
                "args": list(server_command[1:]),
                "env": {},
            }
        }
    }
    path = config_dir / MCP_CONFIG_FILENAME
    allow = ("--allowedTools", " ".join(exposed)) if exposed else ()
    logger.info(f"render_run_server_config runtime={RUNTIME_ID} tools={len(exposed)}")
    return RunServerConfig(
        runtime_id=RUNTIME_ID,
        files={str(path): json.dumps(document, indent=2, sort_keys=True) + "\n"},
        argv_flags=(
            "--mcp-config",
            str(path),
            "--strict-mcp-config",
            *allow,
            "--disallowedTools",
            " ".join(ambient_tool_denial()),
        ),
        exposed_tools=exposed,
    )


__all__ = ["MCP_CONFIG_FILENAME", "RUNTIME_ID", "render_run_server_config"]
