"""Nine connection values, a staleness target per view, and an honest count.

The link is one value at a time and there are exactly nine of them. Eight are the
header's own states; the ninth exists because ``live`` answers two different
questions -- a projection that vouches for every row of its scope is not the same
thing as a live one with a bucket it cannot vouch for, and only the first may call a
count complete. The suite pins the count at nine so a tenth cannot be added without
this file saying so, and derives every value from the header pair rather than letting
the console keep a second vocabulary for the same link.

The staleness table is checked for totality rather than for its six named rows alone:
a view drawn with no target is a view whose ageing nobody declared, and the table is
built from the read-model declarations so a new route cannot slip past it.

The count rule is the one an operator reads off the frame. Under a live and complete
link a count is a number. Under any other value it carries a label saying what it is
-- known, not complete. A count whose register could not be read at all is
unavailable, and never a zero, because zero is a count that was taken.
"""

from __future__ import annotations

import pytest

from eawf.kernel.projection.connection import (
    ROUTE_STALENESS_CLASS,
    STALENESS_TARGET_SECONDS,
    UNAVAILABLE_COUNT,
    ConnectionValue,
    StalenessClass,
    connection_value,
    staleness_target_seconds,
    vouches_for_counts,
)
from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND
from eawf.kernel.projection.truth import Completeness, ConnectionState
from eawf.surfaces.tui.console.seam import KNOWN_COUNT_LABEL, ProjectionSeam

#: The nine values, spelled as the wire spells them.
NINE_VALUES = (
    "live_complete",
    "live_partial",
    "gap",
    "replaying",
    "snapshot_required",
    "snapshot_loading",
    "offline_snapshot",
    "disconnected",
    "degraded",
)

#: The design's per-view targets, in seconds.
NAMED_TARGETS = {
    "scope.home": 5.0,
    "batch.detail": 5.0,
    "task.detail": 3.0,
    "run.detail": 3.0,
    "activity": 2.0,
    "attention": 2.0,
    "release": 10.0,
}


def _seam(route: str = "scope.home") -> ProjectionSeam:
    """Return a seam bound to no tree; nothing here opens a transport."""
    return ProjectionSeam(route=route, scope_id="EAWF", state_path=None)


def _at(seam: ProjectionSeam, value: ConnectionValue) -> ProjectionSeam:
    """Put the seam's link at *value*.

    The link is derived from the transport and the daemon's answers, so there is no
    setter to call; a test that wants one particular value states it directly.
    """
    seam._connection = value
    return seam


def test_there_are_exactly_nine_connection_values() -> None:
    """A tenth value cannot arrive without this file saying what it means."""
    assert tuple(value.value for value in ConnectionValue) == NINE_VALUES
    assert len(ConnectionValue) == 9


def test_every_header_state_maps_onto_a_connection_value() -> None:
    """The console keeps no second vocabulary: every header state has a value."""
    for state in ConnectionState:
        assert isinstance(
            connection_value(connection_state=state, completeness=Completeness.PARTIAL),
            ConnectionValue,
        )


@pytest.mark.parametrize(
    ("state", "completeness", "expected"),
    [
        (ConnectionState.LIVE, Completeness.COMPLETE, ConnectionValue.LIVE_COMPLETE),
        (ConnectionState.LIVE, Completeness.PARTIAL, ConnectionValue.LIVE_PARTIAL),
        (ConnectionState.LIVE, Completeness.UNVERIFIED, ConnectionValue.LIVE_PARTIAL),
        (ConnectionState.GAP, Completeness.UNVERIFIED, ConnectionValue.GAP),
        (ConnectionState.REPLAYING, Completeness.UNVERIFIED, ConnectionValue.REPLAYING),
        (
            ConnectionState.SNAPSHOT_REQUIRED,
            Completeness.UNVERIFIED,
            ConnectionValue.SNAPSHOT_REQUIRED,
        ),
        (
            ConnectionState.SNAPSHOT_LOADING,
            Completeness.UNVERIFIED,
            ConnectionValue.SNAPSHOT_LOADING,
        ),
        (
            ConnectionState.OFFLINE_SNAPSHOT,
            Completeness.PARTIAL,
            ConnectionValue.OFFLINE_SNAPSHOT,
        ),
        (ConnectionState.DISCONNECTED, Completeness.PARTIAL, ConnectionValue.DISCONNECTED),
        (ConnectionState.DEGRADED, Completeness.PARTIAL, ConnectionValue.DEGRADED),
    ],
)
def test_the_value_is_derived_from_the_header_pair(
    state: ConnectionState, completeness: Completeness, expected: ConnectionValue
) -> None:
    """Only ``live`` splits on completeness; every other state is itself."""
    assert connection_value(connection_state=state, completeness=completeness) is expected


def test_only_a_live_and_complete_link_vouches_for_a_count() -> None:
    """Eight of the nine values have some range they cannot vouch for."""
    vouching = [value for value in ConnectionValue if vouches_for_counts(value)]

    assert vouching == [ConnectionValue.LIVE_COMPLETE]


def test_every_declared_view_has_a_staleness_target() -> None:
    """A route drawn with no target is a view whose ageing nobody declared."""
    declared = {route for spec in READ_MODEL_BY_KIND.values() for route in spec.routes}

    assert set(ROUTE_STALENESS_CLASS) == declared
    assert all(staleness_target_seconds(route) > 0 for route in declared)


def test_every_staleness_class_states_its_seconds() -> None:
    """The class table is total, so no route can age under an unstated target."""
    assert set(STALENESS_TARGET_SECONDS) == set(StalenessClass)
    assert set(ROUTE_STALENESS_CLASS.values()) <= set(StalenessClass)


@pytest.mark.parametrize(("route", "seconds"), sorted(NAMED_TARGETS.items()))
def test_the_named_views_carry_the_declared_targets(route: str, seconds: float) -> None:
    """The six named view targets are the design's numbers, not approximations."""
    assert staleness_target_seconds(route) == pytest.approx(seconds)


def test_an_unnamed_view_ages_at_the_spine_target() -> None:
    """A surface with no live feed of its own is a spine surface."""
    assert ROUTE_STALENESS_CLASS["settings"] is StalenessClass.SCOPE
    assert staleness_target_seconds("settings") == pytest.approx(5.0)


def test_a_terminal_subject_ages_informationally_on_every_route() -> None:
    """A finished subject that pulses like a live one is a false claim about it."""
    for route in ("activity", "run.detail", "scope.home"):
        assert staleness_target_seconds(route, terminal=True) == pytest.approx(30.0)


def test_a_route_no_read_model_declares_has_no_target() -> None:
    """A target for a view that does not exist is a target nothing ever reads."""
    with pytest.raises(KeyError):
        staleness_target_seconds("not.a.route")


def test_the_seam_answers_the_staleness_target_of_its_own_view() -> None:
    """The seam is where the console asks, because it knows the route it is on."""
    assert _seam("activity").staleness_target() == pytest.approx(2.0)
    assert _seam("release").staleness_target() == pytest.approx(10.0)
    assert _seam("run.detail").staleness_target(terminal=True) == pytest.approx(30.0)


def test_a_count_under_a_live_and_complete_link_is_a_number() -> None:
    """The one value that may state a count states it plainly."""
    seam = _at(_seam(), ConnectionValue.LIVE_COMPLETE)

    assert seam.vouches_for_counts is True
    assert seam.count(41208) == "41208"
    assert seam.count(0) == "0"


@pytest.mark.parametrize(
    "value", [value for value in ConnectionValue if value is not ConnectionValue.LIVE_COMPLETE]
)
def test_a_count_outside_a_complete_link_renders_incomplete(value: ConnectionValue) -> None:
    """Under the other eight the number is labelled rather than claimed."""
    seam = _at(_seam(), value)

    assert seam.vouches_for_counts is False
    assert seam.count(3) == f"3 {KNOWN_COUNT_LABEL}"
    # Zero is a count that was taken, so it is labelled like any other number.
    assert seam.count(0) == f"0 {KNOWN_COUNT_LABEL}"


@pytest.mark.parametrize("value", list(ConnectionValue))
def test_a_register_that_could_not_be_read_renders_unavailable(value: ConnectionValue) -> None:
    """Unread is not zero, under any value of the link."""
    assert _at(_seam(), value).count(None) == UNAVAILABLE_COUNT


def test_a_fresh_seam_is_disconnected_until_something_says_otherwise() -> None:
    """A console that has not reached the daemon does not draw as live."""
    seam = _seam()

    assert seam.connection is ConnectionValue.DISCONNECTED
    assert seam.projection is None
    assert seam.cursor == 0
    assert seam.count(7) == f"7 {KNOWN_COUNT_LABEL}"


def test_an_unreachable_daemon_puts_the_link_at_disconnected() -> None:
    """The transport's degraded flag is the link's own signal, not a separate one."""
    import asyncio

    seam = _at(_seam(), ConnectionValue.LIVE_COMPLETE)

    asyncio.run(seam._on_degraded(True))

    assert seam.connection is ConnectionValue.DISCONNECTED


def test_a_seam_persists_the_selection_and_filters_it_restores_by() -> None:
    """Step one of the reconnect protocol: what the console keeps across a break."""
    seam = _seam("activity")

    seam.select("RUN-538453eb", bucket="running")
    persisted = seam.persisted()

    assert persisted.route == "activity"
    assert persisted.scope_id == "EAWF"
    assert persisted.selected_id == "RUN-538453eb"
    assert persisted.filters == {"bucket": "running"}
    assert persisted.projection_revision == 0
    assert persisted.cursor == 0
