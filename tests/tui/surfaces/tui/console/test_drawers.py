"""The inspect and raw drawers read the held record under the cursor: CON-100.

Diagnostics are two drawers one keypress away. ``i`` opens the provenance of the focused
field -- who stated it, at which revision, through which cursor, how fresh, and the raw
state code -- and ``r`` opens the stored record's own fields, bounded and labelled as the
raw form. Both read the record the frame's cursor names by its stable id, so they open
over a read model the daemon served, where they used to fall back to the unknown frame.
The frame itself stays in friendly words, and the last keystroke belongs to the
``--verbose`` row, never to a drawer.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.clock import QUIT_PROMPT, FakeClock, notify
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.drawers import focused, inspect_rows, raw_rows
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.tokens import Severity
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_console_verbs import _Host

RUN = "RUN-00000002"


def _view(overlay: str | None = None, **kwargs: Any) -> View:
    """Return the Run frame on RUN-00000002 with the cursor published by a first render."""
    view = bodies._view("run.detail", subject=RUN, **kwargs)
    render_route(view)
    view.session.overlay = overlay
    return view


def test_con_100_the_focused_record_is_read_by_its_stable_id() -> None:
    view = _view()
    record = focused(view)
    assert record is not None
    assert record.key == view.session.subj_id == RUN


def test_con_100_a_list_frame_inspects_the_row_its_cursor_names() -> None:
    view = bodies._view("activity")
    view.session.sel_id = "RUN-00000003"
    render_route(view)
    record = focused(view)
    assert record is not None
    assert record.key == "RUN-00000003"
    assert "failure=final report rejected" in "\n".join(raw_rows(view))


def test_con_100_inspect_states_producer_revision_cursor_freshness_and_raw_code() -> None:
    view = _view()
    record = focused(view)
    assert record is not None
    rows = inspect_rows(view)
    text = "\n".join(rows)
    assert rows[0].startswith(" INSPECT")
    assert f"answered by {record.status.producer}" in text
    assert f"revision {record.status.producer_revision}" in text
    assert "cursor 41,208" in text
    assert "freshness   live" in text
    assert "raw state   known" in text


def test_con_100_raw_quotes_the_stored_fields_bounded_and_labelled_raw() -> None:
    rows = raw_rows(_view())
    assert rows[0].startswith(f" RAW       {RUN} · the stored record's own fields")
    assert "task_status=RUNNING" in "\n".join(rows)
    assert rows[-1].strip().startswith("bounded")
    assert len(rows) <= 11


@pytest.mark.parametrize(("overlay", "head"), [("inspect", " INSPECT"), ("raw", " RAW")])
def test_con_100_a_drawer_opens_over_the_held_read_model(overlay: str, head: str) -> None:
    frame = compose_frame(_view(overlay))
    assert any(row.startswith(head) for row in frame)
    assert not any("NOT HELD" in row for row in frame)


@pytest.mark.parametrize(("key", "overlay"), [("i", "inspect"), ("r", "raw")])
def test_con_100_each_drawer_is_one_keypress_away(key: str, overlay: str) -> None:
    view = _view()
    ctx = Ctx(session=view.session, fixture=view.fixture, host=_Host(), w=view.w, h=view.h)
    dispatch(ctx, key, False)
    assert view.session.overlay == overlay


def test_con_100_the_frame_body_stays_in_friendly_words() -> None:
    body = "\n".join(render_route(_view()))
    assert "raw state" not in body
    assert "suspension_reason=" not in body


def test_con_100_no_drawer_carries_a_keystroke_fact() -> None:
    view = _view()
    view.session.log_key("i", "inspect the focused field")
    for rows in (inspect_rows(view), raw_rows(view)):
        text = "\n".join(rows)
        assert "inspect the focused field" not in text


def test_con_100_with_no_record_under_the_cursor_nothing_is_inspected() -> None:
    view = bodies._view("run.detail", subject=RUN, document={"run": {}})
    render_route(view)
    assert focused(view) is None


# ---------- K-02: the rack is the console's, so no drawer hides it ----------


def _below(frame: list[str]) -> list[str]:
    """Return the count of route rows the drawer's cut hid, as its edge line states it."""
    return [row.split(" · ")[0] for row in frame if " more row" in row and " below" in row]


@pytest.mark.parametrize("surface", ["inspect", "raw", "actions", "prefix"])
def test_k_02_a_toast_stands_over_an_open_drawer_and_hides_no_row(surface: str) -> None:
    view = _view(None if surface == "prefix" else surface)
    if surface == "prefix":
        view.session.prefix = "g"
    quiet = compose_frame(view)
    notify(view.session, FakeClock(), text=QUIT_PROMPT, title="", sev=Severity.INFO)
    frame = compose_frame(view)
    assert sum(QUIT_PROMPT in row for row in frame) == 1
    # the rack is never counted as route rows the drawer's cut hid
    assert _below(frame) == _below(quiet)


# ---------- K-13: the selection line counts its results in words ----------


@pytest.mark.parametrize(("marked", "said"), [(1, "1 result"), (2, "2 results")])
def test_k_13_the_selection_line_pluralises_its_results(marked: int, said: str) -> None:
    view = bodies._view("activity")
    render_route(view)
    view.session.marked = ["RUN-00000001", "RUN-00000002"][:marked]
    view.session.overlay = "actions"
    line = next(row for row in compose_frame(view) if row.startswith(" SELECTED"))
    assert line.rstrip().endswith(f"one preview, {said}")
