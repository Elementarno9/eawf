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
from textual.widgets import TabbedContent

from eawf.state.enums import EffortBucket
from eawf.state.models import State
from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays.detail import (
    DetailCard,
    DetailModal,
    resolve_detail,
)
from eawf.tui_v2.snapshot import capture_screen_text
from eawf.tui_v2.widgets.backlog_table import BacklogTable
from eawf.tui_v2.widgets.eu_bar import EMPTY_STATE
from eawf.tui_v2.widgets.roadmap_tree import RoadmapTree

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_BACKLOG = _FIXTURES / "07-decisions-and-backlog.json"


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _state_with_bucketed_wave(bucket: EffortBucket = EffortBucket.L) -> tuple[State, str]:
    """Return a state whose single wave carries *bucket*, plus that wave id.

    The committed fixtures leave ``effort_bucket`` unset, so the size-bar
    paths need a wave with a populated bucket. Rebuilds the state with one
    wave's bucket overridden (the model is frozen — ``model_copy`` makes
    the edit) so the resolver renders a real size bar.
    """
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    bucketed = state.waves[wave_id].model_copy(update={"effort_bucket": bucket})
    new_waves = dict(state.waves)
    new_waves[wave_id] = bucketed
    return state.model_copy(update={"waves": new_waves}), wave_id


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
# resolve_detail — iter / phase cards (new in W05)
# --------------------------------------------------------------------------


def test_resolve_detail_iter_card() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    assert card.title == f"iter {iter_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "phase", "title", "status", "waves"} <= row_labels


def test_resolve_detail_phase_card() -> None:
    state = _load(_PHASE_ITER_WAVE)
    phase_id = next(iter(state.phases))
    card = resolve_detail(state, phase_id)
    assert card.title == f"phase {phase_id}"
    row_labels = {label for label, _ in card.rows}
    assert {"id", "scope", "title", "status", "iters", "waves"} <= row_labels


def test_resolve_detail_iter_metrics_has_completion_bar() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    metric_labels = {label for label, _ in card.metrics}
    assert "completion" in metric_labels


def test_resolve_detail_phase_metrics_has_completion_bar() -> None:
    state = _load(_PHASE_ITER_WAVE)
    phase_id = next(iter(state.phases))
    card = resolve_detail(state, phase_id)
    metric_labels = {label for label, _ in card.metrics}
    assert "completion" in metric_labels


def test_resolve_detail_wave_metrics_has_size_bar() -> None:
    state, wave_id = _state_with_bucketed_wave(EffortBucket.L)
    card = resolve_detail(state, wave_id)
    size_values = [value for label, value in card.metrics if label == "size"]
    assert size_values
    assert "L" in size_values[0]
    assert EMPTY_STATE not in size_values[0]


def test_resolve_detail_wave_metrics_eu_tokens_empty_state() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    metrics = dict(card.metrics)
    assert metrics["eu"] == EMPTY_STATE
    assert metrics["tokens"] == EMPTY_STATE


def test_resolve_detail_iter_metrics_eu_tokens_empty_state() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    metrics = dict(card.metrics)
    assert metrics["eu"] == EMPTY_STATE
    assert metrics["tokens"] == EMPTY_STATE


def test_resolve_detail_wave_has_dispatch_prompt() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    assert card.dispatch_prompt is not None
    # The rendered prompt is a wave dispatch Markdown doc — it names the wave.
    assert wave_id in card.dispatch_prompt


def test_resolve_detail_iter_dispatch_prompt_is_fallback() -> None:
    state = _load(_PHASE_ITER_WAVE)
    iter_id = next(iter(state.iters))
    card = resolve_detail(state, iter_id)
    assert card.dispatch_prompt == "no dispatch prompt for this entity"


def test_resolve_detail_backlog_has_no_metrics_or_dispatch() -> None:
    state = _load(_BACKLOG)
    item_id = next(iter(state.backlog))
    card = resolve_detail(state, item_id)
    assert card.metrics == ()
    assert card.dispatch_prompt is None


# --------------------------------------------------------------------------
# DetailModal._present_tabs — only data-bearing tabs are built
# --------------------------------------------------------------------------


def test_present_tabs_detail_always_present() -> None:
    card = DetailCard(title="t", rows=(("id", "x"),))
    assert DetailModal._present_tabs(card) == ("d",)


def test_present_tabs_wave_includes_metrics_and_dispatch() -> None:
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    tabs = DetailModal._present_tabs(card)
    assert "d" in tabs
    assert "m" in tabs
    assert "dp" in tabs


def test_present_tabs_skips_empty_events() -> None:
    # A wave with no dispatch history yields no ``e`` tab.
    state = _load(_PHASE_ITER_WAVE)
    wave_id = next(iter(state.waves))
    card = resolve_detail(state, wave_id)
    if not card.events:
        assert "e" not in DetailModal._present_tabs(card)


def test_present_tabs_order_follows_brief_sequence() -> None:
    card = DetailCard(
        title="t",
        rows=(("id", "x"),),
        metrics=(("size", "###--  M"),),
        history=(("status", "open"),),
        events=(("attempt 1", "fresh"),),
        dispatch_prompt="body",
    )
    assert DetailModal._present_tabs(card) == ("h", "d", "m", "e", "dp")


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


# --------------------------------------------------------------------------
# DetailModal tabs + bars (Pilot, W05)
# --------------------------------------------------------------------------


def test_detail_modal_iter_shows_completion_bar() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-m"
            await pilot.pause()
            rendered = capture_screen_text(app)
            # The closed/total count suffix of the completion bar is visible.
            assert "0/1" in rendered

    asyncio.run(body())


def test_detail_modal_wave_shows_size_bar() -> None:
    async def body() -> None:
        state, wave_id = _state_with_bucketed_wave(EffortBucket.L)
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            card = resolve_detail(state, wave_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            tabs = modal.query_one(TabbedContent)
            tabs.active = "detail-tab-m"
            await pilot.pause()
            rendered = capture_screen_text(app)
            # The size-bar row carries the bucket label.
            assert "L" in rendered

    asyncio.run(body())


def test_detail_modal_metrics_show_empty_state_for_eu_tokens() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            wave_id = next(iter(app.state.waves))  # type: ignore[union-attr]
            card = resolve_detail(app.state, wave_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            tabs = app.screen.query_one(TabbedContent)
            tabs.active = "detail-tab-m"
            await pilot.pause()
            rendered = capture_screen_text(app)
            assert EMPTY_STATE in rendered

    asyncio.run(body())


def test_detail_modal_tab_cycles_forward_and_back() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, DetailModal)
            tabs = modal.query_one(TabbedContent)
            # Lands on the detail tab by default.
            assert tabs.active == "detail-tab-d"
            await pilot.press("tab")
            await pilot.pause()
            after_tab = tabs.active
            assert after_tab != "detail-tab-d"
            await pilot.press("shift+tab")
            await pilot.pause()
            assert tabs.active == "detail-tab-d"

    asyncio.run(body())


def test_detail_modal_arrows_do_not_switch_tabs() -> None:
    """Arrow keys keep their scroll role — they must not cycle the tabs."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            tabs = app.screen.query_one(TabbedContent)
            before = tabs.active
            for key in ("down", "up", "left", "right"):
                await pilot.press(key)
                await pilot.pause()
            assert tabs.active == before

    asyncio.run(body())


def test_detail_modal_iter_opens_with_tabs() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            iter_id = next(iter(app.state.iters))  # type: ignore[union-attr]
            card = resolve_detail(app.state, iter_id)
            app.push_screen(DetailModal(card))
            await pilot.pause()
            assert isinstance(app.screen, DetailModal)
            assert iter_id in app.export_screenshot()

    asyncio.run(body())
