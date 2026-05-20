"""Tests for the C06 ``DetailModal`` + the drill-in seam (P26-W19).

Two layers: pure :func:`resolve_detail` resolution (wave / backlog /
fallback) without Textual, and Pilot-driven routing of the W17 widget
selection messages (:class:`BacklogTable.RowActivated` /
:class:`RoadmapTree.WaveSelected`) into a mounted DetailModal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson

from eawf.state.models import State
from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays.detail import (
    DetailCard,
    DetailModal,
    resolve_detail,
)
from eawf.tui_v2.widgets.backlog_table import BacklogTable
from eawf.tui_v2.widgets.roadmap_tree import RoadmapTree

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_BACKLOG = _FIXTURES / "07-decisions-and-backlog.json"


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


# --------------------------------------------------------------------------
# resolve_detail — wave / backlog / fallback
# --------------------------------------------------------------------------


def test_resolve_detail_wave_card() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    assert card.title == f"wave {wave_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "iter", "title", "status"} <= row_labels


def test_resolve_detail_backlog_card() -> None:
    state = _load(_BACKLOG)
    item_id = next(iter(state.backlog))
    card = resolve_detail(state, item_id)
    assert card.title == f"backlog {item_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "title", "priority", "status"} <= row_labels


def test_resolve_detail_unknown_id_returns_fallback() -> None:
    state = _load(_PHASE_ITER_WAVE)
    card = resolve_detail(state, "DOES-NOT-EXIST")
    assert card.title == "detail DOES-NOT-EXIST"
    assert ("id", "DOES-NOT-EXIST") in card.rows


def test_resolve_detail_none_state_returns_fallback() -> None:
    card = resolve_detail(None, "X")
    assert card.title == "detail X"
    assert any(label == "note" for label, _ in card.rows)


def test_resolve_detail_wave_includes_success_criteria_rows() -> None:
    state = _load(_PHASE_ITER_WAVE)
    # Find a wave that carries success criteria, if any.
    wave = next(
        (w for w in state.waves.values() if w.success_criteria),
        None,
    )
    if wave is None:
        return
    card = resolve_detail(state, wave.id)
    criterion_rows = [value for label, value in card.rows if label == "criterion"]
    assert criterion_rows == list(wave.success_criteria)


# --------------------------------------------------------------------------
# DetailCard contract
# --------------------------------------------------------------------------


def test_detail_card_is_frozen() -> None:
    card = DetailCard(title="t", rows=())
    try:
        card.title = "x"  # type: ignore[misc]
    except AttributeError, TypeError:
        return
    raise AssertionError("DetailCard should be frozen")


# --------------------------------------------------------------------------
# Drill-in seam — W17 messages route to a DetailModal (Pilot)
# --------------------------------------------------------------------------


def test_backlog_row_activated_opens_detail_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_BACKLOG)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            item_id = next(iter(app.state.backlog))  # type: ignore[union-attr]
            table = app.screen.query_one(BacklogTable)
            table.post_message(BacklogTable.RowActivated(item_id))
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            assert item_id in app.export_screenshot()

    asyncio.run(body())


def test_roadmap_wave_selected_opens_detail_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            wave_id = next(iter(app.state.waves))  # type: ignore[union-attr]
            tree = app.screen.query_one(RoadmapTree)
            tree.post_message(RoadmapTree.WaveSelected(wave_id))
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            assert wave_id in app.export_screenshot()

    asyncio.run(body())


def test_detail_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            wave_id = next(iter(app.state.waves))  # type: ignore[union-attr]
            tree = app.screen.query_one(RoadmapTree)
            tree.post_message(RoadmapTree.WaveSelected(wave_id))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())
