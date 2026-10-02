"""History pages through its feed, opens the changed record, and the diff steps back in time.

The ledger holds one page of the feed; ``n`` reads the next older page from the cursor the
page names, and on the last page returns to the newest. The menu's ``open target`` opens
the record the change under the caret is about. The diff of one record steps to that
record's older changes on ``p`` and opens the record itself on ``e``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from eawf.kernel.store.changes import ChangePage
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.live_reads import HISTORY_DIFF_READ, HISTORY_READ, LIVE_READS
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session
from tests.tui.surfaces.tui.console.overlay_support import Host
from tests.tui.surfaces.tui.console.test_history_change_feed import _page, _record
from tests.tui.surfaces.tui.console.test_spine_frames import _model, _session

LINKED = Fixture.from_chrome(load_chrome())


def _paged(next_cursor: int | None) -> ChangePage:
    feed = _page(
        _record("TSK-0003", 12, status=("DRAFT", "PLANNED")),
        _record("TSK-0001", 11, status=("PLANNED", "CLAIMED")),
    )
    return feed.model_copy(update={"next_cursor": next_cursor})


def _press(session: Session, live: Mapping[str, object], *keys: str) -> list[str]:
    """Draw the frame before each key, as the console does, and return the last one drawn."""
    frame: list[str] = []
    model = _model(session.route)
    for key in keys:
        frame = compose_frame(
            View(
                session=session,
                fixture=LINKED,
                w=120,
                h=dict(SIZES)[120],
                projection=model,
                live=live,
            )
        )
        ctx = Ctx(
            session=session,
            fixture=LINKED,
            host=Host(),
            w=120,
            h=dict(SIZES)[120],
            projection=model,
            live=live,
        )
        dispatch(ctx, key, False)
    return frame


def _draw(session: Session, live: Mapping[str, object]) -> list[str]:
    view = View(
        session=session,
        fixture=LINKED,
        w=120,
        h=dict(SIZES)[120],
        projection=_model(session.route),
        live=live,
    )
    return render_route(view)


# ---------- History pages through the feed ----------


def test_a_page_with_an_older_one_offers_n_and_n_reads_from_its_cursor() -> None:
    session = _session("history", None)
    live = {HISTORY_READ: _paged(11)}
    assert "older" in _draw(session, live)[-1]
    _press(session, live, "n")
    assert session.history_cursor == 11


def test_n_on_the_last_page_returns_to_the_newest() -> None:
    session = _session("history", None)
    session.history_cursor = 11
    live = {HISTORY_READ: _paged(None)}
    assert "newest" in _draw(session, live)[-1]
    _press(session, live, "n")
    assert session.history_cursor is None


def test_the_newest_page_with_no_older_one_offers_no_paging() -> None:
    frame = _draw(_session("history", None), {HISTORY_READ: _paged(None)})
    assert "older" not in frame[-1] and "newest" not in frame[-1]


class _Host:
    """A live-read host on History, paged to ``history_cursor``, recording what it asks."""

    route = "history"
    subject = None
    operator = None

    def __init__(self, history_cursor: int | None) -> None:
        self.history_cursor = history_cursor
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def projection_for(self, route: str) -> object:
        return object()

    def live(self, name: str, *, anywhere: bool = False) -> None:
        return None

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.asked.append((method, dict(params)))
        return {"changes": []}

    def now(self) -> None:
        return None


def test_the_history_read_asks_from_the_paged_cursor() -> None:
    read = LIVE_READS[HISTORY_READ]
    newest, paged = _Host(None), _Host(11)
    assert read.address(newest) != read.address(paged)  # type: ignore[arg-type]
    for host in (newest, paged):
        address = read.address(host)  # type: ignore[arg-type]
        assert address is not None
        asyncio.run(read.fetch(host, address))  # type: ignore[arg-type]
    assert newest.asked[0][1] == {}
    assert paged.asked[0][1] == {"cursor": 11}


# ---------- open target ----------


def test_open_target_is_a_live_history_verb() -> None:
    verbs = {verb.verb: verb for verb in LINKED.menus.verbs("history")}
    assert "open target" in verbs and verbs["open target"].available


def test_open_target_opens_the_record_the_change_is_about() -> None:
    session = _session("history", None)
    key = next(v.key for v in LINKED.menus.verbs("history") if v.verb == "open target")
    _press(session, {HISTORY_READ: _paged(None)}, "ArrowDown", ".", key)
    assert (session.route, session.subj_id) == ("task.detail", "TSK-0001")


# ---------- the diff steps through one record's changes ----------


def _diff_live() -> dict[str, object]:
    newest = _record("TSK-0003", 12, status=("PLANNED", "CLAIMED"))
    older = _record("TSK-0003", 7, status=("DRAFT", "PLANNED"))
    older["revision_before"], older["revision_after"] = 0, 1
    return {HISTORY_DIFF_READ: _page(newest, older)}


def test_p_steps_the_diff_to_the_record_s_older_change() -> None:
    session = _session("history.diff", "TSK-0003")
    live = _diff_live()
    assert any("rev 1 → rev 2" in row for row in _draw(session, live))
    _press(session, live, "p")
    frame = _draw(session, live)
    assert any("rev 0 → rev 1" in row for row in frame)
    assert any("2 of 2" in row for row in frame)


def test_e_opens_the_record_the_diff_is_about() -> None:
    session = _session("history.diff", "TSK-0003")
    _press(session, _diff_live(), "e")
    assert (session.route, session.subj_id) == ("task.detail", "TSK-0003")
