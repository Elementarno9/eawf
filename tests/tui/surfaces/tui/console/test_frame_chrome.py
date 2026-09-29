"""The console chrome: header name, gutter, bands, summary, keys, styling.

The chrome is what every route shares -- the header's scope name and connection value,
the summary line under it, the keybar's paging pairs, the tinted bands, the one-cell outer
gutter, the cursor ground, the id links, the receded pane and what Shift-Tab does -- so
each is proved on the native
frames an operator's console draws and, where the golden contract's prototype mode shares
the code path, on that mode too.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.surfaces.tui.chassis.theme import EA_CB, EA_DARK, EA_LIGHT, mix
from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import Receded, View, recede, scope_label
from eawf.surfaces.tui.console.navigation import Ctx, cycle
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.read_model import counts
from eawf.surfaces.tui.console.renderers.scope_home import ATTENTION_REGION
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.token_map import SURFACES
from eawf.surfaces.tui.launch import project_name

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
ROOT_ID = "root-62f9ec2c5304de47"
CURSOR = 41208
URN = "eawf://EAWF/EAWF/EAWF"
GOLDEN_FIXTURE = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"


def _row(kind: str, key: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{URN}/{kind}/{key}", "revision": 1, "status": status, **extra}


#: Twelve Tasks, so the Task frame's table is cut to its window at 80x24.
TASKS: dict[str, Any] = {
    "task": {
        f"TSK-{n:04d}": _row("task", f"TSK-{n:04d}", "DROPPED", title=f"Task number {n}")
        for n in range(1, 13)
    }
}
#: One Track and one Milestone: a home tree that shows every row it holds.
HOME: dict[str, Any] = {
    "track": {"TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", title="Core")},
    "milestone": {
        "MLS-0100": _row(
            "milestone",
            "MLS-0100",
            "ACTIVE",
            title="Close out",
            primary_track_ref=f"{URN}/track/TRK-CORE",
        )
    },
}
RUNS: dict[str, Any] = {"run": {"RUN-00000001": _row("run", "RUN-00000001", "RUNNING")}}


def _projection(route: str, document: dict[str, Any]) -> Any:
    return build_route_projection(
        route=route, document=document, cursor=CURSOR, scope_id=ROOT_ID, generated_at=AT
    )


def _native(
    route: str,
    document: dict[str, Any],
    *,
    w: int = 80,
    subject: str | None = None,
    scope_name: str = "eawf",
    conn: str = "LIVE",
) -> list[str]:
    """Return ``route``'s native frame on a linked console that names its scope."""
    h = dict(SIZES)[w]
    session = Session()
    session.route, session.subj_id, session.conn = route, subject, conn
    projection = _projection(route, document)
    held: dict[str, Any] = (
        {"register": build_register_view(projection)}
        if route in ("activity", "attention")
        else {"projection": build_spine_view(projection)}
    )
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=h,
        linked=True,
        scope_name=scope_name,
        **held,
    )
    # the app resets what a render publishes before each frame
    session.windowed = False
    return render_route(view)


# ---------- the header names the project ----------


@pytest.mark.parametrize(
    ("route", "document", "subject"),
    [("scope.home", HOME, None), ("activity", RUNS, None), ("task.detail", TASKS, "TSK-0001")],
)
def test_the_header_names_the_project_not_the_root_id(
    route: str, document: dict[str, Any], subject: str | None
) -> None:
    header = _native(route, document, subject=subject)[0]
    assert header.startswith(" Eä ▸ eawf")
    assert ROOT_ID not in header


def test_a_console_given_no_name_names_the_scope_by_its_id() -> None:
    view = View(session=Session(), fixture=Fixture.from_chrome(load_chrome()), w=80, h=24)
    assert scope_label(view, ROOT_ID) == ROOT_ID
    assert _native("scope.home", HOME, scope_name="")[0].startswith(f" Eä ▸ {ROOT_ID}")


def test_the_project_name_is_the_state_records_slug(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    project = {
        "code": "EAWF",
        "slug": "eawf",
        "title": "Eä Workflow",
        "domains": [],
        "default_branch": "main",
        "status": "active",
        "repo_urn": "urn:eawf:v1:repo:EAWF",
    }
    state.write_text(json.dumps({"project": project}), encoding="utf-8")
    assert project_name(state, tmp_path / "checkout") == "eawf"


@pytest.mark.parametrize(
    "content",
    [None, "", "{not json", "[]", "{}", '{"project": {"slug": "eawf"}}', '{"project": null}'],
    ids=["missing", "empty", "malformed", "not-a-mapping", "no-project", "invalid", "null"],
)
def test_an_unreadable_project_falls_back_to_the_repository_name(
    tmp_path: Path, content: str | None
) -> None:
    state = tmp_path / "state.json"
    if content is not None:
        state.write_text(content, encoding="utf-8")
    assert project_name(state, tmp_path / "my-repo") == "my-repo"


def test_the_seam_hands_its_name_to_every_render() -> None:
    from eawf.surfaces.tui.console.seam import ProjectionSeam

    seam = ProjectionSeam(route="scope.home", scope_id=ROOT_ID, state_path=None, scope_name="eawf")
    app = ConsoleApp(chrome=load_chrome(), clock=FakeClock(), seam=seam)
    assert app.view().scope_name == "eawf"
    assert ConsoleApp(chrome=load_chrome(), clock=FakeClock()).view().scope_name == ""


# ---------- the summary line ----------


@pytest.mark.parametrize(
    ("route", "document"), [("scope.home", HOME), ("activity", RUNS), ("search", HOME)]
)
def test_a_complete_read_names_no_cursor(route: str, document: dict[str, Any]) -> None:
    assert "cursor" not in _native(route, document)[1]


def test_only_an_incomplete_read_says_how_far_it_got() -> None:
    whole = build_spine_view(_projection("scope.home", HOME))
    part = dataclasses.replace(whole, complete=False)
    assert counts(whole) == "1 track · 1 milestone · 0 batches"
    assert counts(part) == "1 track · 1 milestone · 0 batches · known · cursor 41,208"


def test_a_detail_frame_names_its_subject() -> None:
    summary = _native("task.detail", TASKS, subject="TSK-0003")[1]
    assert summary.startswith(" Task TSK-0003 Task number 3 · DROPPED")
    assert "tasks" not in summary


def test_the_run_frame_names_the_run_and_its_sequence() -> None:
    summary = _native("run.detail", RUNS, subject="RUN-00000001")[1]
    assert summary.rstrip() == " Run RUN-00000001 · RUNNING · seq 41,208"


# ---------- paging only when the table is windowed ----------


def test_a_table_showing_every_row_offers_no_paging() -> None:
    bar = _native("scope.home", HOME)[-1]
    assert "PageUp" not in bar and "Home End" not in bar
    assert ". actions" in bar and "? help" in bar


def test_a_windowed_table_pages_where_the_bar_has_room() -> None:
    runs = {"run": {f"RUN-{n:08d}": _row("run", f"RUN-{n:08d}", "RUNNING") for n in range(40)}}
    assert "PageUp PageDown page" in _native("activity", runs, w=160)[-1]


def test_a_narrow_windowed_frame_keeps_the_way_back_over_paging() -> None:
    bar = _native("task.detail", TASKS, subject="TSK-0001")[-1]
    assert "Esc back" in bar and "i inspect" in bar
    assert "Home End" not in bar


# ---------- tinted bands ----------


def test_the_bands_are_the_accent_over_the_surface() -> None:
    assert EA_DARK.variables["band"] == "#0d1818"
    for theme in (EA_DARK, EA_CB, EA_LIGHT):
        variables = theme.variables
        assert variables["band"] == mix(variables["accent"], variables["surface"], 0.06)
    assert SURFACES["band"].token == "band"


@pytest.mark.parametrize(
    ("share", "expected"), [(0.0, "#000000"), (1.0, "#ffffff"), (0.5, "#808080")]
)
def test_a_mix_takes_its_share_of_the_first_colour(share: float, expected: str) -> None:
    assert mix("#ffffff", "#000000", share) == expected


@pytest.mark.parametrize("share", [-0.01, 1.01])
def test_a_mix_refuses_a_share_outside_the_unit(share: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        mix("#ffffff", "#000000", share)


# ---------- the outer gutter ----------


def test_the_frame_is_laid_out_inside_a_one_cell_gutter() -> None:
    async def body() -> tuple[tuple[int, int], list[int], list[str], int]:
        app = ConsoleApp(load_fixture(GOLDEN_FIXTURE), FakeClock(), gutter=OUTER_GUTTER)
        async with app.run_test(size=(82, 24)) as pilot:
            await pilot.pause()
            header = app.query_one("#header")
            strips = [s.text for s in app.screen._compositor.render_strips()]
            return (
                app.frame_size,
                [header.region.x, header.region.width],
                strips,
                len(app.frame_rows[0]),
            )

    size, (x, width), strips, row_w = asyncio.run(body())
    assert size == (80, 24)
    assert (x, width, row_w) == (1, 80, 80)
    assert all(len(row) == 82 and row[0] == " " and row[-1] == " " for row in strips)


def test_a_console_with_no_gutter_fills_the_terminal() -> None:
    app = ConsoleApp(load_fixture(GOLDEN_FIXTURE), FakeClock())
    assert app.gutter == 0


def test_a_negative_gutter_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        ConsoleApp(load_fixture(GOLDEN_FIXTURE), FakeClock(), gutter=-1)


# ---------- the cursor grounds its own pane ----------


def test_a_rail_caret_does_not_ground_the_body_beside_it() -> None:
    rail_row = paint("▸ flow        │   · advance_after.polish        true", Part.BODY)
    assert {s.ground for s in rail_row} == {None}
    body_row = paint("  dispatch    │ ▸ · advance_after.audit         true", Part.BODY)
    grounded = "".join(s.text for s in body_row if s.ground == "cursor")
    assert grounded.startswith(" ▸ · advance_after.audit")
    assert "dispatch" not in grounded


def test_a_row_with_no_rail_is_grounded_whole() -> None:
    row = " ▸ RUN-00000003 EAWF-0147 Complete…    RUNNING"
    assert {s.ground for s in paint(row, Part.BODY)} == {"cursor"}
    assert {s.ground for s in paint("   RUN-00000004", Part.BODY)} == {None}


# ---------- typed ids are links ----------


@pytest.mark.parametrize(
    "token", ["RUN-538453eb", "RUN-00000003", "EAWF-0147", "MLS-0100", "TRK-EAWF-CORE", "EVT-2218"]
)
def test_a_typed_id_in_the_body_is_underlined(token: str) -> None:
    strokes = paint(f"   {token}  something", Part.BODY)
    assert [s.text for s in strokes if s.underline] == [token]


@pytest.mark.parametrize("text", ["WAIT-PERM", "S-Tab", "RUNNING", "B001", "re-run-3x"])
def test_a_word_that_is_no_id_is_not_underlined(text: str) -> None:
    assert not any(s.underline for s in paint(f"   {text}  x", Part.BODY))


# ---------- the receded pane and the dim tone ----------


def test_the_dim_and_recede_tones_are_mixed_from_the_palette() -> None:
    dark = EA_DARK.variables
    assert dark["dim"] == "#89919a"
    assert dark["recede"] == mix(dark["foreground"], dark["surface"], 0.74)
    assert SURFACES["dim"].token == "dim"
    assert SURFACES["recede"].token == "recede"


def test_a_receded_row_recedes_but_keeps_what_needs_the_operator() -> None:
    strokes = paint(recede(" NEEDS OPERATOR  !3 waiting RUNNING", 60), Part.BODY)
    by_text = {s.text: s for s in strokes}
    assert by_text["!3"].surface == "warn" and by_text["!3"].bold
    assert by_text["NEEDS OPERATOR"].bold
    assert {s.surface for s in strokes if s.text not in ("!3",)} == {"recede"}
    assert not any(s.ground for s in strokes)


def test_the_unfocused_home_pane_recedes() -> None:
    session = Session()
    fixture = load_fixture(GOLDEN_FIXTURE)
    view = View(session=session, fixture=fixture, w=120, h=30)
    frame = render_route(view)
    rule = next(i for i, row in enumerate(frame) if row.startswith(" ATTENTION"))
    assert any(isinstance(row, Receded) for row in frame[rule:-1])
    assert not any(isinstance(row, Receded) for row in frame[3:rule])
    session.home_region = ATTENTION_REGION
    frame = render_route(View(session=session, fixture=fixture, w=120, h=30))
    rule = next(i for i, row in enumerate(frame) if row.startswith(" ATTENTION"))
    assert all(isinstance(row, Receded) for row in frame[3 : rule - 1])


# ---------- a terminal frame keeps the link ----------


def test_a_terminal_subject_keeps_live_and_says_final() -> None:
    frame = _native("task.detail", TASKS, subject="TSK-0001")
    assert frame[0].rstrip().endswith("● LIVE")
    assert "DROPPED · final" in frame[1]


# ---------- Shift-Tab reverses Tab ----------


class _Host:
    """The dispatcher's host: a held clock, and a quit no key here may reach."""

    def __init__(self) -> None:
        self.clock = FakeClock()

    def quit(self) -> None:
        raise AssertionError("no key in these tests quits")


def _press(session: Session, key: str, *, shift: bool) -> None:
    ctx = Ctx(session=session, fixture=load_fixture(GOLDEN_FIXTURE), host=_Host(), w=120, h=30)
    dispatch(ctx, key, shift)


def test_shift_tab_walks_back_through_what_tab_walks() -> None:
    session = Session()
    session.route = "activity"
    _press(session, "Tab", shift=False)
    first = session.bucket
    assert first is not None
    _press(session, "Tab", shift=True)
    assert session.bucket is None
    _press(session, "Tab", shift=True)
    assert session.bucket not in (None, first)


def test_shift_tab_where_nothing_cycles_says_so_honestly() -> None:
    session = Session()
    session.route = "health"
    _press(session, "Tab", shift=True)
    text = " ".join(entry.note for entry in session.log)
    assert "canvas" not in text
    assert "nothing here cycles" in text


@pytest.mark.parametrize(
    ("current", "back", "expected"),
    [("a", False, "b"), ("c", False, "a"), ("a", True, "c"), ("z", False, "a"), ("z", True, "c")],
)
def test_a_cycle_wraps_both_ways(current: str, back: bool, expected: str) -> None:
    assert cycle(("a", "b", "c"), current, back=back) == expected


def test_a_cycle_of_one_stays_and_of_none_is_refused() -> None:
    assert cycle(("a",), "a", back=True) == "a"
    with pytest.raises(ValueError, match="nothing to cycle"):
        cycle((), "a", back=False)
