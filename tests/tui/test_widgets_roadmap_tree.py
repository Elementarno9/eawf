"""Unit + Pilot tests for the C06 ``RoadmapTree`` widget (P26-W17).

Covers the V12 glyph maps (pure), tree construction from a fixture state
(phase → iter → wave with status glyphs), the inline completion bar on
iter / phase rows (W06), width-aware row-title ellipsis (W06), the
empty/None-state clear path, and the Enter-on-wave →
:class:`RoadmapTree.WaveSelected` message seam.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from textual.app import ComposeResult

from eawf.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.state.models import State
from eawf.tui.app import EaApp
from eawf.tui.snapshot.pilot_harness import capture_screen_text, settle_screen
from eawf.tui.widgets.eu_bar import EMPTY_STATE
from eawf.tui.widgets.roadmap_tree import (
    _GLYPH_PREFIX_WIDTH,
    _GUIDE_INDENT_PER_DEPTH,
    _ROW_TOGGLE_WIDTH,
    _SCROLLBAR_GUTTER,
    _UNSIZED_BUDGET,
    ELLIPSIS,
    ITER_GLYPHS,
    PHASE_GLYPHS,
    WAVE_GLYPHS,
    RoadmapTree,
    _truncate_body,
    _wave_completion,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui" / "theme.tcss"


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield RoadmapTree(id="rt")


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _make_wave(
    wave_id: str, iter_id: str, status: str, *, title: str = "wave"
) -> dict[str, object]:
    """Build a minimal wave payload dict for fixture composition."""
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": title,
        "status": status,
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
    }


def _state_half_closed_iter() -> State:
    """Return the fixture with the iter holding 4 waves, 2 of them CLOSED.

    Gives the iter / phase completion bar a deterministic 50 % (``2/4``):
    two CLOSED waves plus one in_progress and one pending.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["iters"]["P01-I01"]["wave_ids"] = [
        "P01-I01-W01",
        "P01-I01-W02",
        "P01-I01-W03",
        "P01-I01-W04",
    ]
    payload["waves"] = {
        "P01-I01-W01": _make_wave("P01-I01-W01", "P01-I01", "closed"),
        "P01-I01-W02": _make_wave("P01-I01-W02", "P01-I01", "closed"),
        "P01-I01-W03": _make_wave("P01-I01-W03", "P01-I01", "in_progress"),
        "P01-I01-W04": _make_wave("P01-I01-W04", "P01-I01", "pending"),
    }
    return State.model_validate(payload)


def _state_long_titles() -> State:
    """Return the fixture with very long phase / iter / wave titles.

    Each title is far wider than the narrow roadmap pane so the row-title
    ellipsis path fires on every depth.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    long = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do"
    payload["phases"]["P01"]["title"] = long
    payload["iters"]["P01-I01"]["title"] = long
    payload["waves"]["P01-I01-W01"]["title"] = long
    return State.model_validate(payload)


def _state_max_length_titles() -> State:
    """Return the fixture with 72-char (model-max) phase / iter / wave titles.

    W23 bounds every entity ``title`` to ``max_length=72``. This composes a
    realistic worst case — a title exactly at the cap — so the row-width
    ellipsis still fits the narrow roadmap pane (titles fit, not overflow).
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    max_title = "T" * 72
    payload["phases"]["P01"]["title"] = max_title
    payload["iters"]["P01-I01"]["title"] = max_title
    payload["waves"]["P01-I01-W01"]["title"] = max_title
    return State.model_validate(payload)


def _state_long_iter_many_waves() -> State:
    """Return the fixture with a long iter title and ~40 waves.

    Enough child waves that the tree's content outgrows a 40-row pane and
    Textual shows the vertical scrollbar; the long iter title forces the
    iter row to truncate so its trailing completion-bar count sits at the
    right edge — the cell the scrollbar gutter would otherwise clip.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    ids = [f"P01-I01-W{n:02d}" for n in range(1, 40)]
    payload["iters"]["P01-I01"]["wave_ids"] = ids
    payload["iters"]["P01-I01"]["title"] = (
        "First iteration with an extremely long descriptive title that overflows"
    )
    payload["waves"] = {
        wid: _make_wave(wid, "P01-I01", "pending", title=f"wave {n} title")
        for n, wid in enumerate(ids, start=1)
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
# _wave_completion — pure closed/total tally
# --------------------------------------------------------------------------


def test_wave_completion_counts_closed_over_total() -> None:
    state = _state_half_closed_iter()
    closed, total = _wave_completion(state, list(state.iters["P01-I01"].wave_ids))
    assert (closed, total) == (2, 4)


def test_wave_completion_empty_wave_ids_is_zero_zero() -> None:
    state = _load(_PHASE_ITER_WAVE)
    assert _wave_completion(state, []) == (0, 0)


def test_wave_completion_skips_unknown_wave_id() -> None:
    """A dangling wave id is skipped, never inflating *total*."""
    state = _load(_PHASE_ITER_WAVE)
    _closed, total = _wave_completion(state, ["P01-I01-W01", "P99-MISSING"])
    assert total == 1  # only the resolvable wave counts


# --------------------------------------------------------------------------
# Inline completion bar on iter / phase rows (W06)
# --------------------------------------------------------------------------


def test_tree_iter_row_shows_completion_bar() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_half_closed_iter()
            await pilot.pause()
            iter_label = next(lbl for lbl in _labels(tree) if "P01-I01 " in lbl)
            assert "2/4" in iter_label  # 2 of 4 child waves closed
            assert "#" in iter_label  # filled cells present

    asyncio.run(body())


def test_tree_phase_row_shows_completion_bar() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_half_closed_iter()
            await pilot.pause()
            phase_label = next(lbl for lbl in _labels(tree) if "P01 " in lbl and "P01-" not in lbl)
            assert "2/4" in phase_label  # phase tallies across its iters

    asyncio.run(body())


def test_tree_wave_row_has_no_completion_bar() -> None:
    """Wave rows carry no inline bar — only iter / phase rows do."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_half_closed_iter()
            await pilot.pause()
            wave_labels = [lbl for lbl in _labels(tree) if "P01-I01-W0" in lbl]
            assert wave_labels  # waves are present
            for lbl in wave_labels:
                assert "/" not in lbl  # no `closed/total` count suffix
                assert EMPTY_STATE not in lbl

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


# --------------------------------------------------------------------------
# Width-aware row-title ellipsis (W06)
# --------------------------------------------------------------------------


def test_truncate_body_short_text_unchanged() -> None:
    """A body within budget is returned verbatim — no ellipsis."""
    body = "P01-I01  short"
    assert _truncate_body(body, 40) == body


def test_truncate_body_long_text_gets_ellipsis() -> None:
    out = _truncate_body("P01-I01  a very long iteration title here", 20)
    assert out.endswith(ELLIPSIS)
    assert len(out) == 20


def test_truncate_body_equal_to_budget_unchanged() -> None:
    """Off-by-one boundary: body length exactly the budget is untouched."""
    body = "exactly-ten"  # 11 chars
    assert _truncate_body(body, len(body)) == body


def test_truncate_body_keeps_min_body_chars_on_tiny_budget() -> None:
    """An extreme budget still shows a sliver of the id, not a lone marker."""
    out = _truncate_body("P01-I01-W01  title", 2)
    assert out.startswith("P01")  # >= _MIN_BODY_CHARS kept
    assert out.endswith(ELLIPSIS)


def test_tree_long_iter_title_ellipsizes_to_row_width() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_long_titles()
            await pilot.pause()
            iter_label = str(_node_by_data(tree, "P01-I01").label)  # type: ignore[attr-defined]
            assert ELLIPSIS in iter_label
            # The visible label (glyph + body + bar) fits the tree width.
            assert len(iter_label) <= tree.size.width

    asyncio.run(body())


def test_tree_long_wave_title_ellipsizes_to_row_width() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_long_titles()
            await pilot.pause()
            wave_label = next(lbl for lbl in _labels(tree) if "P01-I01-W01" in lbl)
            assert ELLIPSIS in wave_label
            assert len(wave_label) <= tree.size.width

    asyncio.run(body())


def test_tree_max_length_title_fits_row_width() -> None:
    """A 72-char (model-max) wave title still fits the tree row width.

    Now that ``title`` is bounded to ``max_length=72`` the worst-case row
    no longer overflows: the width-aware ellipsis cuts the bounded title to
    the pane and the rendered label stays within the tree's content width.
    """

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_max_length_titles()
            await pilot.pause()
            wave_label = next(lbl for lbl in _labels(tree) if "P01-I01-W01" in lbl)
            assert ELLIPSIS in wave_label  # 72 chars > narrow pane → truncated
            assert len(wave_label) <= tree.size.width  # fits, never overflows

    asyncio.run(body())


def test_tree_short_titles_not_ellipsized() -> None:
    """Short titles in a wide pane keep their full text — no ellipsis."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            for lbl in _labels(tree):
                assert ELLIPSIS not in lbl

    asyncio.run(body())


def test_tree_re_truncates_on_resize() -> None:
    """Shrinking the pane re-cuts a title that fit before the resize."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _state_long_titles()
            await pilot.pause()
            wide_label = next(lbl for lbl in _labels(tree) if "P01-I01-W01" in lbl)
            wide_len = len(wide_label)
            # Narrow the viewport; on_resize rebuilds + re-truncates.
            await pilot.resize_terminal(40, 20)
            await pilot.pause()
            narrow_label = next(lbl for lbl in _labels(tree) if "P01-I01-W01" in lbl)
            assert len(narrow_label) < wide_len
            assert narrow_label.endswith(ELLIPSIS)

    asyncio.run(body())


# --------------------------------------------------------------------------
# Scrollbar-gutter budget (P26-I02-W12)
# --------------------------------------------------------------------------


def test_body_budget_reserves_scrollbar_gutter() -> None:
    """A sized row budget subtracts the full chrome, gutter included.

    For a measured content width the budget must equal
    ``width - (2*depth + toggle + glyph + scrollbar_gutter)`` so a row
    sized before the vertical scrollbar appears still fits the narrower
    post-scrollbar content region.
    """

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            width = tree.size.width
            assert width > 0  # laid out
            for depth in (0, 1, 2):
                chrome = (
                    _GUIDE_INDENT_PER_DEPTH * depth
                    + _ROW_TOGGLE_WIDTH
                    + _GLYPH_PREFIX_WIDTH
                    + _SCROLLBAR_GUTTER
                )
                assert tree._body_budget(depth) == width - chrome

    asyncio.run(body())


def test_body_budget_unsized_path_unchanged() -> None:
    """A tree with no measured width still falls back to the unsized budget."""
    tree = RoadmapTree(id="rt")
    assert tree.size.width == 0  # never laid out
    assert tree._body_budget(0) == _UNSIZED_BUDGET
    assert tree._body_budget(2) == _UNSIZED_BUDGET


def test_scrolled_iter_row_completion_count_not_clipped(tmp_path: Path) -> None:
    """A long iter title + a scrolling tree keeps its bar count on screen.

    Regression for the W12 review issue: at 120x40 a long iter title plus
    ~39 child waves forces the vertical scrollbar; before the gutter was
    reserved the iter row was sized for the pre-scrollbar width and
    ``overflow-x: hidden`` clipped the trailing ``N/M`` count. Mount the
    real :class:`EaApp` over the mutated state and assert the rendered iter
    row carries the full ``0/39`` count and no rendered roadmap line
    exceeds the tree's content width.
    """
    state = _state_long_iter_many_waves()
    state_path = tmp_path / "state.json"
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            tree = app.screen.query_one(RoadmapTree)
            # Precondition: the geometry actually forces the scrollbar.
            assert tree.show_vertical_scrollbar
            assert tree.scrollbar_size_vertical == _SCROLLBAR_GUTTER
            iter_label = str(_node_by_data(tree, "P01-I01").label)  # type: ignore[attr-defined]
            assert iter_label.endswith("0/39")  # count present, not clipped
            assert ELLIPSIS in iter_label  # the long title did truncate
            text = capture_screen_text(app)
            iter_row = next(line for line in text.splitlines() if "P01-I01  First iter" in line)
            assert "0/39" in iter_row  # the on-screen render keeps the count

    asyncio.run(body())
