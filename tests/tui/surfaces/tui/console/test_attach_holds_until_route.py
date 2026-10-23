"""A launch holds its attach frame until the route it opens on is read.

The daemon a launch reaches may still be starting, so the first read of the route can
fail while the pinned Attention register, read just after, succeeds. The console keeps
the attach frame up through that -- naming the daemon's progress and the time spent, and
asking for the route again -- rather than dropping to a route frame that holds nothing.
Past the deadline it lands on the offline frame naming the commands that bring the
daemon back, and a link that comes back reads the route that failed again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import read_method
from eawf.kernel.projection.registers import ATTENTION_ROUTE
from eawf.surfaces.tui.console.app import (
    ATTACH_DEADLINE_SECONDS,
    ATTACH_RETRY_SECONDS,
    ConsoleApp,
)
from eawf.surfaces.tui.console.attach import (
    DAEMON_COMMANDS,
    NO_SNAPSHOT,
    OFFLINE,
    AttachStep,
    offline_state,
    with_entry_state,
)
from eawf.surfaces.tui.console.chrome import ConsoleChrome, load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.keymap import ENTRY_ROUTE
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES

SCOPE = "ABC"
AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
HOME = "scope.home"
TRACE = (AttachStep(name="registered root", result="named outright · --workspace"),)
_ROUTES = {read_method(route): route for route in ROUTE_COLLECTIONS}


class _Daemon:
    """A binding call serving every route read, refusing the first *refusals* home reads."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.asked: list[str] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        route = _ROUTES.get(method)
        if route is None:
            raise ConnectionRefusedError(f"{method} is not served here")
        self.asked.append(route)
        if route == HOME and self.refusals > 0:
            self.refusals -= 1
            raise ConnectionRefusedError("the daemon is still starting")
        projection = build_route_projection(
            route=route, document={}, cursor=1, scope_id=SCOPE, generated_at=AT
        )
        return projection.model_dump(mode="json")

    def home_reads(self) -> int:
        return self.asked.count(HOME)


class _Recording(ConsoleApp):
    """A console that keeps every frame it paints, so what stood between two is seen."""

    def __init__(self, **options: Any) -> None:
        self.painted: list[list[str]] = []
        super().__init__(**options)

    def render_frame(self) -> None:
        super().render_frame()
        self.painted.append(list(self.frame_rows))


def _chrome() -> ConsoleChrome:
    """Return the chrome a launch opens with: its offline frame filled as launch fills it."""
    chrome = load_chrome()
    return with_entry_state(chrome, offline_state(chrome, None))


def _console(
    daemon: _Daemon, *, attach: Sequence[AttachStep] = TRACE
) -> tuple[_Recording, ProjectionSeam, FakeClock]:
    seam = ProjectionSeam(route=HOME, scope_id=SCOPE, clock=lambda: AT)
    seam._binding.call = daemon  # type: ignore[method-assign]
    clock = FakeClock()
    app = _Recording(chrome=_chrome(), seam=seam, clock=clock, attach=attach)
    return app, seam, clock


async def _settle(app: ConsoleApp, pilot: Any) -> None:
    """Let every read the console scheduled finish, and the frames they owe paint."""
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def _text(rows: list[str]) -> str:
    return "\n".join(rows)


def _attaching(rows: list[str]) -> bool:
    return rows[0].rstrip().endswith("◈ RESOLVING")


def test_a_refused_first_read_keeps_the_attach_frame_then_lands_on_the_route() -> None:
    """The register arriving first is not the route; the frame waits, asks again, lands."""
    daemon = _Daemon(refusals=1)
    app, seam, clock = _console(daemon)

    async def body() -> tuple[list[str], list[str], bool]:
        async with app.run_test(size=SIZES[0]) as pilot:
            await _settle(app, pilot)
            waiting = list(app.frame_rows)
            attention_held = seam.projection_for(ATTENTION_ROUTE) is not None
            clock.advance(ATTACH_RETRY_SECONDS)
            app.tick()
            await _settle(app, pilot)
            return waiting, list(app.frame_rows), attention_held

    waiting, landed, attention_held = asyncio.run(body())
    assert attention_held, "the pinned register was read while the route was refused"
    assert _attaching(waiting)
    assert "daemon" in _text(waiting) and "not answering yet · 1 tries" in _text(waiting)
    assert not _attaching(landed)
    assert seam.projection_for(HOME) is not None
    assert daemon.home_reads() == 2
    between = app.painted[: app.painted.index(landed)]
    assert between, "the attach frame was painted before the route landed"
    assert all(_attaching(rows) for rows in between)
    assert not any("NOT HELD" in _text(rows) for rows in app.painted)
    assert not any("DISCONNECTED" in rows[0] for rows in between)


def test_a_route_read_at_once_lands_with_no_retry() -> None:
    """The boundary: the first read answers, so no second is asked and nothing waits."""
    daemon = _Daemon(refusals=0)
    app, seam, _clock = _console(daemon)

    async def body() -> list[str]:
        async with app.run_test(size=SIZES[0]) as pilot:
            await _settle(app, pilot)
            app.tick()
            await _settle(app, pilot)
            return list(app.frame_rows)

    landed = asyncio.run(body())
    assert not _attaching(landed)
    assert seam.projection_for(HOME) is not None
    assert daemon.home_reads() == 1
    assert app._attach_misses == 0
    assert not any("NOT HELD" in _text(rows) for rows in app.painted)


def test_the_attach_frame_states_the_daemon_step_and_ticks_its_elapsed_time() -> None:
    """The step is the read's live progress, not a fixed claim, and the clock moves it."""
    daemon = _Daemon(refusals=10_000)
    app, _seam, clock = _console(daemon)

    async def body() -> tuple[list[str], list[str]]:
        async with app.run_test(size=SIZES[1]) as pilot:
            first = list(app.painted[0])
            await _settle(app, pilot)
            clock.advance(2.0)
            app.tick()
            await _settle(app, pilot)
            return first, list(app.frame_rows)

    first, later = asyncio.run(body())
    assert _attaching(first) and _attaching(later)
    assert "named outright · --workspace" in _text(first)
    assert f"starting · {HOME} not asked yet" in _text(first)
    assert "contacted" not in _text(first)
    assert "~0.0s" in _text(first)
    assert "~2.0s" in _text(later)


def test_past_the_deadline_the_launch_lands_offline_naming_the_daemon_commands() -> None:
    """A route refused throughout is asked again on each step, then the frame gives up."""
    daemon = _Daemon(refusals=10_000)
    app, _seam, clock = _console(daemon)
    offline = next(i for i, state in enumerate(app.fixture.proto.entry) if state.id == OFFLINE)

    async def body() -> tuple[bool, list[str]]:
        async with app.run_test(size=SIZES[2]) as pilot:
            await _settle(app, pilot)
            steps = int(ATTACH_DEADLINE_SECONDS / ATTACH_RETRY_SECONDS) - 1
            for _ in range(steps):
                clock.advance(ATTACH_RETRY_SECONDS)
                app.tick()
                await _settle(app, pilot)
            before = _attaching(app.frame_rows)
            clock.advance(ATTACH_RETRY_SECONDS)
            app.tick()
            await _settle(app, pilot)
            return before, list(app.frame_rows)

    still_attaching, landed = asyncio.run(body())
    assert still_attaching, "one step short of the deadline the frame still waits"
    assert app.session.route == ENTRY_ROUTE
    assert app.session.entry_sel == offline
    assert daemon.home_reads() > 2, "the route was asked again while the daemon started"
    text = _text(landed)
    assert NO_SNAPSHOT in text
    assert "eawf daemon status" in text
    assert "eawf daemon restart" in text


def test_a_link_coming_back_reads_again_the_route_that_failed() -> None:
    """Degraded clearing is what a returning daemon looks like; the failed route is owed."""
    daemon = _Daemon(refusals=1)
    app, seam, _clock = _console(daemon, attach=())

    async def body() -> tuple[bool, bool]:
        async with app.run_test(size=SIZES[0]) as pilot:
            await _settle(app, pilot)
            before = seam.projection_for(HOME) is not None
            await seam.binding._set_degraded(True)
            await seam.binding._on_subscription_connected(resync=False)
            await _settle(app, pilot)
            return before, seam.projection_for(HOME) is not None

    before, after = asyncio.run(body())
    assert not before, "the first read was refused and is not asked again on its own"
    assert after
    assert daemon.home_reads() == 2


def test_a_console_off_the_attach_path_drops_the_route_frame_without_waiting() -> None:
    """No resolution trace, no attach frame: the old landing stands for such a console."""
    daemon = _Daemon(refusals=1)
    app, _seam, _clock = _console(daemon, attach=())

    async def body() -> list[str]:
        async with app.run_test(size=SIZES[0]) as pilot:
            await _settle(app, pilot)
            return list(app.frame_rows)

    rows = asyncio.run(body())
    assert not _attaching(rows)
    assert "NOT HELD" in _text(rows)
    assert daemon.home_reads() == 1


def test_an_attach_trace_without_a_seam_draws_no_attach_frame() -> None:
    """The attach frame waits on a seam's read, so a console with none never holds one."""
    app = ConsoleApp(chrome=load_chrome(), clock=FakeClock(), attach=TRACE)
    assert app._attach_trace is None


def test_retry_route_owes_only_the_visible_routes_failed_read() -> None:
    """A failed register read stays failed; the visible route's own read is owed again."""
    seam = ProjectionSeam(route=HOME, scope_id=SCOPE, clock=lambda: AT)
    seam._failed.update({(HOME, None), (ATTENTION_ROUTE, None)})
    assert seam.owed() == ()
    seam.retry_route()
    assert seam.owed() == (HOME,)
    seam.retry_route()
    assert seam.owed() == (HOME,), "asking again for a read already owed changes nothing"


def test_the_offline_frame_names_the_daemon_commands() -> None:
    """Each command is printed on a line of its own beneath what it does."""
    state = offline_state(load_chrome(), None)
    assert not state.rows
    assert state.tail[0] == NO_SNAPSHOT
    for command in DAEMON_COMMANDS:
        assert command.purpose in state.tail
        assert f"  {command.line}" in state.tail
