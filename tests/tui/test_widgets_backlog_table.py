"""Unit + Pilot tests for the C06 ``BacklogTable`` widget (P26-W17).

Covers the pure sort / filter / sort-cycle helpers (including the
invalid-sort-key error path), the widget's row rebuild from state, the
``/filter backlog`` apply path, the sort cycle, and the Enter →
:class:`BacklogTable.RowActivated` modal seam.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from textual.app import ComposeResult

from eawf.kernel.state.models import BacklogItem, State
from eawf.surfaces.tui.widgets.backlog_table import (
    _COLUMN_LABELS,
    _ELLIPSIS,
    _SORT_GLYPH,
    _TITLE_MIN_WIDTH,
    SORT_KEYS,
    BacklogTable,
    _fixed_columns_width,
    _truncate,
    column_label,
    filter_items,
    next_sort_key,
    sort_items,
    title_budget,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_BACKLOG = _FIXTURES / "07-decisions-and-backlog.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"


def _item(item_id: str, priority: str, status: str, title: str) -> BacklogItem:
    return BacklogItem.model_validate(
        {
            "id": item_id,
            "scope_id": "QR",
            "title": title,
            "priority": priority,
            "status": status,
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "resolution": None,
            "commit": None,
        }
    )


def _items() -> list[BacklogItem]:
    return [
        _item("BL-003", "P2", "open", "Refactor loader"),
        _item("BL-001", "P0", "in_progress", "Wire init"),
        _item("BL-002", "P1", "closed", "Add metrics"),
    ]


def _state_with_backlog(items: list[BacklogItem]) -> State:
    payload = orjson.loads(_BACKLOG.read_bytes())
    payload["backlog"] = {it.id: orjson.loads(it.model_dump_json()) for it in items}
    return State.model_validate(payload)


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield BacklogTable(id="bt")


# --------------------------------------------------------------------------
# sort_items — by priority / id / status + error path
# --------------------------------------------------------------------------


def test_sort_items_by_priority_p0_first() -> None:
    ordered = sort_items(_items(), "priority")
    assert [it.id for it in ordered] == ["BL-001", "BL-002", "BL-003"]


def test_sort_items_by_id() -> None:
    ordered = sort_items(_items(), "id")
    assert [it.id for it in ordered] == ["BL-001", "BL-002", "BL-003"]


def test_sort_items_by_status() -> None:
    ordered = sort_items(_items(), "status")
    # status string order: closed < in_progress < open.
    assert [it.status.value for it in ordered] == ["closed", "in_progress", "open"]


def test_sort_items_unknown_key_raises() -> None:
    with pytest.raises(ValueError, match="unknown sort key"):
        sort_items(_items(), "bogus")


def test_sort_items_does_not_mutate_input() -> None:
    items = _items()
    before = [it.id for it in items]
    sort_items(items, "priority")
    assert [it.id for it in items] == before


# --------------------------------------------------------------------------
# filter_items — substring over id + title; empty restores all
# --------------------------------------------------------------------------


def test_filter_items_matches_title_case_insensitive() -> None:
    matched = filter_items(_items(), "METRICS")
    assert [it.id for it in matched] == ["BL-002"]


def test_filter_items_matches_id() -> None:
    matched = filter_items(_items(), "bl-003")
    assert [it.id for it in matched] == ["BL-003"]


def test_filter_items_empty_needle_returns_all() -> None:
    assert len(filter_items(_items(), "   ")) == 3


def test_filter_items_no_match_returns_empty() -> None:
    assert filter_items(_items(), "zzz") == []


# --------------------------------------------------------------------------
# next_sort_key — cycle + reset
# --------------------------------------------------------------------------


def test_next_sort_key_cycles_and_wraps() -> None:
    first = SORT_KEYS[0]
    second = next_sort_key(first)
    assert second == SORT_KEYS[1]
    # Wrap from the last back to the first.
    assert next_sort_key(SORT_KEYS[-1]) == SORT_KEYS[0]


def test_next_sort_key_unknown_resets_to_first() -> None:
    assert next_sort_key("bogus") == SORT_KEYS[0]


# --------------------------------------------------------------------------
# Widget — rebuild, filter apply, sort cycle, Enter seam
# --------------------------------------------------------------------------


def test_table_rebuilds_rows_from_state() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            assert table.row_count == 2
            # Default sort is priority -> P0 (BL-001) first.
            assert next(it.id for it in table.visible_items()) == "BL-001"
            table.show_closed = True
            await pilot.pause()
            assert table.row_count == 3

    asyncio.run(body())


def test_table_apply_filter_narrows_rows() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            table.apply_filter("metrics")
            await pilot.pause()
            assert table.row_count == 0
            assert table.visible_items() == []
            table.show_closed = True
            await pilot.pause()
            assert table.row_count == 1
            assert [it.id for it in table.visible_items()] == ["BL-002"]

    asyncio.run(body())


def test_clear_filter_key_restores_all_rows() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            # Filter down to the in-progress row, then ``x`` clears it.
            table.apply_filter("wire")
            await pilot.pause()
            assert table.row_count == 1
            assert table.filter_text == "wire"
            table.focus()
            await pilot.press("x")
            await pilot.pause()
            # CLOSED rows stay hidden (show_closed still False), so the two
            # non-closed rows return.
            assert table.filter_text == ""
            assert table.row_count == 2

    asyncio.run(body())


def test_clear_filter_key_is_noop_without_active_filter() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            before = table.row_count
            table.focus()
            await pilot.press("x")
            await pilot.pause()
            assert table.filter_text == ""
            assert table.row_count == before

    asyncio.run(body())


def test_clear_filter_method_resets_filter_text() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            table.apply_filter("metrics")
            await pilot.pause()
            table.clear_filter()
            await pilot.pause()
            assert table.filter_text == ""

    asyncio.run(body())


def test_table_cycle_sort_changes_order() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            assert table.sort_key == SORT_KEYS[0]
            table.cycle_sort()
            await pilot.pause()
            assert table.sort_key == SORT_KEYS[1]

    asyncio.run(body())


def test_enter_on_row_posts_row_activated() -> None:
    captured: list[str] = []

    class _CaptureHarness(_Harness):
        def on_backlog_table_row_activated(self, message: BacklogTable.RowActivated) -> None:
            captured.append(message.item_id)

    async def body() -> None:
        app = _CaptureHarness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            table.focus()
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(body())
    # Cursor starts on the first row; default priority sort -> BL-001.
    assert captured == ["BL-001"]


def test_table_empty_when_no_backlog() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            # 01-empty-repo has backlog == None.
            table.state = State.model_validate(
                orjson.loads((_FIXTURES / "01-empty-repo.json").read_bytes())
            )
            await pilot.pause()
            assert table.row_count == 0
            assert table.visible_items() == []

    asyncio.run(body())


# --------------------------------------------------------------------------
# _truncate — width-aware clip with ellipsis
# --------------------------------------------------------------------------


def test_truncate_short_text_untouched() -> None:
    assert _truncate("short", 48) == "short"


def test_truncate_exact_width_untouched() -> None:
    text = "x" * 10
    assert _truncate(text, 10) == text


def test_truncate_long_text_ellipsised_to_width() -> None:
    clipped = _truncate("abcdefghij", 5)
    assert clipped == "abcd" + _ELLIPSIS
    assert len(clipped) == 5


def test_truncate_width_one_yields_only_ellipsis() -> None:
    assert _truncate("abc", 1) == _ELLIPSIS


def test_truncate_width_below_one_floors_to_one() -> None:
    assert _truncate("abc", 0) == _ELLIPSIS


# --------------------------------------------------------------------------
# title_budget — content area minus fixed columns + padding, floored
# --------------------------------------------------------------------------


def test_title_budget_subtracts_fixed_columns_and_padding() -> None:
    # content 80, fixed cols 30, padding 1*2 per col * 4 cols = 8 -> 42.
    assert title_budget(80, 30, 1, 4) == 42


def test_title_budget_floors_at_minimum() -> None:
    # A narrow pane can drive the raw budget negative; the floor holds.
    assert title_budget(10, 30, 1, 4) == _TITLE_MIN_WIDTH


def test_title_budget_custom_floor() -> None:
    assert title_budget(10, 30, 1, 4, floor=3) == 3


def test_title_budget_zero_padding() -> None:
    assert title_budget(50, 20, 0, 4) == 30


def test_fixed_columns_width_uses_header_floor_when_empty() -> None:
    # With no rows the fixed columns size to their rendered header labels. The
    # priority label is the short "pri" (W09 fix c), and the default sort
    # (priority) adds the sort glyph to that column ("pri v").
    expected = len("id") + len("pri v") + len("status")
    assert _fixed_columns_width([]) == expected


def test_fixed_columns_width_grows_with_widest_cell() -> None:
    items = [_item("BL-LONG-0001", "P0", "in_progress", "t")]
    width = _fixed_columns_width(items)
    # id widens to the 12-char id; status widens to "in_progress" (11); the
    # priority column floors at the glyph-suffixed "pri v" header (5).
    assert width == len("BL-LONG-0001") + len("pri v") + len("in_progress")


def test_fixed_columns_width_priority_label_short_when_not_sorted() -> None:
    """Sorting by a non-priority key drops the glyph -> the bare ``pri`` floor."""
    # With id as the active sort, the id column carries the glyph ("id v") and
    # the priority column floors at the bare short label ("pri", width 3) --
    # the reclaimed space the 8-char "priority" header used to waste.
    width = _fixed_columns_width([], "id")
    assert width == len("id v") + len("pri") + len("status")


# --------------------------------------------------------------------------
# Widget — width-aware title rendering + reactive re-truncate on resize
# --------------------------------------------------------------------------

_LONG_TITLE = "A very long backlog title that overflows any reasonable column"


def _rendered_title(table: BacklogTable, item_id: str) -> str:
    return str(table.get_cell(item_id, "title"))


def test_table_renders_short_title_untouched() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog([_item("BL-001", "P0", "open", "Wire init")])
            await pilot.pause()
            assert _rendered_title(table, "BL-001") == "Wire init"

    asyncio.run(body())


def test_table_truncates_long_title_to_column_width() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog([_item("BL-001", "P0", "open", _LONG_TITLE)])
            await pilot.pause()
            rendered = _rendered_title(table, "BL-001")
            assert rendered.endswith(_ELLIPSIS)
            assert len(rendered) < len(_LONG_TITLE)
            # The rendered title fits inside the title budget for this width.
            budget = table._title_budget(table.visible_items())
            assert len(rendered) == budget

    asyncio.run(body())


def test_table_resize_narrower_re_truncates_title() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog([_item("BL-001", "P0", "open", _LONG_TITLE)])
            await pilot.pause()
            wide = _rendered_title(table, "BL-001")
            wide_budget = table._title_budget(table.visible_items())
            # Shrink the terminal: the resize watcher rebuilds with a
            # smaller title budget, so the rendered title gets shorter.
            await pilot.resize_terminal(50, 12)
            await pilot.pause()
            narrow = _rendered_title(table, "BL-001")
            narrow_budget = table._title_budget(table.visible_items())
            assert narrow_budget < wide_budget
            assert len(narrow) < len(wide)
            assert narrow.endswith(_ELLIPSIS)

    asyncio.run(body())


# --------------------------------------------------------------------------
# column_label - short priority label + active-sort glyph (W09 b + c)
# --------------------------------------------------------------------------


def test_column_label_priority_uses_short_pri_label() -> None:
    """The priority column's display label is the short ``pri`` (fix c)."""
    assert _COLUMN_LABELS["priority"] == "pri"
    # Sorted by a different key -> bare short label, no glyph.
    assert column_label("priority", "id") == "pri"


def test_column_label_active_column_carries_sort_glyph() -> None:
    """The active sort column's header gets the ``_SORT_GLYPH`` suffix."""
    assert column_label("priority", "priority") == f"pri {_SORT_GLYPH}"
    assert column_label("id", "id") == f"id {_SORT_GLYPH}"
    assert column_label("status", "status") == f"status {_SORT_GLYPH}"


def test_column_label_inactive_column_has_no_glyph() -> None:
    """A column that is not the active sort renders without the glyph."""
    assert column_label("id", "priority") == "id"
    assert column_label("status", "priority") == "status"
    assert _SORT_GLYPH not in column_label("status", "id")


def _header_label(table: BacklogTable, column_key: str) -> str:
    """Return the rendered header text for *column_key* (the ``Column.label``)."""
    for column in table.columns.values():
        if str(column.key.value) == column_key:
            return str(column.label)
    raise AssertionError(f"no column {column_key!r}")


def test_table_priority_header_renders_short_label() -> None:
    """The mounted table's priority header shows ``pri`` (width 3), not ``priority``.

    Fix (c): the 8-char ``priority`` header drove the priority column's fixed
    width even though its values are the 2-char ``P0``..``P3`` codes. The
    short ``pri`` label reclaims that space; the column *key* stays
    ``priority`` so the sort + cell lookups are unchanged.
    """

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            # Sort off priority so the header carries no glyph -> bare "pri".
            table.sort_key = "id"
            await pilot.pause()
            header = _header_label(table, "priority")
            assert header == "pri"  # short label, width 3
            assert "priority" not in header
            # The column key is unchanged, so sorting by priority still works.
            table.sort_key = "priority"
            await pilot.pause()
            assert [it.id for it in table.visible_items()] == ["BL-001", "BL-003"]

    asyncio.run(body())


def test_table_active_sort_header_glyph_on_right_column() -> None:
    """The active sort renders the header glyph on the sorted column, and only it."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            # Default sort is priority -> the glyph sits on the priority header.
            assert _header_label(table, "priority") == f"pri {_SORT_GLYPH}"
            assert _SORT_GLYPH not in _header_label(table, "id")
            assert _SORT_GLYPH not in _header_label(table, "status")

    asyncio.run(body())


def test_table_s_key_cycles_sort_through_all_keys() -> None:
    """The bound ``s`` key advances the sort key through the full cycle + wraps."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            table.focus()
            assert table.sort_key == SORT_KEYS[0]
            seen = [table.sort_key]
            for _ in range(len(SORT_KEYS)):
                await pilot.press("s")
                await pilot.pause()
                seen.append(table.sort_key)
            # Pressing N times steps through every key and wraps to the start.
            assert seen == [*SORT_KEYS, SORT_KEYS[0]]

    asyncio.run(body())


def test_table_s_key_moves_header_glyph_to_new_column() -> None:
    """Cycling the sort with ``s`` moves the header glyph onto the new column."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            table = app.query_one("#bt", BacklogTable)
            table.state = _state_with_backlog(_items())
            await pilot.pause()
            table.focus()
            # priority -> id: the glyph leaves priority and lands on id.
            await pilot.press("s")
            await pilot.pause()
            assert table.sort_key == "id"
            assert _header_label(table, "id") == f"id {_SORT_GLYPH}"
            assert _SORT_GLYPH not in _header_label(table, "priority")

    asyncio.run(body())
