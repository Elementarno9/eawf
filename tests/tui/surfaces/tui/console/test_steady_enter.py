"""Enter paints the frame it opens once, never a frame that holds nothing on the way.

The route on screen stays painted while the next route's own read is in flight; the
next frame is painted when its rows arrive, and the reads that hang off them follow
without holding the rows back. A read that outlasts the grace gives way to a frame
naming the record being loaded, a refused read still says nothing is held, and a caret
that rests on a row has the route its Enter opens read ahead.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, KeyedPatch, PatchEntry
from eawf.kernel.projection.connection import read_method
from eawf.kernel.projection.liveness import HeldLiveness
from eawf.kernel.projection.registers import ATTENTION_ROUTE
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.app import (
    LOADING_GRACE_SECONDS,
    PREFETCH_REST_SECONDS,
    ConsoleApp,
)
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.renderers.milestone import NO_BUNDLE
from eawf.surfaces.tui.console.seam import PREFETCH_CAPACITY, ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity
from eawf.workflow.projection.acceptance import (
    MILESTONE_ACCEPTANCE_METHOD,
    MilestoneAcceptanceRecord,
)
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_enter_opened_cards import _approval, _bundle

#: The Milestone leaf scope home's caret lands on, and the route its Enter opens.
LEAF = "MLS-0100"
TARGET = "milestone"
NOT_HELD = "NOT HELD"
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

_ROUTES = {read_method(route): route for route in ROUTE_COLLECTIONS}


class _Daemon:
    """A binding call serving every route read and the acceptance read, each after a delay.

    Every other read waits ``side_delay`` and is refused, standing in for a slow live read.

    Attributes:
        reads: The route of every route read answered, in order.
        accepted: Whether the acceptance read has answered.
        refused: How many other reads have been refused.
    """

    def __init__(
        self,
        *,
        route_delay: float = 0.0,
        side_delay: float = 0.0,
        failing: frozenset[str] = frozenset(),
        slow: frozenset[str] = frozenset({TARGET}),
        sealed: bool = False,
    ) -> None:
        self.route_delay = route_delay
        self.side_delay = side_delay
        self.failing = failing
        self.slow = slow
        self.sealed = sealed
        self.reads: list[str] = []
        self.accepted = False
        self.refused = 0

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        route = _ROUTES.get(method)
        if route is not None:
            if route in self.slow:
                await asyncio.sleep(self.route_delay)
            if route in self.failing:
                raise ConnectionError(f"{route} could not be read")
            self.reads.append(route)
            return bodies._projection(route).model_dump(mode="json")
        if method == MILESTONE_ACCEPTANCE_METHOD:
            await asyncio.sleep(self.side_delay)
            self.accepted = True
            record = MilestoneAcceptanceRecord(milestone_key=params["milestone_key"])
            if self.sealed:
                record = record.model_copy(update={"bundle": _bundle(), "approval": _approval()})
            return record.model_dump(mode="json")
        await asyncio.sleep(self.side_delay)
        self.refused += 1
        raise ConnectionError(f"{method} is not served here")


def _app(daemon: _Daemon) -> ConsoleApp:
    """Return a console on scope home whose seam reads through ``daemon``, on a held clock."""
    seam = ProjectionSeam(route="scope.home", scope_id=bodies.SCOPE, state_path=None)
    seam._binding.call = daemon  # type: ignore[method-assign]
    return ConsoleApp(clock=FakeClock(), seam=seam)


def _lapse(app: ConsoleApp, by: float) -> None:
    """Move the held clock on by ``by`` seconds and run the sweep a live clock would."""
    clock = app.clock
    assert isinstance(clock, FakeClock)
    clock.advance(by)
    app.tick()


async def _settle(app: ConsoleApp, pilot: Any) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _sample(app: ConsoleApp, pilot: Any) -> list[list[str]]:
    """Return the painted frame every 10 ms until every read in flight has ended."""
    frames = [list(app.frame_rows)]
    done = asyncio.ensure_future(app.workers.wait_for_complete())
    while not done.done():
        await asyncio.sleep(0.01)
        frames.append(list(app.frame_rows))
    await pilot.pause()
    frames.append(list(app.frame_rows))
    return frames


def _distinct(frames: list[list[str]]) -> list[list[str]]:
    """Return the frames in the order painted, each run of one frame counted once."""
    out: list[list[str]] = []
    for frame in frames:
        if not out or out[-1] != frame:
            out.append(frame)
    return out


def _text(rows: list[str]) -> str:
    return "\n".join(rows)


# ---------- Enter holds the frame until the next one's rows arrive ----------


def test_enter_paints_the_drilled_frame_once_and_never_not_held() -> None:
    daemon = _Daemon(route_delay=0.1)

    async def drive() -> tuple[list[str], list[list[str]], ConsoleApp]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            home = list(app.frame_rows)
            app.press_key("Enter")
            # pressed again before the rows arrive: no toast that outlives the load
            app.press_key("Enter")
            frames = await _sample(app, pilot)
            return home, frames, app

    home, frames, app = asyncio.run(drive())
    assert LEAF in _text(home)
    assert not any(NOT_HELD in _text(frame) for frame in frames)
    painted = _distinct(frames)
    assert len(painted) <= 2
    assert painted[0] == home
    assert (app.session.route, app.session.subj_id) == (TARGET, LEAF)
    assert LEAF in _text(painted[-1]) and painted[-1] != home
    assert not [toast for toast in app.session.toasts if toast.sev is Severity.WARN]
    assert daemon.reads.count(TARGET) == 1


def test_the_route_rows_draw_before_a_slow_live_read_that_hangs_off_them() -> None:
    daemon = _Daemon(side_delay=1.0)
    run = "RUN-00000001"

    async def drive() -> tuple[bool, bool, str]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            home = list(app.frame_rows)
            app.reset(SessionSetup(route="run.detail", subjId=run))
            for _ in range(80):
                await asyncio.sleep(0.01)
                if app.frame_rows != home:
                    break
            drawn, early = app.frame_rows != home, daemon.refused == 0
            await _settle(app, pilot)
            return drawn, early, _text(app.frame_rows)

    drawn, early, settled = asyncio.run(drive())
    assert drawn, "the route rows waited on the live reads"
    assert early
    assert run in settled


def test_a_milestone_opened_on_no_subject_draws_its_acceptance_in_the_first_frame() -> None:
    """The acceptance owed for the caret's row is read in the same sync, before any paint."""
    daemon = _Daemon(sealed=True)

    async def drive() -> tuple[list[list[str]], list[str]]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            app.reset(SessionSetup(route=TARGET))
            frames = await _sample(app, pilot)
            await app.workers.wait_for_complete()
            return frames, list(app.frame_rows)

    frames, settled = asyncio.run(drive())
    assert daemon.accepted
    assert not any(NO_BUNDLE in _text(frame) for frame in frames), "a frame drew no bundle"
    assert "revision 1 · sealed" in _text(frames[-1])
    assert frames[-1] == settled


def test_a_refused_route_read_still_says_nothing_is_held() -> None:
    """The error path: holding the frame is for a read in flight, not one that failed."""
    daemon = _Daemon(route_delay=0.05, failing=frozenset({TARGET}))

    async def drive() -> list[list[str]]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            app.press_key("Enter")
            return await _sample(app, pilot)

    frames = asyncio.run(drive())
    assert NOT_HELD not in _text(frames[0])
    assert NOT_HELD in _text(frames[-1])


@pytest.mark.parametrize(("waited", "named"), [(0.0, False), (0.29, False), (0.31, True)])
def test_a_read_past_the_grace_paints_a_frame_naming_the_record(waited: float, named: bool) -> None:
    """Boundary: up to the grace the frame before stays; at the grace the record is named."""
    assert LOADING_GRACE_SECONDS == 0.3
    daemon = _Daemon(route_delay=0.3)

    async def drive() -> tuple[list[str], list[str], list[list[str]]]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            home = list(app.frame_rows)
            app.press_key("Enter")
            _lapse(app, waited)
            during = list(app.frame_rows)
            return home, during, await _sample(app, pilot)

    home, during, frames = asyncio.run(drive())
    assert not any(NOT_HELD in _text(frame) for frame in [during, *frames])
    if named:
        assert f"Loading {LEAF}…" in _text(during)
        assert LEAF in during[0], "the breadcrumb does not name the record being loaded"
    else:
        assert during == home
    assert f"Loading {LEAF}" not in _text(frames[-1])
    assert LEAF in _text(frames[-1])


# ---------- the caret's drill target is read ahead ----------


@pytest.mark.parametrize(("rested", "read"), [(0.0, False), (0.09, False), (0.11, True)])
def test_a_caret_reads_its_target_ahead_only_once_it_rests(rested: float, read: bool) -> None:
    """Boundary: a caret that has not rested for the whole rest reads nothing ahead."""
    assert PREFETCH_REST_SECONDS == 0.1
    daemon = _Daemon()

    async def drive() -> list[str]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            _lapse(app, rested)
            await _settle(app, pilot)
            return list(daemon.reads)

    assert (TARGET in asyncio.run(drive())) is read


def test_a_rested_caret_reads_its_target_ahead_so_enter_paints_it_at_once() -> None:
    daemon = _Daemon(route_delay=0.15)

    async def drive() -> tuple[list[str], list[str], list[str], list[str]]:
        app = _app(daemon)
        async with app.run_test(size=SIZES[1]) as pilot:
            await _settle(app, pilot)
            _lapse(app, PREFETCH_REST_SECONDS)
            await _settle(app, pilot)
            ahead = list(daemon.reads)
            home = list(app.frame_rows)
            app.press_key("Enter")
            first = list(app.frame_rows)
            await _settle(app, pilot)
            return ahead, home, first, list(daemon.reads)

    ahead, home, first, reads = asyncio.run(drive())
    assert TARGET in ahead
    assert first != home
    assert LEAF in _text(first) and NOT_HELD not in _text(first)
    assert reads.count(TARGET) == 1


# ---------- the seam's read-ahead cache ----------


def _seam(daemon: _Daemon) -> ProjectionSeam:
    seam = ProjectionSeam(route="scope.home", scope_id=bodies.SCOPE, state_path=None)
    seam._binding.call = daemon  # type: ignore[method-assign]
    return seam


def test_opening_a_route_read_ahead_owes_no_read() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    asyncio.run(seam.prefetch(TARGET, LEAF))
    seam.retarget(TARGET)
    seam.about(LEAF)
    assert not seam.route_owed()
    assert TARGET not in seam.owed()
    assert daemon.reads == [TARGET]


def test_a_route_read_ahead_for_another_record_is_not_opened() -> None:
    seam = _seam(_Daemon())
    asyncio.run(seam.prefetch(TARGET, LEAF))
    seam.retarget(TARGET)
    seam.about("MLS-0101")
    assert seam.route_owed()


def test_the_read_ahead_cache_keeps_only_the_newest() -> None:
    seam = _seam(_Daemon())
    keys = [f"MLS-{n:04d}" for n in range(PREFETCH_CAPACITY + 1)]
    for key in keys:
        asyncio.run(seam.prefetch(TARGET, key))
    assert list(seam._prefetched) == [(TARGET, key) for key in keys[1:]]


def test_a_held_or_unserved_route_is_not_read_ahead() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    asyncio.run(seam.sync())
    asyncio.run(seam.prefetch("scope.home", None))
    asyncio.run(seam.prefetch("settings", None))
    assert daemon.reads == ["scope.home", ATTENTION_ROUTE]
    assert not seam._prefetched


def test_a_patch_to_a_route_read_ahead_drops_it() -> None:
    seam = _seam(_Daemon())
    asyncio.run(seam.prefetch(TARGET, LEAF))
    patch = KeyedPatch(
        schema_version="1.0",
        projection_kind=bodies._projection(TARGET).header.projection_kind,
        routes=(TARGET,),
        scope_id=bodies.SCOPE,
        canonical_sequence=bodies.CURSOR + 1,
        entries=(
            PatchEntry(
                key=LEAF,
                urn=f"urn:eawf:{bodies.SCOPE}:milestone:{LEAF}",
                collection=Epoch2Collection.MILESTONE,
                revision=2,
                status="COMPLETED",
            ),
        ),
    )
    asyncio.run(seam.apply_patch(patch))
    assert not seam._prefetched


def test_a_failed_read_ahead_is_raised_and_caches_nothing() -> None:
    seam = _seam(_Daemon(failing=frozenset({TARGET})))
    with pytest.raises(ConnectionError):
        asyncio.run(seam.prefetch(TARGET, LEAF))
    assert not seam._prefetched


# ---------- a re-read that found the same answer repaints nothing ----------


def test_a_stall_read_again_with_the_same_stalls_is_the_same_answer() -> None:
    first = HeldLiveness(stalls=(), read_at=AT)
    assert first == HeldLiveness(stalls=(), read_at=AT + timedelta(seconds=1))
