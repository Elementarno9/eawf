"""The MCP servers and grants an epoch-2 tree holds, read from its generation document.

A server is a ``capability`` row and a grant is a ``tool_authority`` row,
each keyed by the id it always had. A row is either one the cutover
imported, which wraps the epoch-1 row and stands at revision 1, or a
native row the ``mcp.*`` verbs wrote, which states its revision. Both are
read into the epoch-1 :class:`McpServer` and :class:`McpGrant` models, so
the installer, the listing and the doctor keep one shape.

A removal is a row too: a removed server or revoked grant stays under its
key at its next revision and reads as absent. Keeping it means a key is
never re-added at a revision an old anchor still names, and a grant id is
never handed out twice.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.native_records import ImportedNativeRecord
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.kernel.state.models import McpGrant, McpServer
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

#: The revision an imported row stands at: the cutover wrote it once.
IMPORTED_REVISION: Final = 1

#: The prefix of an allocated grant id.
GRANT_ID_PREFIX: Final = "GRANT-"

#: Whether a server row stands or was removed.
CapabilityStatus = Literal["registered", "removed"]

#: Whether a grant row stands or was revoked.
AuthorityStatus = Literal["granted", "revoked"]


class CapabilityRow(BaseModel):
    """One native ``capability`` row: a registered MCP server at one revision."""

    model_config = ConfigDict(extra="forbid")

    status: CapabilityStatus
    revision: StrictPositiveInt
    recorded_at: UtcDatetime
    server: McpServer


class ToolAuthorityRow(BaseModel):
    """One native ``tool_authority`` row: an MCP grant at one revision."""

    model_config = ConfigDict(extra="forbid")

    status: AuthorityStatus
    revision: StrictPositiveInt
    recorded_at: UtcDatetime
    grant: McpGrant


@dataclass(frozen=True)
class StandingServer:
    """One server as the document reads it back.

    Attributes:
        server: The server, or ``None`` when its row was removed.
        revision: The revision its row stands at.
    """

    server: McpServer | None
    revision: int


@dataclass(frozen=True)
class StandingGrant:
    """One grant as the document reads it back.

    Attributes:
        grant: The grant, or ``None`` when its row was revoked.
        revision: The revision its row stands at.
    """

    grant: McpGrant | None
    revision: int


class McpRowError(ValueError):
    """A ``capability`` or ``tool_authority`` row reads as neither shape it can take."""


def _imported_row(row: dict[str, Any], collection: Epoch2Collection, key: str) -> dict[str, Any]:
    """Return the epoch-1 row an imported record wraps.

    Raises:
        McpRowError: The row is not an imported record of *collection*.
    """
    try:
        record = ImportedNativeRecord.model_validate(row["payload"])
    except (KeyError, ValidationError) as error:
        raise McpRowError(f"{collection.value}/{key} is neither native nor imported") from error
    if record.target is not collection:
        raise McpRowError(f"{collection.value}/{key} was imported into {record.target.value}")
    return record.payload


def _server_of(key: str, row: Any) -> StandingServer:
    if isinstance(row, dict) and "server" in row:
        native = CapabilityRow.model_validate(row)
        return StandingServer(
            server=native.server if native.status == "registered" else None,
            revision=native.revision,
        )
    if not isinstance(row, dict):
        raise McpRowError(f"capability/{key} is not an object")
    body = _imported_row(row, Epoch2Collection.CAPABILITY, key)
    return StandingServer(server=McpServer.model_validate(body), revision=IMPORTED_REVISION)


def _grant_of(key: str, row: Any) -> StandingGrant:
    if isinstance(row, dict) and "grant" in row:
        native = ToolAuthorityRow.model_validate(row)
        return StandingGrant(
            grant=native.grant if native.status == "granted" else None,
            revision=native.revision,
        )
    if not isinstance(row, dict):
        raise McpRowError(f"tool_authority/{key} is not an object")
    body = _imported_row(row, Epoch2Collection.TOOL_AUTHORITY, key)
    return StandingGrant(grant=McpGrant.model_validate(body), revision=IMPORTED_REVISION)


def read_servers(document: dict[str, Any]) -> dict[str, StandingServer]:
    """Return every ``capability`` row of *document*, removed ones included.

    Raises:
        McpRowError: A row reads as neither an imported nor a native row.
        ValidationError: A native row violates its model.
    """
    rows = document_rows(document, Epoch2Collection.CAPABILITY)
    return {key: _server_of(key, rows[key]) for key in sorted(rows)}


def read_grants(document: dict[str, Any]) -> dict[str, StandingGrant]:
    """Return every ``tool_authority`` row of *document*, revoked ones included.

    Raises:
        McpRowError: A row reads as neither an imported nor a native row.
        ValidationError: A native row violates its model.
    """
    rows = document_rows(document, Epoch2Collection.TOOL_AUTHORITY)
    return {key: _grant_of(key, rows[key]) for key in sorted(rows)}


def registered(servers: dict[str, StandingServer]) -> dict[str, McpServer]:
    """Return the servers that stand, keyed by id."""
    return {key: s.server for key, s in servers.items() if s.server is not None}


def granted(grants: dict[str, StandingGrant]) -> dict[str, McpGrant]:
    """Return the grants that stand, keyed by id."""
    return {key: g.grant for key, g in grants.items() if g.grant is not None}


def capability_row(
    server: McpServer, *, status: CapabilityStatus, revision: int, at: datetime
) -> dict[str, Any]:
    """Return the native row *server* is stored as at *revision*."""
    row = CapabilityRow(status=status, revision=revision, recorded_at=at, server=server)
    return row.model_dump(mode="json")


def tool_authority_row(
    grant: McpGrant, *, status: AuthorityStatus, revision: int, at: datetime
) -> dict[str, Any]:
    """Return the native row *grant* is stored as at *revision*."""
    row = ToolAuthorityRow(status=status, revision=revision, recorded_at=at, grant=grant)
    return row.model_dump(mode="json")


def next_grant_id(existing: Iterable[str]) -> str:
    """Return ``GRANT-<n>`` one past the largest numbered id in *existing*.

    A custom id without a numeric suffix is passed over, so an operator's
    ``--grant-id`` never blocks allocation.
    """
    highest = 0
    for key in existing:
        suffix = key.removeprefix(GRANT_ID_PREFIX)
        if key.startswith(GRANT_ID_PREFIX) and suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{GRANT_ID_PREFIX}{highest + 1}"


def generation_document(tree_root: Path) -> Path | None:
    """Return the document of the generation *tree_root* selects.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        The document path, or ``None`` when the tree answers in epoch 1.
    """
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        return None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def tree_servers(tree_root: Path) -> dict[str, StandingServer] | None:
    """Return the servers an epoch-2 tree holds, or ``None`` for an epoch-1 tree.

    Raises:
        McpRowError: A row reads as neither an imported nor a native row.
        ValidationError: A native row violates its model.
    """
    document = generation_document(tree_root)
    if document is None:
        return None
    servers = read_servers(read_document(document))
    logger.debug(f"tree_servers rows={len(servers)}")
    return servers


__all__ = [
    "GRANT_ID_PREFIX",
    "IMPORTED_REVISION",
    "CapabilityRow",
    "McpRowError",
    "StandingGrant",
    "StandingServer",
    "ToolAuthorityRow",
    "capability_row",
    "generation_document",
    "granted",
    "next_grant_id",
    "read_grants",
    "read_servers",
    "registered",
    "tool_authority_row",
    "tree_servers",
]
