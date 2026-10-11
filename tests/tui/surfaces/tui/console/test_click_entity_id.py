"""A single left click on an underlined typed entity id opens that entity's route.

The clicks go through a Textual pilot over the tracked golden registers, so the hit test
reads the same body rows the painter underlines.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.paint import id_at
from eawf.surfaces.tui.console.session import SessionSetup


def _fixture() -> Fixture:
    """Return the tracked prototype registers the console renders from."""
    root = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
    return load_fixture(root)


@dataclass(frozen=True)
class _Outcome:
    """Where one click left the console."""

    route: str
    subject: str | None
    sel: int
    sel_before: int
    notes: tuple[str, ...]
    rows: tuple[str, ...]


def _click(
    route: str, row: int, token: str, *, at: int = 0, keys: tuple[str, ...] = ()
) -> _Outcome:
    """Click frame row ``row`` of ``route`` at ``token``'s character ``at``, then press ``keys``.

    ``at`` counts from the token's first character, so ``-1`` is the cell before it and
    ``len(token)`` the cell after it.
    """

    async def body() -> _Outcome:
        app = ConsoleApp(_fixture(), FakeClock())
        async with app.run_test(size=(120, 30)) as pilot:
            app.reset(SessionSetup(route=route))
            app.render_frame()
            await pilot.pause()
            sel_before = app.session.sel
            line = app.frame_rows[row]
            # the header is the frame's first row, so frame row ``row`` is body line ``row - 1``
            await pilot.click("#body", offset=(line.index(token) + at, row - 1))
            await pilot.pause()
            for key in keys:
                app.press_key(key)
                await pilot.pause()
            s = app.session
            return _Outcome(
                route=s.route,
                subject=s.subj_id,
                sel=s.sel,
                sel_before=sel_before,
                notes=tuple(entry.note for entry in s.log),
                rows=tuple(app.frame_rows),
            )

    return asyncio.run(body())


# milestone frame row 7: "  BATCHES    ▸ BAT-0007 Escape audit · ACTIVE"
# batch.detail frame row 8: "   EAWF-0003 Replay guard            PLANNED"
# attention frame row 7: "   ACT-0031 RUN-538453eb wants to write outside its batch"


def test_clicking_a_batch_id_in_a_milestone_opens_the_batch() -> None:
    out = _click("milestone", 7, "BAT-0007")
    assert (out.route, out.subject) == ("batch.detail", "BAT-0007")
    assert out.rows[1].startswith(" Batch BAT-0007")


def test_clicking_a_task_id_in_a_batch_opens_the_task() -> None:
    out = _click("batch.detail", 8, "EAWF-0003")
    assert (out.route, out.subject) == ("task.detail", "EAWF-0003")


@pytest.mark.parametrize("at", [0, len("EAWF-0003") - 1], ids=["first", "last"])
def test_the_first_and_last_character_of_an_id_both_open_it(at: int) -> None:
    out = _click("batch.detail", 8, "EAWF-0003", at=at)
    assert (out.route, out.subject) == ("task.detail", "EAWF-0003")


@pytest.mark.parametrize("at", [-1, len("EAWF-0003")], ids=["before", "after"])
def test_the_cell_just_outside_an_id_selects_the_row(at: int) -> None:
    out = _click("batch.detail", 8, "EAWF-0003", at=at)
    assert out.route == "batch.detail"
    assert out.rows[8].lstrip().startswith("▸ EAWF-0003")


def test_a_click_off_the_id_selects_the_row_as_today() -> None:
    out = _click("batch.detail", 8, "Replay guard")
    assert (out.route, out.subject) == ("batch.detail", None)
    assert out.sel != out.sel_before
    assert out.rows[8].lstrip().startswith("▸ EAWF-0003")


def test_an_id_no_route_opens_falls_back_to_selection_and_says_why() -> None:
    out = _click("attention", 7, "ACT-0031")
    # the row is selected exactly as a click on its plain words selects it
    plain = _click("attention", 7, "wants to write")
    assert (out.route, out.sel, out.rows) == ("attention", plain.sel, plain.rows)
    assert out.sel != out.sel_before
    assert "no route opens ACT-0031" in out.notes


def test_a_click_on_the_pages_own_subject_opens_nothing() -> None:
    # the subjectless batch page names BAT-0001 as its breadcrumb leaf
    out = _click("batch.detail", 1, "BAT-0001")
    assert (out.route, out.subject) == ("batch.detail", None)
    assert out.sel == out.sel_before
    assert not any(note.startswith("drill") for note in out.notes)


def test_escape_returns_to_the_route_the_click_left() -> None:
    out = _click("milestone", 7, "BAT-0007", keys=("Escape",))
    assert out.route == "milestone"


def test_an_open_overlay_keeps_the_click_from_opening_an_id() -> None:
    async def body() -> tuple[bool, str, str | None]:
        app = ConsoleApp(_fixture(), FakeClock())
        async with app.run_test(size=(120, 30)) as pilot:
            app.reset(SessionSetup(route="batch.detail", overlay="help"))
            app.render_frame()
            await pilot.pause()
            return app.open_id("EAWF-0003"), app.session.route, app.session.overlay

    assert asyncio.run(body()) == (False, "batch.detail", "help")


# ---------- the hit test, over the painter's own id pattern ----------


def test_id_at_on_an_empty_row_finds_nothing() -> None:
    assert id_at("", 0) is None


def test_id_at_on_a_single_id_row_covers_exactly_its_characters() -> None:
    row = "EAWF-0042"
    assert [id_at(row, x) for x in (0, len(row) - 1)] == ["EAWF-0042", "EAWF-0042"]
    assert id_at(row, len(row)) is None
    assert id_at(row, -1) is None


def test_id_at_tells_neighbouring_ids_apart() -> None:
    row = "ACT-0031 RUN-538453eb wants"
    assert id_at(row, 8) is None
    assert id_at(row, 9) == "RUN-538453eb"
    assert id_at(row, 7) == "ACT-0031"


def test_id_at_past_the_row_end_finds_nothing() -> None:
    assert id_at("TRK-EAWF-CORE", 500) is None


def test_id_at_ignores_a_word_that_is_not_a_typed_id() -> None:
    assert id_at("x-ray Abc-def", 1) is None
