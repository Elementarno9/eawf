"""The console lays its frames out on the terminal's width and keeps each column one grid.

The console draws inside a
one-cell gutter at each side, so a frame is two cells narrower than the terminal, yet the
pack's breakpoints are terminal sizes: a 120-column terminal takes the wide layout. The
frame is laid out again at the new size the moment the terminal is resized. Within a
frame, a column of names ends at one cell, labels share one width, a rail's counts line up
and a chooser keeps the caret's slot on every row.
"""

from __future__ import annotations

import asyncio
import copy
import re
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp, compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import RailReceded, View
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session, SessionSetup
from eawf.surfaces.tui.console.width import cell_len
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.tui.surfaces.tui.console import test_native_route_bodies as nrb
from tests.tui.surfaces.tui.console import test_native_route_frames as nrf
from tests.tui.surfaces.tui.console import test_native_windowing as nw
from tests.tui.surfaces.tui.console import test_settings_route as sr
from tests.tui.surfaces.tui.console import test_spine_frames as sp

GOLDEN_FIXTURE = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose layers the test owns, the home redirected beside it."""
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    return repo


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the golden fixture the console is built over."""
    return load_fixture(GOLDEN_FIXTURE)


async def _live(columns: int, rows: int, route: str) -> list[str]:
    """Return ``route``'s frame on a guttered console in a terminal of this size."""
    app = ConsoleApp(load_fixture(GOLDEN_FIXTURE), FakeClock(), gutter=OUTER_GUTTER)
    async with app.run_test(size=(columns, rows)) as pilot:
        await pilot.pause()
        app.reset(SessionSetup(route=route))
        app.render_frame()
        await pilot.pause()
        return list(app.frame_rows)


def _home(columns: int, document: dict[str, Any] | None = None) -> list[str]:
    """Return the native home frame drawn inside the gutter of a ``columns``-wide terminal."""
    session = sp._session("scope.home", None)
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=columns - 2 * OUTER_GUTTER,
        h=40,
        projection=sp._model("scope.home", document),
        gutter=OUTER_GUTTER,
    )
    return render_route(view)


# ---------- breakpoints step on the terminal's width ----------


@pytest.mark.parametrize(
    ("columns", "wide", "xwide"),
    [
        (80, False, False),
        (119, False, False),
        (120, True, False),
        (159, True, False),
        (160, True, True),
    ],
)
def test_the_terminal_width_not_the_frame_decides_the_layout(
    columns: int, wide: bool, xwide: bool
) -> None:
    view = View(
        session=Session(),
        fixture=Fixture.from_chrome(load_chrome()),
        w=columns - 2 * OUTER_GUTTER,
        h=30,
        gutter=OUTER_GUTTER,
    )
    assert (view.wide, view.xwide) == (wide, xwide)


def test_a_frame_with_no_gutter_steps_on_its_own_width() -> None:
    view = View(session=Session(), fixture=Fixture.from_chrome(load_chrome()), w=118, h=30)
    assert not view.wide


def test_a_120_column_terminal_advertises_the_wide_settings_keys() -> None:
    frame = asyncio.run(_live(120, 30, "settings"))
    assert len(frame[0]) == 118
    assert "filter" in frame[-1] and "stack" in frame[-1]


def test_a_160_column_terminal_draws_the_widest_campaign_plan() -> None:
    frame = asyncio.run(_live(160, 40, "campaign"))
    assert len(frame[0]) == 158
    assert any("✓ 1 ─┐" in row for row in frame)


def test_the_home_name_column_is_capped_wide_at_a_120_column_terminal() -> None:
    head = next(row for row in _home(120) if "MILESTONES" in row)
    # the name column is the wide cap of 48 cells after the three-cell lead
    assert head.index("RUNS") == 3 + 48


# ---------- a resize lays the frame out again at the new size ----------


def test_a_resize_relays_the_frame_at_the_new_size() -> None:
    async def body() -> list[tuple[int, str]]:
        app = ConsoleApp(load_fixture(GOLDEN_FIXTURE), FakeClock(), gutter=OUTER_GUTTER)
        seen: list[tuple[int, str]] = []
        async with app.run_test(size=(162, 40)) as pilot:
            await pilot.pause()
            for size in ((82, 24), (162, 40)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                seen.append((len(app.frame_rows[0]), app.frame_rows[0]))
        return seen

    for (width, header), columns in zip(asyncio.run(body()), (82, 162), strict=True):
        assert width == columns - 2 * OUTER_GUTTER
        # the connection slot closes the header row, whatever the new width
        assert header.rstrip().endswith("● LIVE")
        assert cell_len(header) == width


# ---------- one column grid for the whole home tree ----------


def _long_home() -> dict[str, Any]:
    document = copy.deepcopy(sp.DOCUMENT)
    document["milestone"]["MLS-0101"] = sp._row(
        "milestone",
        "MLS-0101",
        "PLANNED",
        title="Bring every console route onto the one grid the packet draws at all sizes",
        primary_track_ref=sp._under("track", "TRK-CORE"),
    )
    return document


@pytest.mark.parametrize("columns", [82, 122, 162])
def test_a_milestone_state_sits_under_runs(columns: int) -> None:
    frame = _home(columns, _long_home())
    head = next(row for row in frame if "MILESTONES" in row)
    leaves = [row for row in frame if re.search(r"MLS-010[01] ", row)]
    assert len(leaves) == 2
    for row in leaves:
        assert row[head.index("RUNS") :].split()[0] in ("ACTIVE", "PLANNED"), row


@pytest.mark.parametrize("columns", [82, 162])
def test_clipped_names_end_at_one_cell(columns: int) -> None:
    frame = _home(columns, _long_home())
    ends = {row.index("…") for row in frame if re.search(r"MLS-010[01] ", row)}
    assert len(ends) == 1


# ---------- a Batch's Task rows keep their state ----------


def test_a_task_row_keeps_its_state_and_only_the_caret_row_its_title() -> None:
    document = copy.deepcopy(sp.DOCUMENT)
    document["task"]["TSK-0001"]["intent"] = sp.LONG * 2
    frame = sp._frame("batch.detail", "BAT-0100", w=80, document=document)
    caret = next(row for row in frame if "▸ TSK-0001" in row)
    assert "TSK-0001 · RUNNING" in caret
    assert any(row.rstrip().endswith("TSK-0002 · COMPLETED") for row in frame)


# ---------- the Activity rail and its window ----------


def _activity(n: int, bucket: str | None = None) -> list[str]:
    session = Session()
    session.bucket = bucket
    return render_route(nw.register_view(session, n))


def test_the_rail_counts_end_in_one_column() -> None:
    rail = [row for row in _activity(30) if re.search(r"│ [ ▸] *[↳a-z]", row)]
    assert rail
    assert len({cell_len(row.rstrip()) for row in rail}) == 1


def test_a_chosen_bucket_recedes_every_other_rail_row() -> None:
    frame = _activity(30, bucket="running")
    chosen = next(row for row in frame if "│ ▸running" in row)
    failed = next(row for row in frame if re.search(r"│  failed ", row))
    assert not isinstance(chosen, RailReceded)
    assert isinstance(failed, RailReceded)


def test_no_bucket_chosen_recedes_nothing() -> None:
    assert not any(isinstance(row, RailReceded) for row in _activity(30))


def test_a_table_showing_every_run_states_no_window() -> None:
    assert not any(row.startswith(" WINDOW") for row in _activity(3))


def test_a_cut_table_still_states_its_window() -> None:
    assert any(row.startswith(" WINDOW") for row in _activity(60))


# ---------- the help card's key column ----------


def _help(n: int) -> list[str]:
    view = nw.spine_view(Session(), n)
    compose_frame(view)
    view.session.overlay = "help"
    return compose_frame(view)


def _meaning_columns(rows: list[str]) -> set[int]:
    keys = [row for row in rows if row.startswith("   ") and not row.startswith("    ")]
    return {len(row) - len(row[3:].split("  ", 1)[1].lstrip()) for row in keys if "  " in row[3:]}


def test_every_meaning_starts_in_one_column() -> None:
    rows = _help(60)
    assert any("PageUp PageDown" in row for row in rows)
    assert len(_meaning_columns(rows)) == 1


def test_a_frame_showing_every_row_teaches_no_paging_key() -> None:
    rows = _help(3)
    assert not any("PageUp PageDown" in row or "Home End" in row for row in rows)


# ---------- the settings chooser, lens and stack ----------

#: A chooser option row: after the rail, the caret's slot, then the value's dot.
_OPTION = re.compile(r"│ +(?:▸ )?(?P<dot>[●○]) [a-z]")


def test_every_chooser_row_keeps_the_caret_slot(tree: Path, fixture: Fixture) -> None:
    view = sr._view(tree)
    session = sr._on(sr._session(), view, sr.LITERAL_KEY)
    sr._press(fixture, view, session, ["Enter"])
    rows = sr._frame(fixture, view, session, w=120, h=30)
    options = [found for row in rows if (found := _OPTION.search(row))]
    assert len(options) == 3
    assert len({found.start("dot") for found in options}) == 1


def test_the_lens_is_bold_without_brackets(tree: Path, fixture: Fixture) -> None:
    sr._write(tree / ".ea" / "config.yaml", "telemetry:\n  enabled: false\n")
    view = sr._view(tree)
    rows = sr._frame(fixture, view, sr._on(sr._session(), view, sr.BOOL_KEY), w=120, h=30)
    chain = next(row for row in rows if "global › repo" in row)  # noqa: RUF001
    assert "[repo]" not in chain
    strokes = paint(chain, Part.BODY)
    repo = next(stroke for stroke in strokes if stroke.text == "repo")
    assert repo.bold
    # the repo layer sets the key and wins, so it wears the ok surface
    assert repo.surface == "ok"
    rest = next(stroke for stroke in strokes if "global" in stroke.text)
    assert rest.surface is None and not rest.bold


def test_the_stack_card_labels_are_whole_and_the_caret_has_room(
    tree: Path, fixture: Fixture
) -> None:
    view = sr._view(tree)
    session = sr._on(sr._session("settings.stack"), view, sr.BOOL_KEY)
    body = "\n".join(sr._frame(fixture, view, session))
    assert "LENS SETS " in body
    assert "ON THIS" not in body
    assert "▸ built-in" in body and "▸built-in" not in body


# ---------- one label width per frame ----------


def _value_column(row: str, label: str) -> int:
    rest = row[row.index(label) + len(label) :]
    return row.index(label) + len(label) + len(rest) - len(rest.lstrip())


def test_the_release_labels_share_one_width() -> None:
    model = build_acceptance_view(sp._projection("release"))
    frame = compose_frame(sp._view(sp._session("release", None), model, 80))
    approval = next(row for row in frame if row.lstrip().startswith("APPROVAL"))
    publication = next(row for row in frame if row.lstrip().startswith("PUBLICATION"))
    assert _value_column(approval, "APPROVAL") == _value_column(publication, "PUBLICATION")


def test_the_empty_attention_labels_share_the_strip_width() -> None:
    frame = nrb._frame("attention", w=80, document=nrb._no_open_actions())
    strip = next(row for row in frame if row.startswith(" BUCKETS"))
    nothing = next(row for row in frame if row.startswith(" NOTHING YET"))
    todo = next(row for row in frame if row.startswith(" WHAT TO DO"))
    assert (
        _value_column(strip, "BUCKETS")
        == _value_column(nothing, "NOTHING YET")
        == _value_column(todo, "WHAT TO DO")
    )


def test_the_sandbox_log_head_sits_over_its_rows() -> None:
    frame = nrf._frame("sandbox.log", w=80)
    head = next(row for row in frame if "DECISION" in row and "REASON" in row)
    empty = next(row for row in frame if "no decision is recorded for this scope" in row)
    assert head.index("TIME") == len(empty) - len(empty.lstrip())


# ---------- the backlog states an empty group and cuts titles at one cell ----------


def _backlog() -> list[str]:
    document = copy.deepcopy(sp.DOCUMENT)
    # one title breaks at a word early in the column, the other at none, so a word cut
    # would end them at different cells
    titles = {
        "TSK-0101": "Remove the dead code paths from the ledger",
        "TSK-0102": "Make check_affordability honest everywhere",
    }
    document["task"] = {key: sp._row("task", key, "DRAFT", intent=t) for key, t in titles.items()}
    document["task"]["TSK-0103"] = sp._row("task", "TSK-0103", "DRAFT", intent="Short")
    return sp._frame("backlog", w=80, document=document)


def test_an_empty_group_states_its_absence() -> None:
    frame = _backlog()
    deferred = next(i for i, row in enumerate(frame) if row.startswith(" DEFERRED"))
    assert "∅ nothing deferred" in frame[deferred + 1]


def test_clipped_titles_end_at_one_cell() -> None:
    ends = {row.index("…") for row in _backlog() if re.search(r"TSK-010[12] ", row)}
    assert len(ends) == 1
