"""The inspector pane: at 180 columns a list route draws what Enter would open beside it.

Activity, Backlog and Attention set a pane to the right of their list whose rows are the
frame Enter opens from the row under the cursor, drawn at the pane's width, while Enter
still opens that frame whole. The pane's route is a held read the feed patches in place,
so a patch to the inspected record moves the pane without it ever standing empty. Below
180 columns no frame changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, KeyedPatch, PatchEntry
from eawf.kernel.projection.connection import read_method
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import INSPECT_FROM, View
from eawf.surfaces.tui.console.inspector import (
    DIVIDER,
    PANE_MIN_W,
    beside,
    entered,
    inspects,
    pane_lines,
    pane_width,
)
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays import render_overlay
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.seam import INSPECTOR_READ, ProjectionSeam
from eawf.surfaces.tui.console.session import Session, SessionSetup
from eawf.surfaces.tui.console.tokens import BRAND
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies

W, H = 180, 40
RUN = "RUN-00000001"
OTHER_RUN = "RUN-00000002"
DETAIL = "run.detail"
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_ROUTES = {read_method(route): route for route in ROUTE_COLLECTIONS}


class _Host:
    """The dispatcher's host: a held clock and a quit nothing asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """End nothing: no key here quits."""


def _list_view(route: str, *, w: int = W, gutter: int = 0) -> View:
    """Return a linked view of list ``route`` over the probe tree, its frame drawn once."""
    session = Session()
    session.route = route
    held: dict[str, Any] = {}
    if route == "backlog":
        drafts = bodies._backlog_document("DRAFT", "DEFERRED")
        held["projection"] = build_spine_view(bodies._projection(route, drafts))
    else:
        held["register"] = build_register_view(bodies._projection(route))
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=H,
        linked=True,
        principal=bodies.ME,
        attention=bodies._attention(),
        gutter=gutter,
        **held,
    )
    compose_frame(view)
    return view


def _ctx(view: View, session: Session | None = None) -> Ctx:
    """Return the context a key on ``view``'s frame acts in."""
    projection = bodies._projection("attention") if view.session.route == "attention" else None
    return Ctx(
        session=session or view.session,
        fixture=view.fixture,
        host=_Host(),
        w=view.w,
        h=view.h,
        projection=view.projection,
        attention=projection,
        principal=view.principal,
    )


def _pressed(view: View) -> Session:
    """Return a copy of ``view``'s session after Enter really is pressed on it."""
    session = view.session.model_copy(deep=True)
    dispatch(_ctx(view, session), "Enter")
    return session


def _drilled_view(view: View, opened: Session) -> View:
    """Return the view the inspector draws for ``opened``: its route's own read model."""
    if opened.route == view.session.route:
        return replace(view, session=opened)
    return replace(
        view,
        session=opened,
        projection=build_spine_view(bodies._projection(opened.route)),
        register=None,
    )


def _pane(row: str, w: int = W) -> str:
    """Return the pane's part of a joined row: what follows the list and its divider."""
    return row[w - pane_width(w) :]


def _heading(leaf: str) -> str:
    """Return the pane's first row naming ``leaf``: its gutter and the leaf, padded."""
    return f" {leaf}".ljust(pane_width(W))


# ---------- the pane's geometry ----------


@pytest.mark.parametrize(
    ("w", "pane"),
    [(180, PANE_MIN_W), (201, 80), (203, 80), (204, 81), (240, 95), (400, 159)],
)
def test_the_pane_takes_two_fifths_never_under_the_least_width(w: int, pane: int) -> None:
    assert pane_width(w) == pane
    assert w - pane_width(w) - 1 > 0


@pytest.mark.parametrize(("columns", "gutter", "inspected"), [(179, 0, False), (180, 0, True)])
def test_the_pane_opens_at_180_columns_and_not_one_column_under(
    columns: int, gutter: int, inspected: bool
) -> None:
    assert INSPECT_FROM == 180
    assert inspects(_list_view("activity", w=columns - 2 * gutter, gutter=gutter)) is inspected


def test_the_breakpoint_counts_the_gutters_as_every_layout_does() -> None:
    assert inspects(_list_view("activity", w=178, gutter=1))


@pytest.mark.parametrize("route", ["run.detail", "track", "history"])
def test_only_the_three_list_routes_set_a_pane(route: str) -> None:
    session = Session()
    session.route = route
    view = View(session=session, fixture=Fixture.from_chrome(load_chrome()), w=W, h=H)
    assert not inspects(view)


@pytest.mark.parametrize("field", ["overlay", "prefix", "typing"])
def test_no_pane_while_something_else_owns_the_keys(field: str) -> None:
    view = _list_view("activity")
    setattr(view.session, field, {"overlay": "draft", "prefix": "g", "typing": True}[field])
    assert not inspects(view)


# ---------- the pane is the frame Enter opens (CR-2, CON-018) ----------


def test_activity_pane_is_the_run_frame_enter_opens_at_the_pane_width() -> None:
    view = _list_view("activity")
    opened = entered(_ctx(view))
    assert opened is not None
    assert (opened.route, opened.subj_id) == (DETAIL, view.session.sel_id) == (DETAIL, RUN)
    frame = compose_frame(replace(view, inspector=_drilled_view(view, opened)))
    # what the pane draws is what pressing Enter for real draws, at the pane's width
    pressed = _pressed(view)
    expected = render_route(
        replace(_drilled_view(view, pressed), w=pane_width(W), h=H - 1, inspector=None)
    )[:-1]
    pane = [_pane(row) for row in frame[1:-1]]
    assert pane[0] == _heading(RUN)
    assert pane[1:] == expected[1:]
    assert all(row[W - pane_width(W) - 1] == DIVIDER for row in frame[1:-1])
    assert len(frame) == H and {len(row) for row in frame} == {W}


def test_enter_on_the_inspected_list_still_opens_the_route_the_pane_draws() -> None:
    view = _list_view("activity")
    opened = entered(_ctx(view))
    assert opened is not None
    compose_frame(replace(view, inspector=_drilled_view(view, opened)))
    dispatch(_ctx(view), "Enter")
    assert (view.session.route, view.session.subj_id) == (opened.route, opened.subj_id)


def test_finding_what_enter_opens_leaves_the_session_on_screen_untouched() -> None:
    view = _list_view("activity")
    before = view.session.model_dump()
    assert entered(_ctx(view)) is not None
    assert view.session.model_dump() == before


def test_backlog_pane_is_the_draft_card_enter_opens() -> None:
    view = _list_view("backlog")
    opened = entered(_ctx(view))
    assert opened is not None and opened.overlay == "draft"
    frame = compose_frame(replace(view, inspector=_drilled_view(view, opened)))
    pressed = _pressed(view)
    assert pressed.overlay == opened.overlay == "draft"
    card = replace(view, session=pressed, w=pane_width(W), h=H - 1)
    pane = [_pane(row) for row in frame[1:-1]]
    assert pane[0] == _heading("draft · TSK-0100")
    assert pane[1:] == render_overlay("draft", card)[1:-1]


def test_attention_pane_is_the_card_enter_opens_on_the_row() -> None:
    view = _list_view("attention")
    opened = entered(_ctx(view))
    pressed = _pressed(view)
    assert opened is not None and opened.overlay is not None
    assert (opened.route, opened.overlay) == (pressed.route, pressed.overlay)
    frame = compose_frame(replace(view, inspector=_drilled_view(view, opened)))
    card = replace(view, session=pressed, w=pane_width(W), h=H - 1)
    pane = [_pane(row) for row in frame[1:-1]]
    assert pane[0] == _heading("consequence · ACT-0001")
    assert pane[1:] == render_overlay(opened.overlay, card)[1:-1]


def test_an_empty_list_opens_nothing_so_no_pane_is_set() -> None:
    session = Session()
    session.route = "activity"
    document = {**bodies.DOCUMENT, "run": {}}
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=W,
        h=H,
        linked=True,
        register=build_register_view(bodies._projection("activity", document)),
    )
    compose_frame(view)
    assert entered(_ctx(view)) is None


def test_the_header_and_keybar_span_the_frame_and_offer_enter() -> None:
    view = _list_view("activity")
    opened = entered(_ctx(view))
    assert opened is not None
    alone = compose_frame(replace(view))
    joined = compose_frame(replace(view, inspector=_drilled_view(view, opened)))
    assert joined[0] == alone[0]
    assert joined[-1] == alone[-1] and "Enter" in joined[-1]


@pytest.mark.parametrize(
    ("route", "leaf"),
    [
        ("activity", RUN),
        ("backlog", "draft · TSK-0100"),
        ("attention", "consequence · ACT-0001"),
    ],
)
def test_the_pane_heads_with_its_subject_alone_and_keeps_its_rows(route: str, leaf: str) -> None:
    view = _list_view(route)
    opened = entered(_ctx(view))
    assert opened is not None
    inspected = replace(view, inspector=_drilled_view(view, opened))
    pane = pane_lines(inspected)
    assert pane[0] == _heading(leaf)
    assert "LIVE" not in pane[0] and BRAND not in pane[0] and "NEEDS YOU" not in pane[0]
    assert len(pane) == H - 2
    frame = compose_frame(inspected)
    assert sum("● LIVE" in row for row in frame) == 1
    assert [_pane(row) for row in frame[1:-1]] == pane


def test_a_view_holding_no_inspector_has_no_pane_to_draw() -> None:
    view = _list_view("activity")
    with pytest.raises(ValueError, match="no inspector"):
        pane_lines(view)
    with pytest.raises(ValueError, match="no inspector"):
        beside(view)


# ---------- the held secondary read ----------


class _Daemon:
    """A binding call serving every route read over a document the test may move.

    Attributes:
        asked: The route and record of every route read, in order.
        document: The tree the reads are answered from.
        failing: Routes whose read is refused.
    """

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.asked: list[tuple[str, str | None]] = []
        self.document: dict[str, Any] = bodies.DOCUMENT
        self.failing = failing

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        route = _ROUTES.get(method)
        if route is None:
            raise ConnectionError(f"{method} is not served here")
        self.asked.append((route, params.get("key")))
        if route in self.failing:
            raise ConnectionError(f"{route} could not be read")
        answer: dict[str, Any] = bodies._projection(route, self.document).model_dump(mode="json")
        return answer


def _seam(daemon: _Daemon, route: str = "activity") -> ProjectionSeam:
    seam = ProjectionSeam(route=route, scope_id=bodies.SCOPE, clock=lambda: AT)
    seam._binding.call = daemon  # type: ignore[method-assign]
    return seam


def _patch(status: str = "FAILED", *, key: str = RUN, sequence: int = 1) -> KeyedPatch:
    """Return the patch one committed move of Run ``key`` publishes to both its routes."""
    return KeyedPatch(
        schema_version="1.0",
        projection_kind=bodies._projection(DETAIL).header.projection_kind,
        routes=("activity", DETAIL),
        scope_id=bodies.SCOPE,
        canonical_sequence=bodies.CURSOR + sequence,
        entries=(
            PatchEntry(
                key=key,
                urn=f"{bodies.ROOT}/run/{key}",
                collection=Epoch2Collection.RUN,
                revision=9,
                status=status,
            ),
        ),
    )


def _read(seam: ProjectionSeam) -> bool:
    """Run the inspected route's read when one is due; return whether one arrived."""
    read = seam.read_inspected()
    if read is None:
        return False

    async def wait() -> bool:
        return await read

    return asyncio.run(wait())


def _status(seam: ProjectionSeam, key: str = RUN) -> str | None:
    held = seam.inspected()
    assert held is not None
    return next(row.status.value for row in held.rows if row.key == key)


def test_the_inspected_route_is_owed_once_and_held_for_its_record() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    seam.inspect(DETAIL, RUN)
    assert seam.inspecting == DETAIL
    assert INSPECTOR_READ in seam.owed() and seam.inspect_due()
    assert _read(seam)
    assert daemon.asked == [(DETAIL, RUN)]
    assert seam.inspected() is not None and not seam.inspect_due()
    assert not _read(seam)
    assert daemon.asked == [(DETAIL, RUN)]


def test_a_patch_to_the_inspected_record_moves_it_in_place_without_a_read() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    seam.inspect(DETAIL, RUN)
    _read(seam)
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)
    asyncio.run(seam.apply_patch(_patch("FAILED")))
    assert _status(seam) == "FAILED"
    assert heard == [(DETAIL,)]
    assert not seam.inspect_due()
    assert daemon.asked == [(DETAIL, RUN)]


def test_a_patch_the_held_read_already_states_is_not_applied_again() -> None:
    seam = _seam(_Daemon())
    seam.inspect(DETAIL, RUN)
    _read(seam)
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)
    asyncio.run(seam.apply_patch(_patch("FAILED", sequence=0)))
    assert _status(seam) == "COMPLETED"
    assert heard == []


def test_a_read_across_a_feed_move_is_held_and_owed_again() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    seam.inspect(DETAIL, RUN)
    real = daemon.__call__

    async def racing(method: str, params: dict[str, Any]) -> dict[str, Any]:
        answer = await real(method, params)
        await seam.apply_patch(_patch("FAILED"))
        return answer

    seam._binding.call = racing  # type: ignore[method-assign]
    _read(seam)
    assert seam.inspected() is not None
    assert seam.inspect_due()


def test_an_answer_for_a_row_the_cursor_left_is_dropped() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    seam.inspect(DETAIL, RUN)
    real = daemon.__call__

    async def moving(method: str, params: dict[str, Any]) -> dict[str, Any]:
        seam.inspect(DETAIL, OTHER_RUN)
        return await real(method, params)

    seam._binding.call = moving  # type: ignore[method-assign]
    asyncio.run(seam.load_inspected())
    assert seam.inspected() is None and seam.inspect_due()


def test_a_failed_read_is_not_due_again_for_the_same_record() -> None:
    seam = _seam(_Daemon(failing=frozenset({DETAIL})))
    seam.inspect(DETAIL, RUN)
    assert not _read(seam)
    assert seam.inspected() is None and not seam.inspect_due()
    seam.inspect(DETAIL, OTHER_RUN)
    assert seam.inspect_due()


def test_nothing_inspected_reads_nothing_and_refuses_a_direct_read() -> None:
    seam = _seam(_Daemon())
    seam.inspect(None, None)
    assert seam.inspecting is None and seam.inspected() is None
    assert not seam.inspect_due() and INSPECTOR_READ not in seam.owed()
    with pytest.raises(ValueError, match="names the route"):
        asyncio.run(seam.load_inspected())


def test_a_route_read_ahead_for_the_record_is_taken_without_a_read() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    asyncio.run(seam.prefetch(DETAIL, RUN))
    seam.inspect(DETAIL, RUN)
    assert seam.inspected() is not None and not seam.inspect_due()
    assert daemon.asked == [(DETAIL, RUN)]


def test_enter_onto_the_inspected_route_holds_it_without_a_read() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)
    seam.inspect(DETAIL, RUN)
    _read(seam)
    seam.retarget(DETAIL)
    seam.about(RUN)
    assert not seam.route_owed()
    assert daemon.asked == [(DETAIL, RUN)]


def test_a_break_in_the_link_drops_the_inspected_read_for_a_fresh_one() -> None:
    seam = _seam(_Daemon())
    seam.inspect(DETAIL, RUN)
    _read(seam)
    seam._drop_hidden()
    assert seam.inspected() is None and seam.inspect_due()


# ---------- live: the pane holds across a patch (CR-3) ----------


def _app(daemon: _Daemon) -> ConsoleApp:
    return ConsoleApp(clock=FakeClock(), seam=_seam(daemon))


async def _settle(app: ConsoleApp, pilot: Any) -> None:
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


def _pane_of(rows: list[str]) -> list[str]:
    return [_pane(row) for row in rows[1:-1]]


def test_a_patch_to_the_inspected_run_moves_the_pane_and_never_blanks_it() -> None:
    daemon = _Daemon()

    async def drive() -> tuple[list[str], list[list[str]], ConsoleApp]:
        app = _app(daemon)
        async with app.run_test(size=(W, H)) as pilot:
            app.reset(SessionSetup(route="activity"))
            app.render_frame()
            await _settle(app, pilot)
            before = list(app.frame_rows)
            assert app.seam is not None
            frames: list[list[str]] = []
            app.seam.watch(lambda _routes: frames.append(list(app.frame_rows)))
            await app.seam.apply_patch(_patch("FAILED"))
            frames.append(list(app.frame_rows))
            await _settle(app, pilot)
            frames.append(list(app.frame_rows))
            return before, frames, app

    before, frames, app = asyncio.run(drive())
    assert app.session.sel_id == RUN
    shown = _pane_of(before)
    assert RUN in shown[0] and "COMPLETED" in shown[1]
    assert "NOT HELD" not in "\n".join(shown)
    for frame in frames:
        pane = _pane_of(frame)
        assert RUN in pane[0], "the pane lost its record across the patch"
        assert all(line.strip() for line in pane[:3]), "the pane stood empty"
        assert "NOT HELD" not in "\n".join(pane)
    assert _pane_of(frames[-1]) != shown
    assert "FAILED" in "\n".join(_pane_of(frames[-1]))
    # the held read was moved in place: the Run frame was read once, for its record
    assert daemon.asked.count((DETAIL, RUN)) == 1


def test_a_live_console_below_180_columns_reads_and_draws_no_pane() -> None:
    daemon = _Daemon()

    async def drive() -> list[str]:
        app = _app(daemon)
        async with app.run_test(size=(INSPECT_FROM - 1, H)) as pilot:
            app.reset(SessionSetup(route="activity"))
            app.render_frame()
            await _settle(app, pilot)
            return list(app.frame_rows)

    rows = asyncio.run(drive())
    assert not [ask for ask in daemon.asked if ask[0] == DETAIL]
    assert not any(DIVIDER in row[60:] and RUN in row[100:] for row in rows)


def test_enter_at_180_columns_opens_the_run_the_pane_drew_without_a_read() -> None:
    daemon = _Daemon()

    async def drive() -> ConsoleApp:
        app = _app(daemon)
        async with app.run_test(size=(W, H)) as pilot:
            app.reset(SessionSetup(route="activity"))
            app.render_frame()
            await _settle(app, pilot)
            app.press_key("Enter")
            await _settle(app, pilot)
            return app

    app = asyncio.run(drive())
    assert (app.session.route, app.session.subj_id) == (DETAIL, RUN)
    assert daemon.asked.count((DETAIL, RUN)) == 1
    assert app.seam is not None and app.seam.inspecting is None
    assert RUN in app.frame_rows[0]
