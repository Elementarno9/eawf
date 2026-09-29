"""The small helpers every native frame shares: one label gutter, one cursor restore, one instant.

A labelled fact starts its value in the same column on every native frame, whichever helper
drew it; the cursor is restored by the row's stable id before its offset; and a projected
instant is read in one place, so a renderer never parses a stamp itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.surfaces.tui.console.derive import restore_by_id, sel_by_id
from eawf.surfaces.tui.console.format import instant
from eawf.surfaces.tui.console.renderers.children import lrow
from eawf.surfaces.tui.console.renderers.read_model import LABEL_W, label, more, wrapped
from eawf.surfaces.tui.console.session import Session

VALUE_AT = LABEL_W + 1


# ---------- one value column ----------


@pytest.mark.parametrize(
    "row",
    [
        label("STATE", "VALUE"),
        more("VALUE"),
        lrow("TASKS", "VALUE"),
        lrow("TASKS", "VALUE", cur=True),
        wrapped("NEEDS", "VALUE", 80)[0],
    ],
    ids=["label", "more", "lrow", "lrow-caret", "wrapped"],
)
def test_label_helpers_start_every_value_in_one_column(row: str) -> None:
    assert row.index("VALUE") == VALUE_AT


def test_wrapped_continues_under_its_own_value() -> None:
    lines = wrapped("NEEDS", " ".join(["word"] * 40), 60)
    assert len(lines) > 1
    assert all(line[:VALUE_AT].strip() == "" for line in lines[1:])
    assert all(line[VALUE_AT] != " " for line in lines)


def test_lrow_puts_the_caret_between_label_and_value() -> None:
    assert lrow("TASKS", "x", cur=True)[VALUE_AT - 2] == "▸"
    assert "▸" not in lrow("TASKS", "x")


# ---------- the cursor is restored by id ----------


def _session(sel: int = 0, sel_id: str | None = None) -> Session:
    session = Session()
    session.sel, session.sel_id = sel, sel_id
    return session


def test_restore_by_id_follows_the_row_after_a_resort() -> None:
    session = _session(sel=0, sel_id="B")
    assert restore_by_id(session, ["C", "A", "B"]) == 2
    assert (session.sel, session.sel_id) == (2, "B")


def test_restore_by_id_clamps_the_offset_when_the_row_is_gone() -> None:
    session = _session(sel=5, sel_id="Z")
    assert restore_by_id(session, ["A", "B"]) == 1
    assert session.sel_id == "B"


def test_restore_by_id_on_an_empty_list_publishes_no_id() -> None:
    session = _session(sel=3, sel_id="A")
    assert restore_by_id(session, []) == 0
    assert (session.sel, session.sel_id) == (0, None)


def test_restore_by_id_with_one_row_lands_on_it() -> None:
    session = _session(sel=0, sel_id=None)
    assert restore_by_id(session, ["A"]) == 0
    assert session.sel_id == "A"


def test_restore_by_id_never_lands_an_absent_selection_on_a_heading() -> None:
    session = _session(sel=2, sel_id=None)
    assert restore_by_id(session, [None, "A", "B"]) == 2


def test_sel_by_id_also_publishes_how_many_rows_it_walks() -> None:
    session = _session(sel=0, sel_id="B")
    assert sel_by_id(session, ["A", "B"]) == 1
    assert (session.count, session.nav_rows) == (2, 2)


# ---------- one reader of a projected instant ----------


def test_instant_reads_an_iso_stamp() -> None:
    assert instant("2026-09-29T12:00:00+00:00") == datetime(2026, 9, 29, 12, tzinfo=UTC)


@pytest.mark.parametrize("stamp", [None, "", "yesterday", "2026-13-40T99:00:00"])
def test_instant_of_an_absent_or_unreadable_stamp_is_none(stamp: str | None) -> None:
    assert instant(stamp) is None
