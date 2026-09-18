"""What a reconnecting console is owed: a replayable gap, or a stated refusal.

A client that comes back holds a cursor and nothing else. The question it cannot
answer for itself is whether the range it missed is still on the daemon's disk: if
it is, the daemon replays exactly that range as keyed patches and the console ends
at the daemon's cursor; if it is not, the daemon says so and the console must fetch
a whole projection instead. Guessing either way is the defect this module exists to
remove -- a client that assumes replay silently draws a projection with a hole in
it, and a client that assumes a snapshot throws away a cursor that was fine.

:func:`negotiate_reconnect` is the whole decision and it is pure: it takes the
client's cursor, the daemon's cursor and the ordinals retention still holds, and
returns one of three dispositions with the gap range spelled out. It checks the gap
is contiguous rather than only that its ends are retained, because a replay over a
hole is exactly the silent corruption the refusal exists to prevent.

:func:`apply_patches` closes the gap on the client side, and it rebuilds the
projection through :func:`~eawf.kernel.projection.compute.build_route_projection`
rather than digesting the rows itself. That is deliberate: the digest a replayed
projection carries must come from the same code the daemon's own read came from, or
digest equality at one cursor proves only that two copies of one bug agree.

The nine connection values and the per-view staleness targets live here too, beside
the negotiation that moves between them, so a console does not keep a second
vocabulary for the same link.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, Self

from pydantic import ConfigDict, model_validator

from eawf.kernel.projection.compute import (
    PROJECTION_SCHEMA_VERSION,
    ROUTE_COLLECTIONS,
    KeyedPatch,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND
from eawf.kernel.projection.truth import Completeness, ConnectionState
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)

logger = logging.getLogger(__name__)


class ConnectionValue(StrEnum):
    """The console's link to the daemon, always exactly one of nine values.

    Eight of them are the :class:`~eawf.kernel.projection.truth.ConnectionState`
    members one for one. The ninth exists because ``live`` answers two different
    operator questions: a live projection that vouches for every row of its scope
    is not the same thing as a live projection with a bucket it cannot vouch for,
    and only the first may label a count complete.
    """

    LIVE_COMPLETE = "live_complete"
    LIVE_PARTIAL = "live_partial"
    GAP = "gap"
    REPLAYING = "replaying"
    SNAPSHOT_REQUIRED = "snapshot_required"
    SNAPSHOT_LOADING = "snapshot_loading"
    OFFLINE_SNAPSHOT = "offline_snapshot"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"


class StalenessClass(StrEnum):
    """The named staleness targets a view ages under.

    ``terminal_run`` is not a view: a terminal entity ages informationally rather
    than pulsing, whatever route it is drawn on, so it overrides the route's own
    class instead of being assigned to one.
    """

    SCOPE = "scope"
    BATCH = "batch"
    TASK = "task"
    ACTIVITY = "activity"
    ATTENTION = "attention"
    RELEASE = "release"
    TERMINAL_RUN = "terminal_run"


class ReconnectDisposition(StrEnum):
    """What the daemon answers a reconnecting client with.

    ``current`` and ``replay`` both end at the daemon's cursor; they differ in
    whether anything had to be sent to get there. ``snapshot_required`` is a
    refusal: the console cannot be repaired from patches within retention, so it
    must fetch a whole projection.
    """

    CURRENT = "current"
    REPLAY = "replay"
    SNAPSHOT_REQUIRED = "snapshot_required"


#: The seconds a view may age before it is stale. The six view targets are the
#: console's declared display policy; the source's receive and revision facts stay
#: authoritative, so tightening a target never makes a stale read look fresh.
STALENESS_TARGET_SECONDS: Final[Mapping[StalenessClass, float]] = MappingProxyType(
    {
        StalenessClass.SCOPE: 5.0,
        StalenessClass.BATCH: 5.0,
        StalenessClass.TASK: 3.0,
        StalenessClass.ACTIVITY: 2.0,
        StalenessClass.ATTENTION: 2.0,
        StalenessClass.RELEASE: 10.0,
        StalenessClass.TERMINAL_RUN: 30.0,
    }
)

#: The routes whose staleness class the design names outright. Every other route
#: ages at the spine target: a surface with no live feed of its own is a spine
#: surface, and giving it the fastest target would only make it look stale.
_NAMED_ROUTE_CLASSES: Final[Mapping[str, StalenessClass]] = MappingProxyType(
    {
        "activity": StalenessClass.ACTIVITY,
        "attention": StalenessClass.ATTENTION,
        "task.detail": StalenessClass.TASK,
        "run.detail": StalenessClass.TASK,
        "batch.detail": StalenessClass.BATCH,
        "release": StalenessClass.RELEASE,
    }
)

#: The connection value each non-live state maps to. ``live`` is absent because it
#: is the one state whose value depends on what the projection claims to hold.
_STATE_VALUES: Final[Mapping[ConnectionState, ConnectionValue]] = MappingProxyType(
    {
        ConnectionState.GAP: ConnectionValue.GAP,
        ConnectionState.REPLAYING: ConnectionValue.REPLAYING,
        ConnectionState.SNAPSHOT_REQUIRED: ConnectionValue.SNAPSHOT_REQUIRED,
        ConnectionState.SNAPSHOT_LOADING: ConnectionValue.SNAPSHOT_LOADING,
        ConnectionState.OFFLINE_SNAPSHOT: ConnectionValue.OFFLINE_SNAPSHOT,
        ConnectionState.DISCONNECTED: ConnectionValue.DISCONNECTED,
        ConnectionState.DEGRADED: ConnectionValue.DEGRADED,
    }
)

#: What the console's link reads as while each disposition is being carried out.
#: A refusal is entered as the refusal, never as a loading state the operator
#: would wait on: nothing is being transferred until the repair is accepted.
_DISPOSITION_VALUES: Final[Mapping[ReconnectDisposition, ConnectionValue]] = MappingProxyType(
    {
        ReconnectDisposition.CURRENT: ConnectionValue.LIVE_COMPLETE,
        ReconnectDisposition.REPLAY: ConnectionValue.REPLAYING,
        ReconnectDisposition.SNAPSHOT_REQUIRED: ConnectionValue.SNAPSHOT_REQUIRED,
    }
)

#: What a count renders as when the register behind it could not be read at all.
#: Distinct from a zero, which is a count that was taken and came out empty.
UNAVAILABLE_COUNT: Final = "∅ unavailable"

#: How a route's read verb is spelled on the wire. The name lives beside the shapes
#: it carries rather than in the daemon module that registers it, so the console can
#: address the verb without importing the daemon's dispatch graph.
READ_METHOD_TEMPLATE: Final = "projection.{route}.read"

#: How a route's reconnect verb is spelled on the wire.
RECONNECT_METHOD_TEMPLATE: Final = "projection.{route}.reconnect"


def _route_staleness_classes() -> Mapping[str, StalenessClass]:
    """Return the staleness class of every route a read model declares.

    Returns:
        A read-only mapping, total over the declared routes so a view cannot be
        drawn without a target.

    Raises:
        ValueError: A named route is not declared by any read model. Raised at
            import, because a target keyed on a route that does not exist is a
            target no view ever reads.
    """
    declared = {route for spec in READ_MODEL_BY_KIND.values() for route in spec.routes}
    unknown = sorted(set(_NAMED_ROUTE_CLASSES) - declared)
    if unknown:
        raise ValueError(
            f"staleness targets name routes no read model declares: {', '.join(unknown)}"
        )
    return MappingProxyType(
        {route: _NAMED_ROUTE_CLASSES.get(route, StalenessClass.SCOPE) for route in sorted(declared)}
    )


#: The staleness class each declared console route ages under.
ROUTE_STALENESS_CLASS: Final[Mapping[str, StalenessClass]] = _route_staleness_classes()


def projection_now() -> datetime:
    """Return the stamp a client-built projection is generated at.

    The console's own clock is monotonic seconds for frame deadlines, which a
    projection header cannot be stamped with, so the default stamp is declared here
    beside the shapes that carry it.
    """
    return datetime.now(UTC)


def connection_value(
    *, connection_state: ConnectionState, completeness: Completeness
) -> ConnectionValue:
    """Return the console value a projection header's link reads as.

    Args:
        connection_state: The header's link state.
        completeness: What the header claims to hold; it splits ``live`` alone.

    Returns:
        One of the nine values.
    """
    if connection_state is not ConnectionState.LIVE:
        return _STATE_VALUES[connection_state]
    if completeness is Completeness.COMPLETE:
        return ConnectionValue.LIVE_COMPLETE
    return ConnectionValue.LIVE_PARTIAL


def connection_for_disposition(disposition: ReconnectDisposition) -> ConnectionValue:
    """Return the value the console's link takes while a disposition is carried out."""
    return _DISPOSITION_VALUES[disposition]


def vouches_for_counts(value: ConnectionValue) -> bool:
    """Return whether a count taken under *value* may be called complete.

    Only a live projection that claims every row of its scope may; every other
    value has some range it cannot vouch for, and a count labelled complete under
    one of them is the claim this whole vocabulary exists to stop.
    """
    return value is ConnectionValue.LIVE_COMPLETE


def staleness_target_seconds(route: str, *, terminal: bool = False) -> float:
    """Return the seconds *route* may age before it is stale.

    Args:
        route: The console route key the view is drawn on.
        terminal: Whether the subject drawn is a terminal entity. A terminal
            entity ages informationally on every route, because a finished
            subject that pulses like a live one is a false claim about it.

    Returns:
        The target in seconds.

    Raises:
        KeyError: No read model declares *route*, so it is not a view.
    """
    if terminal:
        return STALENESS_TARGET_SECONDS[StalenessClass.TERMINAL_RUN]
    return STALENESS_TARGET_SECONDS[ROUTE_STALENESS_CLASS[route]]


class _ConnectionModel(Epoch2Model):
    """Strict and immutable, like every other projection shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GapRange(_ConnectionModel):
    """The exact ordinals a reconnecting client missed, both ends included.

    Attributes:
        first_sequence: The first ordinal the client has not seen.
        last_sequence: The daemon's own cursor, the last ordinal it allocated.
    """

    first_sequence: StrictPositiveInt
    last_sequence: StrictPositiveInt

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        """Refuse a range that runs backwards.

        Raises:
            ValueError: The last ordinal precedes the first.
        """
        if self.last_sequence < self.first_sequence:
            raise ValueError(
                f"a gap runs forwards: {self.first_sequence} to {self.last_sequence} does not"
            )
        return self

    @property
    def length(self) -> int:
        """Return how many ordinals the gap covers."""
        return self.last_sequence - self.first_sequence + 1

    def sequences(self) -> tuple[int, ...]:
        """Return every ordinal the gap covers, in order."""
        return tuple(range(self.first_sequence, self.last_sequence + 1))


class RetentionWindow(_ConnectionModel):
    """What the daemon's event retention still holds, as one range.

    Attributes:
        first_sequence: The oldest ordinal still on disk; ``None`` when retention
            holds no committed transition at all.
        last_sequence: The newest ordinal still on disk; ``None`` under the same
            condition.
    """

    first_sequence: StrictPositiveInt | None = None
    last_sequence: StrictPositiveInt | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        """Refuse a window with one end stated and the other not.

        Raises:
            ValueError: Only one end is stated, or the window runs backwards.
        """
        first, last = self.first_sequence, self.last_sequence
        if (first is None) != (last is None):
            raise ValueError("a retention window states both ends or neither")
        if first is not None and last is not None and last < first:
            raise ValueError("a retention window runs forwards")
        return self


class ReconnectNegotiation(_ConnectionModel):
    """What the daemon answers one reconnecting client with.

    Attributes:
        schema_version: The projection schema this shape is spelled in.
        route: The console route the client is reconnecting for.
        disposition: Whether the client is current, replayable, or must snapshot.
        client_cursor: The ordinal the client last acknowledged; ``0`` for a
            client that has acknowledged nothing.
        server_cursor: The ordinal the tree stands at; ``0`` for a workspace that
            has committed nothing.
        gap: The exact range the client missed; ``None`` only when it missed
            nothing. Stated under ``snapshot_required`` too, because a refusal
            that will not name its range leaves the operator nothing to act on.
        retention: What the daemon still holds, so a refusal can be read against
            what would have been needed.
        first_missing: The first ordinal of the gap retention cannot supply; set
            exactly when the disposition is ``snapshot_required``.
    """

    schema_version: Literal["1.0"]
    route: NonEmptyStr
    disposition: ReconnectDisposition
    client_cursor: StrictNonNegativeInt
    server_cursor: StrictNonNegativeInt
    gap: GapRange | None = None
    retention: RetentionWindow
    first_missing: StrictPositiveInt | None = None

    @model_validator(mode="after")
    def _check_negotiation(self) -> Self:
        """Refuse an answer whose disposition and range disagree.

        Raises:
            ValueError: Every contradiction found, joined in one message.
        """
        refused = self.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED
        current = self.disposition is ReconnectDisposition.CURRENT
        checks = (
            (self.client_cursor <= self.server_cursor, "a client cannot stand ahead of the daemon"),
            (current == (self.gap is None), "a gap is stated exactly when one was missed"),
            (refused == (self.first_missing is not None), "a refusal names its first missing row"),
            (
                self.gap is None or self.gap.last_sequence == self.server_cursor,
                "a gap ends at the daemon's own cursor",
            ),
            (
                self.gap is None or self.gap.first_sequence == self.client_cursor + 1,
                "a gap starts at the ordinal after the client's cursor",
            ),
        )
        problems = [message for holds, message in checks if not holds]
        if problems:
            raise ValueError(f"contradictory reconnect negotiation: {'; '.join(problems)}")
        return self


def retention_window(sequences: Collection[int]) -> RetentionWindow:
    """Return the range *sequences* spans, as the daemon's retention window.

    Args:
        sequences: The committed ordinals retention still holds, in any order.

    Returns:
        The window; both ends unstated when nothing is retained.

    Raises:
        ValueError: An ordinal is not a committed one. A retained row that states
            no usable ordinal cannot bound a window a refusal is read against.
    """
    ordinals = sorted(sequences)
    for ordinal in ordinals:
        if ordinal < 1:
            raise ValueError(f"retention holds {ordinal}, which is not a committed ordinal")
    if not ordinals:
        return RetentionWindow()
    return RetentionWindow(first_sequence=ordinals[0], last_sequence=ordinals[-1])


def negotiate_reconnect(
    *,
    route: str,
    client_cursor: int,
    server_cursor: int,
    retained: Collection[int],
) -> ReconnectNegotiation:
    """Return whether a reconnecting client may be replayed, and over what range.

    Args:
        route: The console route the client is reconnecting for.
        client_cursor: The ordinal it last acknowledged; ``0`` when it has none.
        server_cursor: The ordinal the tree stands at.
        retained: Every committed ordinal the daemon's retention still holds.

    Returns:
        The negotiation, with the gap range stated whenever one was missed --
        under a refusal as much as under a replay.

    Raises:
        ValueError: The route renders no projection, a cursor is negative, the
            client stands ahead of the daemon, or a retained ordinal is not a
            committed one.
    """
    if route not in ROUTE_COLLECTIONS:
        bound = ", ".join(sorted(ROUTE_COLLECTIONS))
        raise ValueError(
            f"route {route!r} renders no epoch-2 collection, so nothing can be replayed "
            f"for it; bound routes: {bound}"
        )
    for name, cursor in (("client_cursor", client_cursor), ("server_cursor", server_cursor)):
        if cursor < 0:
            raise ValueError(f"{name} is a committed canonical_sequence, never {cursor}")
    if client_cursor > server_cursor:
        raise ValueError(
            f"client_cursor {client_cursor} stands ahead of the daemon's {server_cursor}, "
            "so there is no gap a replay could close"
        )
    window = retention_window(retained)
    if client_cursor == server_cursor:
        return ReconnectNegotiation(
            schema_version=PROJECTION_SCHEMA_VERSION,
            route=route,
            disposition=ReconnectDisposition.CURRENT,
            client_cursor=client_cursor,
            server_cursor=server_cursor,
            retention=window,
        )
    gap = GapRange(first_sequence=client_cursor + 1, last_sequence=server_cursor)
    held = set(retained)
    # Contiguity, not just the ends: a replay over a hole would hand the console a
    # projection missing a row while telling it the gap is closed.
    missing = [ordinal for ordinal in gap.sequences() if ordinal not in held]
    if missing:
        logger.info(
            f"negotiate_reconnect refused route={route} gap={gap.first_sequence}-"
            f"{gap.last_sequence} missing={len(missing)}"
        )
        return ReconnectNegotiation(
            schema_version=PROJECTION_SCHEMA_VERSION,
            route=route,
            disposition=ReconnectDisposition.SNAPSHOT_REQUIRED,
            client_cursor=client_cursor,
            server_cursor=server_cursor,
            gap=gap,
            retention=window,
            first_missing=missing[0],
        )
    return ReconnectNegotiation(
        schema_version=PROJECTION_SCHEMA_VERSION,
        route=route,
        disposition=ReconnectDisposition.REPLAY,
        client_cursor=client_cursor,
        server_cursor=server_cursor,
        gap=gap,
        retention=window,
    )


def apply_patches(
    projection: RouteProjection,
    patches: Iterable[KeyedPatch],
    *,
    cursor: int,
    scope_id: str,
    generated_at: datetime,
) -> RouteProjection:
    """Return *projection* advanced to *cursor* by the keyed patches of a replay.

    The rows are re-projected through the daemon's own builder rather than
    digested here, so a replayed projection and a clean read at one cursor are
    digested by one implementation and equality between them means something.

    Args:
        projection: The projection the client held before the gap.
        patches: The gap's keyed patches, in any order; a later ordinal wins.
        cursor: The ordinal the replay closes at.
        scope_id: The scope the rebuilt projection is stated for.
        generated_at: When the rebuild happened.

    Returns:
        The route's projection at *cursor*.

    Raises:
        ValueError: A patch does not reach this route, or carries an ordinal
            past the cursor the replay claims to close at.
    """
    rows: dict[str, dict[str, Any]] = {}
    for row in projection.rows:
        rows.setdefault(row.collection.value, {})[row.key] = {
            "urn": row.urn,
            "revision": row.revision,
            "status": row.status.value,
        }
    for patch in sorted(patches, key=lambda item: item.canonical_sequence):
        if projection.route not in patch.routes:
            raise ValueError(
                f"a patch for {', '.join(patch.routes)} does not reach route {projection.route!r}"
            )
        if patch.canonical_sequence > cursor:
            raise ValueError(
                f"patch at {patch.canonical_sequence} is past the cursor {cursor} the replay closes"
            )
        for entry in patch.entries:
            rows.setdefault(entry.collection.value, {})[entry.key] = {
                "urn": entry.urn,
                "revision": entry.revision,
                "status": entry.status,
            }
    return build_route_projection(
        route=projection.route,
        document=dict(rows),
        cursor=cursor,
        scope_id=scope_id,
        generated_at=generated_at,
    )


__all__ = [
    "READ_METHOD_TEMPLATE",
    "RECONNECT_METHOD_TEMPLATE",
    "ROUTE_STALENESS_CLASS",
    "STALENESS_TARGET_SECONDS",
    "UNAVAILABLE_COUNT",
    "ConnectionValue",
    "GapRange",
    "ReconnectDisposition",
    "ReconnectNegotiation",
    "RetentionWindow",
    "StalenessClass",
    "apply_patches",
    "connection_for_disposition",
    "connection_value",
    "negotiate_reconnect",
    "projection_now",
    "retention_window",
    "staleness_target_seconds",
    "vouches_for_counts",
]
