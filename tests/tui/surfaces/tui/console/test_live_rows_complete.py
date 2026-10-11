"""A row a live patch moved states the same facts a fresh read of the route states.

A keyed patch carries a record's status and revision, never its title, the record it is
filed under or the facts read off other records. A console that opened before the work
began builds every row from patches, so its Runs drew no Task and no minute, its
Milestone no Batch or Track, and its drafts no title. Each check here holds a route read
before the record moved, applies the move's patch, lets the console take what it owes,
and compares the held row to a fresh read of the same document.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    KeyedPatch,
    ProjectionRow,
    RouteProjection,
    build_route_projection,
    patches_for_event,
)
from eawf.kernel.projection.connection import read_method
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.app import Body, ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.live_reads import RUN_TIMELINE_READ
from eawf.surfaces.tui.console.renderers.activity import as_of, task_cell
from eawf.surfaces.tui.console.renderers.children import children
from eawf.surfaces.tui.console.renderers.task_progress import Progress, batches_done
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.tui.surfaces.tui.console.test_seam_pool import settle

AT: Final = datetime(2026, 10, 8, 14, 10, tzinfo=UTC)
SCOPE: Final = "SMOKE"
CONTAINER: Final = "eawf://SMOKE/SMOKE/SMOKE"
CURSOR: Final = 4
MOVED_AT: Final = "2026-10-08T14:10:00+00:00"


def _urn(collection: str, key: str) -> str:
    return f"{CONTAINER}/{collection}/{key}"


def _row(collection: str, key: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"urn": _urn(collection, key), "revision": 1, "status": status, **fields}


def _task(key: str, status: str, **fields: Any) -> dict[str, Any]:
    return _row("task", key, status, intent=f"Write notes/{key[-1]}.txt", **fields)


def _base() -> dict[str, Any]:
    """Return the tree as it stood when the console opened: a Track and a draft Task."""
    return {
        "track": {"TRK-SMOKE": _row("track", "TRK-SMOKE", "ACTIVE", title="Ship the smoke")},
        "task": {"SMOKE-0001": _task("SMOKE-0001", "DRAFT")},
    }


class DocumentDaemon:
    """A daemon that projects every route read from one document it holds.

    Attributes:
        document: The tree's document, moved by the test between reads.
        cursor: The ordinal the document stands at.
        reads: The route of every read issued, in order.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.cursor = CURSOR
        self.reads: list[str] = []
        self._routes = {read_method(route): route for route in ROUTE_COLLECTIONS}

    def fresh(self, route: str) -> RouteProjection:
        """Return *route* as a clean read of the document projects it now."""
        return build_route_projection(
            route=route,
            document=self.document,
            cursor=self.cursor,
            scope_id=SCOPE,
            generated_at=AT,
        )

    def move(self, collection: str, key: str, row: dict[str, Any]) -> tuple[KeyedPatch, ...]:
        """Store *row* under *key*, advance the cursor, and return the patches the move pushes.

        The patches are built by the daemon's own builder from the event the commit
        appends, so they state exactly what a live feed carries.
        """
        self.document.setdefault(collection, {})[key] = row
        self.cursor += 1
        event = Envelope(
            id=f"evt-{self.cursor}",
            kind=StoreKind.EVENT,
            scope_id=row["urn"],
            created_at=AT,
            summary=f"{key} {row['status']}",
            payload={
                "entity_ref": row["urn"],
                "to_status": row["status"],
                "revision_after": row["revision"],
                "canonical_sequence": self.cursor,
            },
        )
        return patches_for_event(event)

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str) -> dict[str, Any]:
        route = self._routes.get(method)
        if route is None:
            raise ConnectionError(f"{method} is not served")
        self.reads.append(route)
        return self.fresh(route).model_dump(mode="json")


class _Client:
    def __init__(self, daemon: DocumentDaemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method)


def _seam(daemon: DocumentDaemon, route: str, subject: str | None = None) -> ProjectionSeam:
    seam = ProjectionSeam(
        route=route,
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
    )
    seam.about(subject)
    return seam


def _held(seam: ProjectionSeam, route: str) -> RouteProjection:
    held = seam.projection_for(route)
    assert held is not None, f"{route} is not held"
    return held


def _row_of(projection: RouteProjection, key: str) -> ProjectionRow:
    return next(row for row in projection.rows if row.key == key)


def _facts(row: ProjectionRow) -> tuple[Any, ...]:
    return row.status.value, row.title, row.parent_key, dict(row.facts)


async def _live(
    daemon: DocumentDaemon,
    route: str,
    moves: list[tuple[str, str, dict[str, Any]]],
    subject: str | None = None,
) -> ProjectionSeam:
    """Read *route*, push each move's patch, and let the seam take what it owes after each."""
    seam = _seam(daemon, route, subject)
    await seam.sync()
    for collection, key, row in moves:
        for patch in daemon.move(collection, key, row):
            await seam.apply_patch(patch)
        await seam.sync()
    return seam


# ---- 1. Activity: a Run states its Task and the minute it last moved ----


def _run(status: str) -> dict[str, Any]:
    return _row(
        "run",
        "RUN-00000001",
        status,
        scope={"task_ref": _urn("task", "SMOKE-0001"), "purpose": "implement"},
        created_at=MOVED_AT,
        updated_at=MOVED_AT,
    )


def test_a_run_patched_in_states_its_task_and_minute_as_a_fresh_read_does() -> None:
    daemon = DocumentDaemon(_base())
    seam = asyncio.run(_live(daemon, "activity", [("run", "RUN-00000001", _run("COMPLETED"))]))
    live = _row_of(_held(seam, "activity"), "RUN-00000001")
    fresh = _row_of(daemon.fresh("activity"), "RUN-00000001")
    assert _facts(live) == _facts(fresh)
    assert task_cell(live) == "SMOKE-0001 Write notes/1.txt"
    assert as_of(live) == "14:10"


def test_a_run_moved_twice_states_the_newest_minute() -> None:
    later = {**_run("COMPLETED"), "revision": 2, "updated_at": "2026-10-08T14:12:00+00:00"}
    daemon = DocumentDaemon({**_base(), "run": {"RUN-00000001": _run("RUNNING")}})
    seam = asyncio.run(_live(daemon, "activity", [("run", "RUN-00000001", later)]))
    assert as_of(_row_of(_held(seam, "activity"), "RUN-00000001")) == "14:12"


# ---- 2. Milestone: its Track, its Batches and their Tasks ----


def _milestone_moves() -> list[tuple[str, str, dict[str, Any]]]:
    milestone = _row(
        "milestone",
        "MLS-0001",
        "ACTIVE",
        title="Write six notes",
        primary_track_ref=_urn("track", "TRK-SMOKE"),
    )
    batch = _row("batch", "BAT-0001", "COMPLETED", milestone_ref=_urn("milestone", "MLS-0001"))
    task = {**_task("SMOKE-0001", "COMPLETED"), "revision": 2, "batch_ref": batch["urn"]}
    return [
        ("milestone", "MLS-0001", milestone),
        ("batch", "BAT-0001", batch),
        ("task", "SMOKE-0001", task),
    ]


def test_a_milestone_patched_in_lists_its_track_batches_and_tasks_as_a_fresh_read_does() -> None:
    daemon = DocumentDaemon(_base())
    seam = asyncio.run(_live(daemon, "milestone", _milestone_moves(), subject="MLS-0001"))
    live, fresh = _held(seam, "milestone"), daemon.fresh("milestone")
    for key in ("MLS-0001", "BAT-0001", "SMOKE-0001"):
        assert _facts(_row_of(live, key)) == _facts(_row_of(fresh, key))
    model = build_acceptance_view(live)
    subject = _row_of(live, "MLS-0001")
    assert subject.parent_key == "TRK-SMOKE"
    assert [b.key for b in children(model.rows, "MLS-0001", Epoch2Collection.BATCH)] == ["BAT-0001"]
    tally, batches = Progress.of(model.rows).milestones(("MLS-0001",))
    assert (tally.completed, tally.total) == (1, 1)
    assert batches_done(batches) == "1 of 1 batch done"


# ---- 3. Backlog: a draft is titled by its intent ----


def test_a_draft_patched_in_is_titled_by_its_intent_as_a_fresh_read_does() -> None:
    daemon = DocumentDaemon({"track": _base()["track"]})
    seam = asyncio.run(
        _live(daemon, "backlog", [("task", "SMOKE-0002", _task("SMOKE-0002", "DRAFT"))])
    )
    live = _row_of(_held(seam, "backlog"), "SMOKE-0002")
    assert _facts(live) == _facts(_row_of(daemon.fresh("backlog"), "SMOKE-0002"))
    assert live.title == "Write notes/2.txt"


# ---- the patch still lands at once, and a read older than it is owed again ----


def test_the_patched_status_is_held_before_the_read_lands() -> None:
    daemon = DocumentDaemon(_base())

    async def drive() -> tuple[str | None, bool]:
        seam = _seam(daemon, "activity")
        await seam.sync()
        for patch in daemon.move("run", "RUN-00000001", _run("RUNNING")):
            await seam.apply_patch(patch)
        row = _row_of(_held(seam, "activity"), "RUN-00000001")
        return row.status.value, seam.route_owed() and not seam.frame_owed()

    assert asyncio.run(drive()) == ("RUNNING", True)


def test_a_read_older_than_the_patch_is_owed_again_and_a_current_one_is_not() -> None:
    daemon = DocumentDaemon(_base())

    async def drive() -> tuple[bool, bool]:
        seam = _seam(daemon, "activity")
        await seam.sync()
        for patch in daemon.move("run", "RUN-00000001", _run("RUNNING")):
            await seam.apply_patch(patch)
        daemon.cursor -= 1
        await seam.load("activity")
        stale = seam.route_owed()
        daemon.cursor += 1
        await seam.load("activity")
        return stale, seam.route_owed()

    assert asyncio.run(drive()) == (True, False)


def test_an_unpatched_route_owes_no_second_read() -> None:
    daemon = DocumentDaemon(_base())

    async def drive() -> None:
        seam = _seam(daemon, "activity")
        await seam.sync()
        await seam.sync()

    asyncio.run(drive())
    assert daemon.reads.count("activity") == 1


# ---- the console on screen paints the completed row ----


def test_the_activity_frame_paints_the_task_after_a_patch() -> None:
    daemon = DocumentDaemon(_base())
    seam = _seam(daemon, "activity")

    async def drive() -> str:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        async with app.run_test(size=SIZES[1]) as pilot:
            app.press_key("g")
            app.press_key("a")
            await settle(app, pilot)
            for patch in daemon.move("run", "RUN-00000001", _run("COMPLETED")):
                await seam.apply_patch(patch)
            await settle(app, pilot)
            return "\n".join(app.query_one("#body", Body).rows)

    painted = asyncio.run(drive())
    assert "SMOKE-0001 Write" in painted
    assert "14:10" in painted


# ---- 4. a live re-read for a route the operator left is no failure ----


def test_a_live_reread_for_a_route_left_logs_no_failure(caplog: pytest.LogCaptureFixture) -> None:
    daemon = DocumentDaemon(_base())
    seam = _seam(daemon, "scope.home")

    async def drive() -> None:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            await app._reload_live((RUN_TIMELINE_READ,))

    with caplog.at_level(logging.WARNING):
        asyncio.run(drive())
    assert not [r for r in caplog.records if "live re-read failed" in r.getMessage()]
