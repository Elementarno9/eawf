"""MCP server install / update / remove primitives for Eä.

The CLI surface (`eawf mcp ...`) is in
:mod:`eawf.surfaces.cli.commands.mcp`; this package houses the runtime-config
write logic, the env-ref token parser, and the (currently empty)
v0.1 known-MCP catalog.

Design contract (Phase 5 W04):

- ``state.mcp_servers`` is the canonical persistence location. The
  CLI writes there via :func:`eawf.surfaces.cli._mutation.state_transaction`,
  not via direct file writes.
- The runtime config (Claude Code's ``.claude/settings.json``) is
  patched in-place: the Eä-owned region is the union of map keys
  whose value carries ``__eawf_owner == "eawf"``. Every other
  ``mcpServers`` key is left byte-equal across install / update /
  remove. User-installed entries Eä never touches.
- ``${ENV:NAME}`` tokens stay literal on disk. Expansion is the MCP
  launcher's responsibility at spawn time. The :mod:`mcp.installer`
  module never reads ``os.environ``.
"""

from __future__ import annotations

from eawf.runtime.mcp.catalog import KNOWN_MCPS, McpCatalogEntry
from eawf.runtime.mcp.env_ref import (
    ENV_REF_RE,
    InvalidEnvRef,
    assert_no_expansion,
    parse_env_ref,
    render_env_block,
)
from eawf.runtime.mcp.installer import (
    InstallEntryResult,
    RemoveEntryResult,
    RuntimeEntry,
    install_runtime_entry,
    list_runtime_entries,
    remove_runtime_entry,
)

__all__ = [
    "ENV_REF_RE",
    "KNOWN_MCPS",
    "InstallEntryResult",
    "InvalidEnvRef",
    "McpCatalogEntry",
    "RemoveEntryResult",
    "RuntimeEntry",
    "assert_no_expansion",
    "install_runtime_entry",
    "list_runtime_entries",
    "parse_env_ref",
    "remove_runtime_entry",
    "render_env_block",
]
