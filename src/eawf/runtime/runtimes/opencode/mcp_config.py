"""How opencode is told about one Run's semantic tool server.

opencode reads one JSON configuration document and takes no per-call
tool flag, which is why the spawn path records an opencode deny as
jail-backed rather than argv-backed. The per-Run server therefore lands
in a configuration file of its own, pointed at by ``OPENCODE_CONFIG`` so
the operator's own document is never rewritten.

The document carries both halves of the boundary: the local server that
publishes exactly the Run's granted tools, and a ``tools`` map pinning
every ambient vendor tool false. On this lane that map is the only place
the ambient denial can be spelled, so it is the one this repo writes
rather than a flag it wishes existed.
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
RUNTIME_ID = "opencode"

#: The file name the per-Run configuration document is written under.
CONFIG_FILENAME = "opencode.json"

#: The environment variable opencode reads its configuration path from.
CONFIG_ENV_VAR = "OPENCODE_CONFIG"


def render_run_server_config(
    binding: RunServerBinding,
    *,
    config_dir: Path,
    server_command: tuple[str, ...],
    capsule: AuthorityCapsule | None = None,
) -> RunServerConfig:
    """Return the opencode configuration of one Run's server.

    Args:
        binding: The Run the server answers for.
        config_dir: Where the configuration document is written.
        server_command: The argv that starts the per-Run stdio server.
        capsule: The Run's sealed capsule, read from *binding* when not
            supplied.

    Returns:
        The lane configuration: one document, the environment entry that
        points at it, and the tool names the grant resolves to.

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
        "mcp": {
            SERVER_NAME: {
                "type": "local",
                "command": list(server_command),
                "enabled": True,
            }
        },
        "tools": dict.fromkeys(ambient_tool_denial(), False),
    }
    path = config_dir / CONFIG_FILENAME
    logger.info(f"render_run_server_config runtime={RUNTIME_ID} tools={len(exposed)}")
    return RunServerConfig(
        runtime_id=RUNTIME_ID,
        files={str(path): json.dumps(document, indent=2, sort_keys=True) + "\n"},
        env={CONFIG_ENV_VAR: str(path)},
        exposed_tools=exposed,
    )


__all__ = ["CONFIG_ENV_VAR", "CONFIG_FILENAME", "RUNTIME_ID", "render_run_server_config"]
