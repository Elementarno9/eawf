"""``projection.<route>.read`` and ``.reconnect``: a route's rows, and the way back.

A surface that projects the whole document itself re-derives rows every other
surface already built, and cannot say which cursor its answer stands at. The
daemon allocates the only workspace-global order, so it is the one place
that can answer both at once: these verbs read the selected generation's document,
build the route's read model through the committed ``canonical_sequence``, and hand
back a header stating that cursor.

One verb is registered per route :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`
binds, so a route with no document binding is not found rather than answered with an
empty projection. The read sits behind the epoch-2 fence: a tree that has not been
shown to hold native authority has no document to read.

``projection.settings.read`` is the one verb that answers from outside the document.
Settings are layered config rather than records, so the route binds no collection and
gets no reconnect verb -- there is no ordinal to replay from. It is served here anyway,
because a console that had to reach for a second surface to draw one of its routes would
have two answers to "what is in force" and no way to say which read is older.

``projection.<route>.reconnect`` is the same idea for a client that went away and
came back holding a cursor. It answers from retention alone: it walks the tree's
firehose once, so it knows both which ordinals it still holds and which keyed
patches those ordinals produce, and it hands back either the gap's patches or a
refusal naming the exact range it could not supply. A console that got a replay ends
at the daemon's cursor holding the same rows a fresh read would give it, which is
the equality the two paths are worth having.

``projection.export.report`` renders one acceptance route's read model as plain text
at the digest that view was read through. It is a read like the others: it opens the
document, renders what it found and hands the bytes back, so exporting allocates no
ordinal, writes no record and leaves the tree exactly as it was. Two reports taken at
one cursor are byte-identical and name one digest, which is what lets a terminal and a
plain-text reader compare what each was shown.

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
from typing import Any, Final, Self

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

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
from eawf.kernel.projection.settings import SETTINGS_ROUTE, SettingsView, build_settings_view
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path, store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootIdentity
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from eawf.runtime.daemon.methods import DaemonValidationError, Handler, MethodContext, register
from eawf.runtime.daemon.native_guard import require_native_call
from eawf.workflow.projection.acceptance import (
    ACCEPTANCE_ROUTES,
    MILESTONE_ROUTE,
    ExportReport,
    build_acceptance_view,
    export_report,
)

logger = logging.getLogger(__name__)


#: The stable code a projection read is refused with. A refused read means the tree
#: cannot be projected at all -- its high-water mark or one of its rows is not what
#: a projection is built from -- never that the route holds nothing.
PROJECTION_UNREADABLE: Final = "projection_unreadable"

#: The stable code an export is refused with. An export that cannot name an acceptance
#: route is refused rather than answered with another route's report, because the
#: digest it would carry addresses rows the caller did not ask for.
EXPORT_UNREPORTABLE: Final = "projection_export_unreportable"

#: The stable code a reconnect is refused with. Distinct from a ``snapshot_required``
#: answer, which is a successful negotiation: this code means the request itself
#: could not be negotiated, because the cursor it named is not one the tree could
#: ever have issued.
RECONNECT_UNNEGOTIABLE: Final = "projection_reconnect_unnegotiable"

#: The tree file the firehose's store directory is anchored on. The firehose keeps
#: its epoch-1 location, because it is the workspace's event log rather than a
#: generation's document.
_TREE_ANCHOR_FILENAME: Final = "state.json"

#: How many of a collection's most recent ledger-held rows a route reads beside
#: its live document rows. A repository's ledger holds every record it ever
#: closed, and a route renders a screen of current work, not an archive, so the
#: read stops at the most recently closed handful rather than the whole file.
LEDGER_MERGE_ROW_LIMIT: Final = 20

#: The collections a route projection also reads through their ledger, so a
#: record does not vanish from a route that lists it the instant it compacts
#: out of the document. Milestone and Batch both leave the document on the
#: commit that moves them to a terminal status, and the Milestone and roadmap
#: routes list both; a collection joins here once a route that lists it needs
#: the same read.
LEDGER_MERGED_COLLECTIONS: Final = (Epoch2Collection.MILESTONE, Epoch2Collection.BATCH)


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


class ExportParams(BaseModel):
    """The parameters one export request carries.

    Attributes:
        repo_root: The repository whose tree to answer for; the daemon's bound tree
            when absent.
        route: The acceptance route to report. The Milestone's bundle view is the
            default because it is the view an acceptance is given against.
    """

    model_config = ConfigDict(extra="forbid")

    repo_root: str | None = None
    route: str = MILESTONE_ROUTE

    @model_validator(mode="after")
    def _route_is_one_this_verb_reports(self) -> Self:
        """Refuse a route outside the acceptance family.

        Raises:
            ValueError: The named route is not one this verb renders a report of, so
                the digest the report carried would address rows nobody asked for.
        """
        if self.route not in ACCEPTANCE_ROUTES:
            stated = ", ".join(ACCEPTANCE_ROUTES)
            raise ValueError(f"route {self.route!r} is not reportable; reportable: {stated}")
        return self


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


def _terminal_ledger_rows(
    *, authority: RootAuthority, collection: Epoch2Collection
) -> tuple[dict[str, Any], ...]:
    """Return *collection*'s most recent terminal rows, read from its ledger.

    A ledger may hold lines a route's collection did not write -- a
    Milestone's acceptance-bundle revision is filed in the same ledger as
    its terminal Milestone rows, under a different key grammar. Such a
    line's payload carries no ``key`` matching the line's own
    ``record_key``, which is what tells a genuine row of *collection* apart
    from one filed there for some other reason.

    Returns:
        Up to :data:`LEDGER_MERGE_ROW_LIMIT` payloads, oldest of the kept
        set first, in the order the ledger appended them. Empty when the
        collection has no ledger file yet.
    """
    path = ledger_path(_document_path(authority), collection)
    records = effective_records(read_ledger_records(path))
    payloads = [
        record.payload for record in records if record.payload.get("key") == record.record_key
    ]
    return tuple(payloads[-LEDGER_MERGE_ROW_LIMIT:])


def _ledger_rows_for(
    *, route: str, authority: RootAuthority
) -> dict[Epoch2Collection, tuple[dict[str, Any], ...]]:
    """Return the ledger-held rows *route*'s merged collections contribute."""
    return {
        collection: _terminal_ledger_rows(authority=authority, collection=collection)
        for collection in ROUTE_COLLECTIONS.get(route, ())
        if collection in LEDGER_MERGED_COLLECTIONS
    }


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
            ledger_rows=_ledger_rows_for(route=route, authority=authority),
        )
    except ValueError as error:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: {error}"
        ) from error


def _read_settings(*, authority: RootAuthority) -> SettingsView:
    """Build the effective-settings read model for a fence-cleared tree.

    The tree's root is both the workspace and the repo anchor, which is the call shape
    every other layered-config consumer uses. Nothing is written: the layers are read,
    merged in memory and handed back.

    Raises:
        DaemonValidationError: The document states its high-water mark as something
            other than an ordinal, so the view could not state the cursor it was read
            beside.
        FileNotFoundError: The selected generation carries no document.
    """
    root = authority.root
    cursor = _document_cursor(read_document(_document_path(authority)))
    return build_settings_view(
        workspace=root,
        repo=root,
        scope_id=RootIdentity.of(root).root_id,
        cursor=cursor,
        generated_at=datetime.now(UTC),
    )


def _report(*, route: str, authority: RootAuthority) -> ExportReport:
    """Render one acceptance route's read model, reading the document and nothing else.

    Raises:
        DaemonValidationError: The tree cannot be projected through a cursor.
        FileNotFoundError: The selected generation carries no document.
    """
    return export_report(build_acceptance_view(_project(route=route, authority=authority)))


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


#: The verb the effective-settings view is read through. It is not in
#: :data:`ROUTE_READ_METHODS`, because the route it serves binds no collection and its
#: answer is not a row projection.
SETTINGS_READ_METHOD: Final = READ_METHOD_TEMPLATE.format(route=SETTINGS_ROUTE)


@register(SETTINGS_READ_METHOD)
async def read_settings(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return every configuration leaf with the layer that set it and the ones it overrode.

    Args:
        ctx: Server context, whose bound state path is the tree fallback.
        params: The request parameters; ``repo_root`` names the tree when the caller
            does not want the one the daemon is bound to.

    Returns:
        The settings view as a JSON-mode mapping.

    Raises:
        NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
        DaemonValidationError: The tree's cursor could not be read.
    """
    authority = require_native_call(ctx, params)
    view = await asyncio.to_thread(_read_settings, authority=authority)
    logger.debug(f"read_settings leaves={len(view.leaves)} cursor={view.header.source_cursor}")
    return view.model_dump(mode="json")


#: The verb an acceptance route's report is taken through. It is not in
#: :data:`ROUTE_READ_METHODS`, because its answer is a rendered report rather than a
#: row projection; the rows it renders are read through the route's own read verb.
EXPORT_REPORT_METHOD: Final = "projection.export.report"


@register(EXPORT_REPORT_METHOD)
async def read_export_report(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one acceptance route's read model as plain text, at that view's digest.

    Nothing is written. The document is read, the view is rendered and the bytes are
    handed back, so an export leaves the tree at the cursor it found it at.

    Args:
        ctx: Server context, whose bound state path is the tree fallback.
        params: The request parameters, validated as :class:`ExportParams`.

    Returns:
        The report's digest, the cursor its rows were read at, and its lines.

    Raises:
        NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
        DaemonValidationError: The parameters are not an export request, or the tree
            cannot be projected.
    """
    try:
        args = ExportParams.model_validate(params)
    except ValidationError as error:
        raise DaemonValidationError(
            f"validation_failed: {EXPORT_UNREPORTABLE}: {error.error_count()} bad "
            f"parameter(s) for {EXPORT_REPORT_METHOD}"
        ) from error
    authority = require_native_call(ctx, params)
    report = await asyncio.to_thread(_report, route=args.route, authority=authority)
    logger.debug(f"read_export_report route={args.route} lines={len(report.lines)}")
    return {
        "route": args.route,
        "digest": report.digest,
        "source_cursor": report.source_cursor,
        "lines": list(report.lines),
    }


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
    "EXPORT_REPORT_METHOD",
    "EXPORT_UNREPORTABLE",
    "PROJECTION_UNREADABLE",
    "READ_METHOD_TEMPLATE",
    "RECONNECT_METHOD_TEMPLATE",
    "RECONNECT_UNNEGOTIABLE",
    "ROUTE_READ_METHODS",
    "ROUTE_RECONNECT_METHODS",
    "SETTINGS_READ_METHOD",
    "ExportParams",
    "ReconnectParams",
    "read_export_report",
    "read_settings",
]
