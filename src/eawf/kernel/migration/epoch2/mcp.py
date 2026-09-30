"""The MCP population, reconciled across the servers and the grants that name them.

Epoch 1 kept the MCP servers a tree registered in ``mcp_servers`` and the
scopes each was granted to in ``mcp_grants``. Epoch 2 keeps the same two
facts natively: a server is a ``capability`` row and a grant is a
``tool_authority`` row, each under the key it already had, because a grant
names its server by that key.

The counts prove nothing was lost or widened on the way: each collection
lands one record per source row, and every imported grant names a server
the import also wrote. A grant naming a server the source never held
would authorise a tool nobody registered, so it refuses the import.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)

#: The epoch-1 collection an MCP server row is read from.
MCP_SERVERS_SOURCE: Final = "mcp_servers"

#: The epoch-1 collection an MCP grant row is read from.
MCP_GRANTS_SOURCE: Final = "mcp_grants"

#: The grant field that names the server it authorises.
GRANT_SERVER_FIELD: Final = "server_id"


class McpImportCensus(StrictMigrationModel):
    """The MCP population, counted on both sides of the import.

    Attributes:
        server_rows: Rows in the ``mcp_servers`` collection.
        capability_records: ``capability`` records the import writes.
        grant_rows: Rows in the ``mcp_grants`` collection.
        tool_authority_records: ``tool_authority`` records the import writes.
        unresolved_grants: Imported grants whose server the import did not
            write; a balanced census holds none.
    """

    server_rows: Annotated[int, Field(ge=0)]
    capability_records: Annotated[int, Field(ge=0)]
    grant_rows: Annotated[int, Field(ge=0)]
    tool_authority_records: Annotated[int, Field(ge=0)]
    unresolved_grants: Annotated[int, Field(ge=0)]

    @classmethod
    def build(
        cls,
        *,
        server_ids: Iterable[str],
        grants: Mapping[str, Mapping[str, Any]],
        capability_keys: Iterable[str],
        tool_authority_keys: Iterable[str],
    ) -> McpImportCensus:
        """Count the source rows beside the records imported from them.

        Args:
            server_ids: Keys of the ``mcp_servers`` collection.
            grants: The ``mcp_grants`` rows, keyed by grant id.
            capability_keys: Keys of the ``capability`` records written.
            tool_authority_keys: Keys of the ``tool_authority`` records written.

        Returns:
            The census; :func:`check_mcp_import` proves it balances.
        """
        capabilities = frozenset(capability_keys)
        authorities = frozenset(tool_authority_keys)
        unresolved = sum(
            1
            for key in authorities
            if key in grants and grants[key].get(GRANT_SERVER_FIELD) not in capabilities
        )
        return cls(
            server_rows=len(frozenset(server_ids)),
            capability_records=len(capabilities),
            grant_rows=len(grants),
            tool_authority_records=len(authorities),
            unresolved_grants=unresolved,
        )


def check_mcp_import(census: McpImportCensus) -> None:
    """Refuse an MCP import whose counts do not reconcile.

    Raises:
        MigrationCountMismatchError: When a collection lands a record count
            other than its source row count, or an imported grant names a
            server the import did not write.
    """
    if census.capability_records != census.server_rows:
        raise MigrationCountMismatchError(
            f"the import writes {census.capability_records} capability records for "
            f"{census.server_rows} mcp_servers rows"
        )
    if census.tool_authority_records != census.grant_rows:
        raise MigrationCountMismatchError(
            f"the import writes {census.tool_authority_records} tool_authority records for "
            f"{census.grant_rows} mcp_grants rows"
        )
    if census.unresolved_grants:
        raise MigrationCountMismatchError(
            f"{census.unresolved_grants} imported grants name a server the import did not "
            "write, which would authorise a tool nobody registered"
        )


def mcp_import_rule_payload() -> dict[str, Any]:
    """Return the digestable statement of the MCP reconciliation."""
    return {
        "sources": [MCP_SERVERS_SOURCE, MCP_GRANTS_SOURCE],
        "grant_server_field": GRANT_SERVER_FIELD,
        "manifest_fields": [
            "server_rows",
            "capability_records",
            "grant_rows",
            "tool_authority_records",
            "unresolved_grants",
        ],
    }


__all__ = [
    "GRANT_SERVER_FIELD",
    "MCP_GRANTS_SOURCE",
    "MCP_SERVERS_SOURCE",
    "McpImportCensus",
    "check_mcp_import",
    "mcp_import_rule_payload",
]
