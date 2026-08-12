"""Pilot bar-swap test for StatusPane / RoadmapTree / WorkspaceTable.

Asserts the W24 swap: in the unicode render mode each of the three bar-
carrying widgets paints the W20 block-eighths bar
(:data:`~eawf.surfaces.render.bars.BLOCK_EIGHTHS`) in place of the prior
braille glyph. Each widget is mounted under a host that carries the
``render_mode`` reactive (seeded to the ``"unicode"`` mode), and a
populated state drives a non-empty fill so a block-eighths glyph is present.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive

from eawf.kernel.state.models import State
from eawf.surfaces.render.bars import BLOCK_EIGHTHS
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.status_pane import StatusPane
from eawf.surfaces.tui.widgets.workspace_table import RepoRow, WorkspaceTable

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _PHASE_ITER_WAVE.is_file(), f"missing fixture: {_PHASE_ITER_WAVE}"


def _has_block(text: str) -> bool:
    """Return ``True`` if *text* carries any block-eighths glyph."""
    return any(ch in BLOCK_EIGHTHS for ch in text)


def _tree_labels(tree: RoadmapTree) -> list[str]:
    """Flatten every non-root tree node label to a plain string."""
    out: list[str] = []

    def walk(node: object) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            out.append(str(child.label))
            walk(child)

    walk(tree.root)
    return out


class _UnicodeHostApp(App[None]):
    """Bare themed host carrying the unicode ``render_mode`` reactive.

    Mirrors :class:`~eawf.surfaces.tui.app.EaApp`'s theme bootstrap + the
    ``render_mode`` reactive the bar widgets read via
    ``getattr(self.app, "render_mode", ...)``, so a mounted widget resolves
    the unicode block-eighths fill the same way the live app does.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, widget: object) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget  # type: ignore[misc]


def _state_phase_half_closed() -> State:
    """Return the active fixture with a 2-wave iter, one CLOSED.

    Gives the active phase / iter a non-zero ``1/2`` completion ratio so the
    StatusPane ``progress`` bar and the RoadmapTree iter/phase completion
    bars carry a partly-filled block-eighths run.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["iters"]["P01-I01"]["wave_ids"] = ["P01-I01-W01", "P01-I01-W02"]
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = opened
    payload["waves"]["P01-I01-W02"] = {
        "id": "P01-I01-W02",
        "iter_id": "P01-I01",
        "title": "second",
        "status": "in_progress",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
    return State.model_validate(payload)


def _repo_rows_populated() -> list[RepoRow]:
    """Return two repo rows with non-zero phase completion + EU burn.

    Drives the WorkspaceTable phase + EU bar cells to a partly-filled
    block-eighths run.
    """
    return [
        RepoRow(
            code="ABC",
            path="/abs/abc",
            phase_id="P01",
            phase_done=3,
            phase_total=6,
            eu_consumed=2.0,
            eu_total=4.0,
            age="1h",
        ),
        RepoRow(
            code="DEF",
            path="/abs/def",
            phase_id="P02",
            phase_done=1,
            phase_total=4,
            eu_consumed=1.0,
            eu_total=8.0,
            age="2h",
        ),
    ]


def test_status_pane_renders_block_eighths_bar() -> None:
    """The StatusPane progress bar renders a block-eighths fill in unicode mode."""

    async def body() -> None:
        pane = StatusPane(id="sp")
        app = _UnicodeHostApp(pane)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            pane.state = _state_phase_half_closed()
            await settle_screen(pilot)
            rendered = str(pane.render())
            assert _has_block(rendered), f"no block-eighths glyph in status pane: {rendered!r}"

    asyncio.run(body())


def test_roadmap_tree_renders_block_eighths_bar() -> None:
    """The RoadmapTree completion bar renders a block-eighths fill in unicode mode."""

    async def body() -> None:
        tree = RoadmapTree(id="rt")
        app = _UnicodeHostApp(tree)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            tree.state = _state_phase_half_closed()
            await settle_screen(pilot)
            joined = "".join(_tree_labels(tree))
            assert _has_block(joined), f"no block-eighths glyph in roadmap tree: {joined!r}"

    asyncio.run(body())


def test_workspace_table_renders_block_eighths_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WorkspaceTable phase / EU bar cells render block-eighths in unicode mode."""

    async def body() -> None:
        table = WorkspaceTable(id="wt")
        monkeypatch.setattr(table, "rows_data", _repo_rows_populated)
        app = _UnicodeHostApp(table)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            table._rebuild()
            await settle_screen(pilot)
            cells = [str(table.get_row_at(r)) for r in range(table.row_count)]
            joined = "".join(cells)
            assert _has_block(joined), f"no block-eighths glyph in workspace table: {joined!r}"

    asyncio.run(body())
