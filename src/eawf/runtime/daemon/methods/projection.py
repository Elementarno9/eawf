"""``projection.<route>.read`` and ``.reconnect``: a route's rows, and the way back.

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

``projection.<route>.reconnect`` is the same idea for a client that went away and
came back holding a cursor. It answers from retention alone: it walks the tree's
firehose once, so it knows both which ordinals it still holds and which keyed
patches those ordinals produce, and it hands back either the gap's patches or a
refusal naming the exact range it could not supply. A console that got a replay ends
at the daemon's cursor holding the same rows a fresh read would give it, which is
the equality the two paths are worth having.

The work runs off the event loop. It is a file read plus a pass over the rows it
holds, which is the shape of occupancy that pushes an unrelated ``daemon.ping`` past
its readiness budget when it runs on the loop. It takes no lock at all: a read model
built inside the commit lock would stall every writer on the root for the length of a
render, and the document a reader wants is the one the last commit left behind.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.compute import (
    CANONICAL_SEQUENCE_FIELD,
    ROUTE_COLLECTIONS,
    KeyedPatch,
    RouteProjection,
    build_route_projection,
    patches_for_event,
)
from eawf.kernel.projection.connection import (
    READ_METHOD_TEMPLATE,
    RECONNECT_METHOD_TEMPLATE,
    ReconnectDisposition,
    negotiate_reconnect,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.epoch2_root import RootIdentity
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from eawf.runtime.daemon.methods import DaemonValidationError, Handler, MethodContext, register
from eawf.runtime.daemon.native_guard import require_native_call

logger = logging.getLogger(__name__)


#: The stable code a projection read is refused with. A refused read means the tree
#: cannot be projected at all -- its high-water mark or one of its rows is not what
#: a projection is built from -- never that the route holds nothing.
PROJECTION_UNREADABLE: Final = "projection_unreadable"

#: The stable code a reconnect is refused with. Distinct from a ``snapshot_required``
#: answer, which is a successful negotiation: this code means the request itself
#: could not be negotiated, because the cursor it named is not one the tree could
#: ever have issued.
RECONNECT_UNNEGOTIABLE: Final = "projection_reconnect_unnegotiable"

#: The tree file the firehose's store directory is anchored on. The firehose keeps
#: its epoch-1 location, because it is the workspace's event log rather than a
#: generation's document.
_TREE_ANCHOR_FILENAME: Final = "state.json"


class ReconnectParams(BaseModel):
    """The parameters one reconnect request carries.

    Attributes:
        repo_root: The repository whose tree to answer for; the daemon's bound
            tree when absent.
        cursor: The ordinal the client last acknowledged; ``0`` for a client that
            has acknowledged nothing.
    """

    model_config = ConfigDict(extra="forbid")

    repo_root: str | None = None
    cursor: StrictNonNegativeInt = 0


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of a fence-cleared tree."""
    target, generation_id = authority.target, authority.generation_id
    assert target is not None, "an epoch-2 answer always carries its target"
    assert generation_id is not None, "an epoch-2 answer always names a generation"
    return target.generation_path(generation_id) / GENERATION_DOCUMENT


def _firehose_path(authority: RootAuthority) -> Path:
    """Return the firehose of a fence-cleared tree, where its commits are logged."""
    return store_path(authority.root / _TREE_ANCHOR_FILENAME, StoreKind.EVENT)


def _document_cursor(document: dict[str, Any]) -> int:
    """Return the committed ordinal a document stands at.

    Raises:
        DaemonValidationError: The document states its high-water mark as
            something other than an ordinal, so nothing it holds can be addressed
            by a cursor.
    """
    cursor = document.get(CANONICAL_SEQUENCE_KEY, 0)
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: the document states "
            f"{CANONICAL_SEQUENCE_KEY} as a {type(cursor).__name__}, not as the committed ordinal"
        )
    return cursor


def _project(*, route: str, authority: RootAuthority) -> RouteProjection:
    """Build one route's read model from the tree's committed document.

    Raises:
        DaemonValidationError: The document states its high-water mark as something
            other than an ordinal, or holds a row the route cannot render.
        FileNotFoundError: The selected generation carries no document.
    """
    document = read_document(_document_path(authority))
    try:
        return build_route_projection(
            route=route,
            document=document,
            cursor=_document_cursor(document),
            scope_id=RootIdentity.of(authority.root).root_id,
            generated_at=datetime.now(UTC),
        )
    except ValueError as error:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: {error}"
        ) from error


def _retained(path: Path, *, route: str) -> tuple[set[int], dict[int, tuple[KeyedPatch, ...]]]:
    """Return the ordinals retention holds and the patches they produce for *route*.

    One pass answers both questions, and they are different questions: an ordinal
    whose record no route renders is still retained, so leaving it out of the held
    set would refuse a reconnect that could have been replayed.

    A line that does not parse, or a transition that states too little to patch, is
    logged and left out of the held set rather than raised. That is the safe
    direction: the ordinal becomes one retention cannot supply, so a gap covering
    it negotiates to ``snapshot_required`` instead of replaying around a hole.

    Args:
        path: The tree's firehose.
        route: The console route whose patches to keep.

    Returns:
        The held ordinals, and the route's patches keyed by ordinal.
    """
    held: set[int] = set()
    patches: dict[int, tuple[KeyedPatch, ...]] = {}
    if not path.exists():
        return held, patches
    with path.open("rb") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = Envelope.model_validate(orjson.loads(line))
            except (orjson.JSONDecodeError, ValidationError, ValueError) as error:
                logger.warning(f"_retained skipped an unreadable firehose row cause={error!s}")
                continue
            sequence = envelope.payload.get(CANONICAL_SEQUENCE_FIELD)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                continue
            try:
                produced = patches_for_event(envelope)
            except ValueError as error:
                logger.warning(f"_retained skipped an unpatchable row cause={error!s}")
                continue
            held.add(sequence)
            for_route = tuple(patch for patch in produced if route in patch.routes)
            if for_route:
                patches[sequence] = for_route
    return held, patches


def _reconnect(*, route: str, authority: RootAuthority, cursor: int) -> dict[str, Any]:
    """Negotiate one client's return and, on a replay, carry the gap's patches.

    Raises:
        DaemonValidationError: The document cannot be read through a cursor, or
            the client's cursor is one this tree could never have issued.
    """
    server_cursor = _document_cursor(read_document(_document_path(authority)))
    held, patches = _retained(_firehose_path(authority), route=route)
    try:
        negotiation = negotiate_reconnect(
            route=route,
            client_cursor=cursor,
            server_cursor=server_cursor,
            retained=held,
        )
    except ValueError as error:
        raise DaemonValidationError(
            f"validation_failed: {RECONNECT_UNNEGOTIABLE}: {error}"
        ) from error
    replayed: tuple[KeyedPatch, ...] = ()
    if negotiation.disposition is ReconnectDisposition.REPLAY and negotiation.gap is not None:
        replayed = tuple(
            patch for sequence in negotiation.gap.sequences() for patch in patches.get(sequence, ())
        )
    return {
        "negotiation": negotiation.model_dump(mode="json"),
        "patches": [patch.model_dump(mode="json") for patch in replayed],
    }


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


def _route_reconnector(route: str) -> Handler:
    """Return the handler that negotiates one route's reconnect, bound to that route."""

    async def reconnect_route(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        """Negotiate a replay or a snapshot for a client returning at its cursor.

        Args:
            ctx: Server context, whose bound state path is the tree fallback.
            params: The request parameters, validated as :class:`ReconnectParams`.

        Returns:
            The negotiation as a JSON-mode mapping, beside the gap's keyed patches
            when the disposition is a replay and an empty list otherwise.

        Raises:
            NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
            DaemonValidationError: The parameters are not a reconnect request, or
                the tree cannot be negotiated with at that cursor.
        """
        try:
            args = ReconnectParams.model_validate(params)
        except ValidationError as error:
            raise DaemonValidationError(
                f"validation_failed: {RECONNECT_UNNEGOTIABLE}: {error.error_count()} bad "
                f"parameter(s) for {RECONNECT_METHOD_TEMPLATE.format(route=route)}"
            ) from error
        authority = require_native_call(ctx, params)
        answer = await asyncio.to_thread(
            _reconnect, route=route, authority=authority, cursor=args.cursor
        )
        logger.debug(
            f"reconnect_route route={route} cursor={args.cursor} "
            f"disposition={answer['negotiation']['disposition']} patches={len(answer['patches'])}"
        )
        return answer

    reconnect_route.__name__ = f"reconnect_{route.replace('.', '_')}"
    return reconnect_route


def _register_route_verbs(template: str, build: Callable[[str], Handler]) -> tuple[str, ...]:
    """Register one verb per bound route and return the names, sorted.

    Args:
        template: The wire-name template, formatted with the route key.
        build: The handler factory for one route.

    Returns:
        The registered names, in route order.
    """
    names: list[str] = []
    for route in sorted(ROUTE_COLLECTIONS):
        name = template.format(route=route)
        register(name)(build(route))
        names.append(name)
    return tuple(names)


#: The read verbs this module registered, one per bound route.
ROUTE_READ_METHODS: Final[tuple[str, ...]] = _register_route_verbs(
    READ_METHOD_TEMPLATE, _route_reader
)

#: The reconnect verbs this module registered, one per bound route. A route with no
#: document binding has neither verb: a console cannot reconnect to a projection
#: that was never served.
ROUTE_RECONNECT_METHODS: Final[tuple[str, ...]] = _register_route_verbs(
    RECONNECT_METHOD_TEMPLATE, _route_reconnector
)


__all__ = [
    "PROJECTION_UNREADABLE",
    "READ_METHOD_TEMPLATE",
    "RECONNECT_METHOD_TEMPLATE",
    "RECONNECT_UNNEGOTIABLE",
    "ROUTE_READ_METHODS",
    "ROUTE_RECONNECT_METHODS",
    "ReconnectParams",
]
