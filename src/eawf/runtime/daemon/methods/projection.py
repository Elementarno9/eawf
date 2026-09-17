"""``projection.<route>.read``: one console route's read model at the tree's cursor.

A console used to project the whole document itself, which meant every surface
re-derived the same rows and none of them could say which cursor its answer stood
at. The daemon allocates the only workspace-global order, so it is the one place
that can answer both at once: these verbs read the selected generation's document,
build the route's read model through the committed ``canonical_sequence``, and hand
back a header stating that cursor.

One verb is registered per route :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`
binds, so a route with no document binding is not found rather than answered with an
empty projection. The read sits behind the epoch-2 fence: a tree that has not been
shown to hold native authority has no document to read.

The work runs off the event loop. It is a file read plus a pass over the rows it
holds, which is the shape of occupancy that pushes an unrelated ``daemon.ping`` past
its readiness budget when it runs on the loop. It takes no lock at all: a read model
built inside the commit lock would stall every writer on the root for the length of a
render, and the document a reader wants is the one the last commit left behind.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.store.compaction import read_document
from eawf.runtime.daemon.epoch2_root import RootIdentity
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from eawf.runtime.daemon.methods import DaemonValidationError, Handler, MethodContext, register
from eawf.runtime.daemon.native_guard import require_native_call

logger = logging.getLogger(__name__)


#: How a route's read verb is spelled on the wire.
READ_METHOD_TEMPLATE: Final = "projection.{route}.read"

#: The stable code a projection read is refused with. A refused read means the tree
#: cannot be projected at all -- its high-water mark or one of its rows is not what
#: a projection is built from -- never that the route holds nothing.
PROJECTION_UNREADABLE: Final = "projection_unreadable"


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of a fence-cleared tree."""
    target, generation_id = authority.target, authority.generation_id
    assert target is not None, "an epoch-2 answer always carries its target"
    assert generation_id is not None, "an epoch-2 answer always names a generation"
    return target.generation_path(generation_id) / GENERATION_DOCUMENT


def _project(*, route: str, authority: RootAuthority) -> RouteProjection:
    """Build one route's read model from the tree's committed document.

    Raises:
        DaemonValidationError: The document states its high-water mark as something
            other than an ordinal, or holds a row the route cannot render.
        FileNotFoundError: The selected generation carries no document.
    """
    document = read_document(_document_path(authority))
    cursor = document.get(CANONICAL_SEQUENCE_KEY, 0)
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: the document states "
            f"{CANONICAL_SEQUENCE_KEY} as a {type(cursor).__name__}, not as the committed ordinal"
        )
    try:
        return build_route_projection(
            route=route,
            document=document,
            cursor=cursor,
            scope_id=RootIdentity.of(authority.root).root_id,
            generated_at=datetime.now(UTC),
        )
    except ValueError as error:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: {error}"
        ) from error


def _route_reader(route: str) -> Handler:
    """Return the handler that reads one route, bound to that route's key."""

    async def read_route(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        """Return the route's read model at the addressed tree's committed cursor.

        Args:
            ctx: Server context, whose bound state path is the tree fallback.
            params: The request parameters; ``repo_root`` names the tree when the
                caller does not want the one the daemon is bound to.

        Returns:
            The route projection as a JSON-mode mapping.

        Raises:
            NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
            DaemonValidationError: The tree cannot be projected.
        """
        authority = require_native_call(ctx, params)
        projection = await asyncio.to_thread(_project, route=route, authority=authority)
        logger.debug(f"read_route route={route} cursor={projection.header.source_cursor}")
        return projection.model_dump(mode="json")

    read_route.__name__ = f"read_{route.replace('.', '_')}"
    return read_route


def _register_route_reads() -> tuple[str, ...]:
    """Register one read verb per bound route and return the names, sorted."""
    names: list[str] = []
    for route in sorted(ROUTE_COLLECTIONS):
        name = READ_METHOD_TEMPLATE.format(route=route)
        register(name)(_route_reader(route))
        names.append(name)
    return tuple(names)


#: The read verbs this module registered, one per bound route.
ROUTE_READ_METHODS: Final[tuple[str, ...]] = _register_route_reads()


__all__ = [
    "PROJECTION_UNREADABLE",
    "READ_METHOD_TEMPLATE",
    "ROUTE_READ_METHODS",
]
