"""Eä-known MCP catalog (empty in v0.1).

The catalog is the well-known set of MCP servers Eä can register
without the user supplying every flag. v0.1.1 will populate it.
v0.1 ships with an empty tuple so ``eawf mcp add --from-catalog`` can
be added in v0.1.1 without surface churn.

Per ``docs/architecture/plugins.md`` MCP-catalog defaults, non-env
secret backends (1Password, sops, age) are deferred — every catalog
entry, when added, must declare its env-refs as ``${ENV:NAME}`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eawf.state.enums import McpRisk


@dataclass(frozen=True)
class McpCatalogEntry:
    """One known-MCP recipe.

    Attributes:
        id: Stable catalog ID (e.g. ``"filesystem"``, ``"github"``).
            The CLI uses this as the default ``state.mcp_servers`` key
            when ``--from-catalog`` is passed (v0.1.1+).
        command: Launcher binary (e.g. ``"npx"``,
            ``"/usr/local/bin/mcp-fs"``).
        default_args: ``argv[1:]`` for the launcher.
        default_env_refs: Canonical ``${ENV:NAME}`` tokens. Empty for
            servers that need no secrets.
        risk: One of :class:`eawf.state.enums.McpRisk`.
        write_capable: ``True`` if the server can mutate user state
            (filesystem writes, GitHub writes, ...). Drives the
            ask-before-install gate's risk wording.
        description: Short human-readable blurb; surfaced by ``eawf
            mcp list --catalog``.
    """

    id: str
    command: str
    default_args: tuple[str, ...] = field(default_factory=tuple)
    default_env_refs: tuple[str, ...] = field(default_factory=tuple)
    risk: McpRisk = McpRisk.READ
    write_capable: bool = False
    description: str = ""


# v0.1 ships intentionally empty (plan §9 line 461). Do not invent
# entries — populating the catalog is a v0.1.1 deliverable that must
# include UX review and per-entry security review.
KNOWN_MCPS: tuple[McpCatalogEntry, ...] = ()


__all__ = ["KNOWN_MCPS", "McpCatalogEntry"]
