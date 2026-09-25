"""The seam holds a bounded cache of routes over its one binding.

A route is read once, on the first navigation to it, and never again while it is held:
the one feed keeps every held route current, so going back costs no read. Attention is
pinned, because the header prints its count on every route. The cache is bounded, and
the least recently shown unpinned route is the one evicted.

The daemon here is a recording stand-in behind the binding's client factory, so every
read the console issues is counted and nothing reaches a real socket.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    KeyedPatch,
    PatchEntry,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import (
    READ_METHOD_TEMPLATE,
    ReconnectDisposition,
    negotiate_reconnect,
)
from eawf.kernel.projection.registers import ATTENTION_ROUTE
from eawf.kernel.projection.settings import SETTINGS_ROUTE
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.frame import needs_count
from eawf.surfaces.tui.console.seam import (
    DEFAULT_ROUTE_CAPACITY,
    PINNED_ROUTES,
    ProjectionSeam,
)
from eawf.surfaces.tui.console.session import SIZES

#: When every probe projection is stamped; the digest does not cover it.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

SCOPE = "EAWF"

#: The cursor every probe projection is read at.
CURSOR = 7

#: One row per route, in a status its collection accepts.
ROWS: dict[str, tuple[str, str, str]] = {
    "scope.home": ("track", "TRK-7001", "ACTIVE"),
    "track": ("track", "TRK-7001", "ACTIVE"),
    "activity": ("run", "RUN-0000000a", "RUNNING"),
    "roadmap": ("milestone", "MLS-0001", "PLANNED"),
}


def projection(route: str, *, cursor: int = CURSOR) -> RouteProjection:
    """Return *route*'s projection over its one probe row; Attention holds none."""
    document: dict[str, Any] = {}
    if route in ROWS:
        collection, key, status = ROWS[route]
        document = {
            collection: {
                key: {
                    "urn": f"urn:eawf:{SCOPE}:{collection}:{key}",
                    "revision": 1,
                    "status": status,
                }
            }
        }
    return build_route_projection(
        route=route, document=document, cursor=cursor, scope_id=SCOPE, generated_at=AT
    )


def patch(route: str, *, key: str, sequence: int, collection: str = "track") -> KeyedPatch:
    """Return a keyed patch adding row *key* to *route* at ordinal *sequence*."""
    return KeyedPatch.model_validate(
        {
            "schema_version": "1.0",
            "projection_kind": ROUTE_READ_MODELS[route],
            "routes": [route],
            "scope_id": SCOPE,
            "canonical_sequence": sequence,
            "entries": [
                PatchEntry.model_validate(
                    {
                        "key": key,
                        "urn": f"urn:eawf:{SCOPE}:{collection}:{key}",
                        "collection": collection,
                        "revision": 1,
                        "status": "ACTIVE" if collection == "track" else "OPEN",
                    }
                ).model_dump(mode="json")
            ],
        }
    )


class FakeDaemon:
    """A daemon that serves every route's read and records each one.

    Attributes:
        reads: The route of every read issued, in order.
        failing: Routes whose read raises, standing in for an unreachable register.
        reconnect_answer: What a reconnect call answers with, when one is expected.
    """

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.reads: list[str] = []
        self.failing = failing
        self.reconnect_answer: dict[str, Any] | None = None
        self._routes = {
            READ_METHOD_TEMPLATE.format(route=route): route for route in ROUTE_COLLECTIONS
        }

    def client(self) -> _Client:
        """Return one client over this daemon, as the binding's factory does."""
        return _Client(self)

    def answer(self, method: str) -> dict[str, Any]:
        """Answer one request/response call.

        Raises:
            ConnectionError: The route is failing, or the method is not served here.
        """
        if method.endswith(".reconnect") and self.reconnect_answer is not None:
            return self.reconnect_answer
        route = self._routes.get(method)
        if route is None:
            raise ConnectionError(f"{method} is not served")
        self.reads.append(route)
        if route in self.failing:
            raise ConnectionError(f"{route} could not be read")
        return projection(route).model_dump(mode="json")


class _Client:
    """A context-managed client over a :class:`FakeDaemon`."""

    def __init__(self, daemon: FakeDaemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method)


def seam_over(daemon: FakeDaemon, *, route: str = "scope.home", **options: Any) -> ProjectionSeam:
    """Return a seam bound to no tree, reading through *daemon*."""
    return ProjectionSeam(
        route=route,
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
        **options,
    )


async def settle(app: ConsoleApp, pilot: Any) -> None:
    """Wait for every route read in flight, then for the repaint it asks for."""
    await app.workers.wait_for_complete()
    await pilot.pause()


# ---------- the app loads on mount and on first navigation ----------


def test_pool_loads_route_on_navigation() -> None:
    """Mount reads the home route and Attention; a new route is read once, a return never."""
    daemon = FakeDaemon()
    seam = seam_over(daemon)

    async def drive() -> list[tuple[list[str], tuple[str, ...], bool]]:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        steps: list[tuple[list[str], tuple[str, ...], bool]] = []
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            steps.append(
                (list(daemon.reads), seam.held_routes, "TRK-7001" in "\n".join(app.frame_rows))
            )
            for keys in (("g", "a"), ("g", "h"), ("g", "a")):
                for key in keys:
                    app.press_key(key)
                await settle(app, pilot)
                shown = ROWS[app.route_key][1]
                steps.append(
                    (list(daemon.reads), seam.held_routes, shown in "\n".join(app.frame_rows))
                )
        return steps

    steps = asyncio.run(drive())
    assert [reads for reads, _, _ in steps] == [
        ["scope.home", ATTENTION_ROUTE],
        ["scope.home", ATTENTION_ROUTE, "activity"],
        ["scope.home", ATTENTION_ROUTE, "activity"],
        ["scope.home", ATTENTION_ROUTE, "activity"],
    ]
    assert all(ATTENTION_ROUTE in held for _, held, _ in steps)
    assert all(drawn for _, _, drawn in steps)


def test_header_attention_count_from_live_register_on_every_route() -> None:
    """Every route's view carries the held Attention register, never the fixture count."""
    daemon = FakeDaemon()
    seam = seam_over(daemon)

    async def drive() -> list[tuple[str, str | None, int, str]]:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        seen: list[tuple[str, str | None, int, str]] = []
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            for letter in ("a", "t", "h"):
                view = app.view()
                attention = view.attention
                seen.append(
                    (
                        app.route_key,
                        attention.route if attention else None,
                        needs_count(view),
                        app.frame_rows[0],
                    )
                )
                app.press_key("g")
                app.press_key(letter)
                await settle(app, pilot)
        return seen

    seen = asyncio.run(drive())
    assert [route for route, _, _, _ in seen] == ["scope.home", "activity", "roadmap"]
    assert all(held == ATTENTION_ROUTE for _, held, _, _ in seen)
    # the shipped register is unwritten, so the honest count is no badge at all
    assert all(count == 0 and "NEEDS YOU" not in row for _, _, count, row in seen)


def test_pool_leaves_an_unreadable_route_unheld_and_loads_the_rest() -> None:
    """A failed read is not raised into the app; the frame keeps saying it holds nothing."""
    daemon = FakeDaemon(failing=frozenset({"scope.home"}))
    seam = seam_over(daemon)

    async def drive() -> str:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            return app.frame_rows[1]

    body = asyncio.run(drive())
    assert seam.held_routes == (ATTENTION_ROUTE,)
    assert body.strip().startswith("NOT HELD")


# ---------- the seam's cache, without an app ----------


def test_sync_reads_the_visible_route_then_the_pinned_ones() -> None:
    daemon = FakeDaemon()
    seam = seam_over(daemon)
    assert asyncio.run(seam.sync()) == ("scope.home", ATTENTION_ROUTE)
    assert asyncio.run(seam.sync()) == ()
    assert daemon.reads == ["scope.home", ATTENTION_ROUTE]


def test_sync_on_the_attention_route_reads_it_once() -> None:
    """The visible route and the pinned one are one route: one read, not two."""
    daemon = FakeDaemon()
    assert asyncio.run(seam_over(daemon, route=ATTENTION_ROUTE).sync()) == (ATTENTION_ROUTE,)
    assert daemon.reads == [ATTENTION_ROUTE]


def test_concurrent_syncs_issue_one_read_per_route() -> None:
    """A second navigation before the first read lands must not read the route again."""
    daemon = FakeDaemon()
    seam = seam_over(daemon)

    async def both() -> None:
        await asyncio.gather(seam.sync(), seam.sync())

    asyncio.run(both())
    assert sorted(daemon.reads) == sorted(["scope.home", ATTENTION_ROUTE])


def test_owed_skips_a_route_the_daemon_serves_no_read_for() -> None:
    seam = seam_over(FakeDaemon(), route="entry")
    assert seam.owed() == (ATTENTION_ROUTE,)


def test_owed_asks_for_the_settings_view_on_a_settings_route() -> None:
    seam = seam_over(FakeDaemon(), route="settings.stack")
    assert seam.owed() == (ATTENTION_ROUTE, SETTINGS_ROUTE)


def test_retarget_clears_the_selection_and_rereads_nothing() -> None:
    daemon = FakeDaemon()
    seam = seam_over(daemon)
    asyncio.run(seam.sync())
    seam.select("TRK-7001", lens="open")
    seam.retarget("activity")
    assert seam.persisted().selected_id is None
    assert seam.persisted().filters == {}
    seam.retarget("scope.home")
    assert seam.projection is not None
    assert daemon.reads == ["scope.home", ATTENTION_ROUTE]


def test_eviction_drops_the_least_recently_shown_unpinned_route() -> None:
    """Capacity three: Attention and the visible route stay; the oldest other goes."""
    daemon = FakeDaemon()
    seam = seam_over(daemon, capacity=3)
    for route in ("scope.home", "activity", "roadmap"):
        seam.retarget(route)
        asyncio.run(seam.sync())
    assert set(seam.held_routes) == {ATTENTION_ROUTE, "activity", "roadmap"}
    seam.retarget("scope.home")
    asyncio.run(seam.sync())
    assert set(seam.held_routes) == {ATTENTION_ROUTE, "roadmap", "scope.home"}
    assert daemon.reads.count("scope.home") == 2


def test_eviction_never_drops_the_pinned_route_at_the_smallest_capacity() -> None:
    """The boundary: room for exactly one route beside the pinned ones."""
    daemon = FakeDaemon()
    seam = seam_over(daemon, capacity=len(PINNED_ROUTES) + 1)
    for route in ("scope.home", "activity", "roadmap", "track"):
        seam.retarget(route)
        asyncio.run(seam.sync())
        assert set(seam.held_routes) == {ATTENTION_ROUTE, route}


def test_the_default_capacity_holds_more_than_the_pinned_routes() -> None:
    assert len(PINNED_ROUTES) < DEFAULT_ROUTE_CAPACITY


@pytest.mark.parametrize("capacity", [0, len(PINNED_ROUTES)])
def test_a_capacity_with_no_room_beside_the_pinned_routes_is_refused(capacity: int) -> None:
    with pytest.raises(ValueError, match="holds no route beside"):
        seam_over(FakeDaemon(), capacity=capacity)


def test_a_patch_fans_out_to_every_held_route_it_names() -> None:
    daemon = FakeDaemon()
    seam = seam_over(daemon)
    asyncio.run(seam.sync())
    seam.retarget("track")
    asyncio.run(seam.sync())
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)
    shared = patch("scope.home", key="TRK-7002", sequence=CURSOR + 1).model_copy(
        update={"routes": ("scope.home", "track")}
    )
    asyncio.run(seam.apply_patch(shared))
    assert heard == [("scope.home", "track")]
    for route in ("scope.home", "track"):
        held = seam.projection_for(route)
        assert held is not None
        assert {row.key for row in held.rows} == {"TRK-7001", "TRK-7002"}


def test_a_patch_for_an_unheld_route_is_ignored_and_heard_by_no_one() -> None:
    seam = seam_over(FakeDaemon())
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)
    asyncio.run(seam.apply_patch(patch("scope.home", key="TRK-7002", sequence=CURSOR + 1)))
    assert heard == []
    assert seam.held_routes == ()


def test_a_patch_the_held_read_already_states_is_not_applied_again() -> None:
    """A read that raced ahead of the push holds the patch already."""
    seam = seam_over(FakeDaemon())
    asyncio.run(seam.sync())
    before = seam.projection
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)
    asyncio.run(seam.apply_patch(patch("scope.home", key="TRK-7002", sequence=CURSOR)))
    assert heard == []
    assert seam.projection is before


@pytest.mark.parametrize("retained", [{8, 9}, {9}], ids=["replay", "snapshot_required"])
def test_a_reconnect_drops_the_hidden_routes_it_could_not_replay(retained: set[int]) -> None:
    """A reconnect answers the visible route alone; the others stand before the gap."""
    daemon = FakeDaemon()
    seam = seam_over(daemon)
    asyncio.run(seam.sync())
    seam.retarget("track")
    asyncio.run(seam.sync())
    seam.retarget("scope.home")
    negotiation = negotiate_reconnect(
        route="scope.home", client_cursor=CURSOR, server_cursor=CURSOR + 2, retained=retained
    )
    replay = negotiation.disposition is ReconnectDisposition.REPLAY
    daemon.reconnect_answer = {
        "negotiation": negotiation.model_dump(mode="json"),
        "patches": [
            patch("scope.home", key="TRK-7002", sequence=CURSOR + 1).model_dump(mode="json")
        ]
        if replay
        else [],
    }
    asyncio.run(seam.reconnect())
    assert seam.held_routes == ("scope.home",)
    assert seam.owed() == (ATTENTION_ROUTE,)
    home = seam.projection
    assert home is not None
    expected = {"TRK-7001", "TRK-7002"} if replay else {"TRK-7001"}
    assert {row.key for row in home.rows} == expected
