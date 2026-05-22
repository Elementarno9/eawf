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

from eawf.state.models import BacklogItem, State
from eawf.tui_v2.widgets.backlog_table import (
    SORT_KEYS,
    BacklogTable,
    filter_items,
    next_sort_key,
    sort_items,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_BACKLOG = _FIXTURES / "07-decisions-and-backlog.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"


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
            assert table.row_count == 3
            # Default sort is priority -> P0 (BL-001) first.
            assert next(it.id for it in table.visible_items()) == "BL-001"

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
            assert table.row_count == 1
            assert [it.id for it in table.visible_items()] == ["BL-002"]

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
