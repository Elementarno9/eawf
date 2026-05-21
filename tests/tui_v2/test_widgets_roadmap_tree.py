"""Unit + Pilot tests for the C06 ``RoadmapTree`` widget (P26-W17).

Covers the V12 glyph maps (pure), tree construction from a fixture state
(phase → iter → wave with status glyphs), the inline EU bar on an
estimate-wired iter, the empty/None-state clear path, and the
Enter-on-wave → :class:`RoadmapTree.WaveSelected` message seam.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from textual.app import App, ComposeResult

from eawf.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.state.models import State
from eawf.tui_v2.widgets.roadmap_tree import (
    ITER_GLYPHS,
    PHASE_GLYPHS,
    WAVE_GLYPHS,
    RoadmapTree,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"


class _Harness(App[None]):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield RoadmapTree(id="rt")


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _state_with_eu() -> State:
    """Return the phase/iter/wave fixture with an EU-wired iter.

    Attaches an estimate (4 EU) to the iter and an actual (2 EU consumed,
    matched by ``scope_id``) so the inline EU bar renders at 50 %.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["iters"]["P01-I01"]["estimate_id"] = "EST-1"
    payload["estimates"] = {
        "EST-1": {
            "id": "EST-1",
            "scope_id": "P01-I01",
            "expected_eu": 4.0,
            "pessimistic_eu": 8.0,
            "expected_minutes": 120.0,
            "pessimistic_minutes": 240.0,
            "display": "4 EU",
            "reference_class": None,
            "confidence": "medium",
            "current_store_record_id": "EST-REC-1",
            "updated_at": "2026-05-08T00:00:00Z",
        }
    }
    payload["actuals"] = {
        "ACT-1": {
            "id": "ACT-1",
            "scope_id": "P01-I01",
            "status": "active",
            "elapsed_eu": 2.0,
            "attention_eu": None,
            "agent_runtime_eu": None,
            "current_store_record_id": "ACT-REC-1",
            "updated_at": "2026-05-08T00:00:00Z",
        }
    }
    return State.model_validate(payload)


def _labels(tree: RoadmapTree) -> list[str]:
    """Flatten every non-root node label to a plain string."""
    out: list[str] = []

    def walk(node: object) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            out.append(str(child.label))
            walk(child)

    walk(tree.root)
    return out


def _node_by_data(tree: RoadmapTree, data: str) -> object:
    """Return the first tree node whose ``data`` payload equals *data*."""
    found: list[object] = []

    def walk(node: object) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            if child.data == data:  # type: ignore[attr-defined]
                found.append(child)
            walk(child)

    walk(tree.root)
    return found[0]


# --------------------------------------------------------------------------
# Glyph maps — V12 schema completeness (pure)
# --------------------------------------------------------------------------


def test_wave_glyphs_cover_every_status() -> None:
    assert set(WAVE_GLYPHS) == set(WaveStatus)


def test_phase_glyphs_cover_every_status() -> None:
    assert set(PHASE_GLYPHS) == set(PhaseStatus)


def test_iter_glyphs_cover_every_status() -> None:
    assert set(ITER_GLYPHS) == set(IterStatus)


def test_wave_glyph_values_match_v12_schema() -> None:
    assert WAVE_GLYPHS[WaveStatus.PENDING] == "-"
    assert WAVE_GLYPHS[WaveStatus.IN_PROGRESS] == "~"
    assert WAVE_GLYPHS[WaveStatus.CLOSED] == "#"
    assert WAVE_GLYPHS[WaveStatus.FAILED] == "!"


# --------------------------------------------------------------------------
# Tree construction from fixture
# --------------------------------------------------------------------------


def test_tree_builds_phase_iter_wave_hierarchy() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            labels = _labels(tree)
            assert any("P01" in lbl and "Bootstrap" in lbl for lbl in labels)
            assert any("P01-I01" in lbl for lbl in labels)
            assert any("P01-I01-W01" in lbl for lbl in labels)

    asyncio.run(body())


def test_tree_wave_row_carries_in_progress_glyph() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            wave_label = next(lbl for lbl in _labels(tree) if "W01" in lbl)
            assert wave_label.startswith("~ ")  # in_progress glyph

    asyncio.run(body())


def test_tree_none_state_clears_to_empty() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            assert _labels(tree)  # populated
            tree.state = None
            await pilot.pause()
            assert _labels(tree) == []

    asyncio.run(body())


# --------------------------------------------------------------------------
# Inline EU bar on an estimate-wired iter
# --------------------------------------------------------------------------


def test_tree_iter_row_shows_inline_eu_bar() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_with_eu()
            await pilot.pause()
            iter_label = next(lbl for lbl in _labels(tree) if "P01-I01 " in lbl)
            assert "50%" in iter_label  # 2 of 4 EU consumed
            assert "#" in iter_label

    asyncio.run(body())


def test_tree_iter_row_without_estimate_has_no_bar() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)  # iter has no estimate_id
            await pilot.pause()
            iter_label = next(lbl for lbl in _labels(tree) if "P01-I01 " in lbl)
            assert "%" not in iter_label

    asyncio.run(body())


# --------------------------------------------------------------------------
# Enter on a wave row -> WaveSelected message
# --------------------------------------------------------------------------


def test_enter_on_wave_posts_wave_selected() -> None:
    captured: list[str] = []

    class _CaptureHarness(_Harness):
        def on_roadmap_tree_wave_selected(self, message: RoadmapTree.WaveSelected) -> None:
            captured.append(message.wave_id)

    async def body() -> None:
        app = _CaptureHarness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            # Cursor starts unset (line -1): 1st down -> phase, 2nd -> iter,
            # 3rd -> wave leaf; Enter then drills into the wave.
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()

    asyncio.run(body())
    assert captured == ["P01-I01-W01"]


def test_enter_on_branch_does_not_post_wave_selected() -> None:
    captured: list[str] = []

    class _CaptureHarness(_Harness):
        def on_roadmap_tree_wave_selected(self, message: RoadmapTree.WaveSelected) -> None:
            captured.append(message.wave_id)

    async def body() -> None:
        app = _CaptureHarness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            # Land on the phase branch row, then Enter toggles it (no message).
            await pilot.press("down", "enter")
            await pilot.pause()

    asyncio.run(body())
    assert captured == []


# --------------------------------------------------------------------------
# Plain left / right collapse-expand (P26-W37)
# --------------------------------------------------------------------------


def test_right_expands_a_collapsed_branch() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            phase = _node_by_data(tree, "P01")
            phase.collapse()  # type: ignore[attr-defined]
            tree.move_cursor(phase)  # type: ignore[arg-type]
            await pilot.pause()
            assert not phase.is_expanded  # type: ignore[attr-defined]
            await pilot.press("right")
            await pilot.pause()
            assert phase.is_expanded  # type: ignore[attr-defined]

    asyncio.run(body())


def test_right_on_expanded_branch_descends_to_first_child() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            phase = _node_by_data(tree, "P01")
            tree.move_cursor(phase)  # type: ignore[arg-type]
            await pilot.pause()
            assert phase.is_expanded  # active phase auto-expands  # type: ignore[attr-defined]
            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == "P01-I01"  # descended to first child

    asyncio.run(body())


def test_left_collapses_an_expanded_branch() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            iter_node = _node_by_data(tree, "P01-I01")
            tree.move_cursor(iter_node)  # type: ignore[arg-type]
            await pilot.pause()
            assert iter_node.is_expanded  # active iter auto-expands  # type: ignore[attr-defined]
            await pilot.press("left")
            await pilot.pause()
            assert not iter_node.is_expanded  # type: ignore[attr-defined]

    asyncio.run(body())


def test_left_on_leaf_moves_cursor_to_parent() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            wave = _node_by_data(tree, "P01-I01-W01")
            assert not wave.allow_expand  # it is a leaf  # type: ignore[attr-defined]
            tree.move_cursor(wave)  # type: ignore[arg-type]
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == "P01-I01"  # ascended to iter parent

    asyncio.run(body())


def test_left_on_collapsed_branch_moves_cursor_to_parent() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            iter_node = _node_by_data(tree, "P01-I01")
            iter_node.collapse()  # type: ignore[attr-defined]
            tree.move_cursor(iter_node)  # type: ignore[arg-type]
            await pilot.pause()
            assert not iter_node.is_expanded  # type: ignore[attr-defined]
            await pilot.press("left")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == "P01"  # ascended to phase parent

    asyncio.run(body())


def test_left_on_top_level_collapsed_node_is_safe() -> None:
    """``←`` on a collapsed top-level node never lands the cursor on root."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            phase = _node_by_data(tree, "P01")
            phase.collapse()  # type: ignore[attr-defined]
            tree.move_cursor(phase)  # type: ignore[arg-type]
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            # Parent is the hidden root, which is skipped; cursor stays put.
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == "P01"

    asyncio.run(body())


def test_right_on_leaf_is_noop() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            wave = _node_by_data(tree, "P01-I01-W01")
            tree.move_cursor(wave)  # type: ignore[arg-type]
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == "P01-I01-W01"  # leaf, cursor unmoved

    asyncio.run(body())


def test_shift_arrow_parent_navigation_still_works() -> None:
    """W37 extends, not clobbers, Textual's inherited ``shift+left``."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            tree.focus()
            wave = _node_by_data(tree, "P01-I01-W01")
            tree.move_cursor(wave)  # type: ignore[arg-type]
            await pilot.pause()
            await pilot.press("shift+left")  # inherited cursor_parent
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data == "P01-I01"

    asyncio.run(body())
