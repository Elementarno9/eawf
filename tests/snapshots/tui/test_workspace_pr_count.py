"""Golden snapshot for the workspace table's open-PR-count column.

The workspace table carries a ``pr`` column between the live git cell and
the age cell: each repo row shows that repo's open-PR total (a dash when
none are open, the integer otherwise), and the portfolio totals row shows
the summed open-PR count. This suite drives a populated table to a golden
ASCII frame so the PR column + per-row counts are pinned byte-for-byte.

No live source spans the workspace index, so the live dashboard renders 0
(a dash) for every repo; the typed :class:`RepoRow.open_prs` field is the
seam the test injects counts through, mounting the table under a bare
themed host with ``rows_data`` stubbed (the bar-swap pattern). Repo codes
are abstract placeholders (``ABC`` / ``DEF`` / ``GHI``), never
real-looking project names.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_workspace_pr_count.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive

from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.workspace_table import (
    RepoRow,
    WorkspaceTable,
    portfolio_totals,
)

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"
_SIZE = (120, 40)


class _HostApp(App[None]):
    """Bare themed host carrying the ``render_mode`` reactive the table reads."""

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("braille")

    def __init__(self, widget: object) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget  # type: ignore[misc]


def _pr_rows() -> list[RepoRow]:
    """Return three repo rows spanning the PR-count cases.

    ``ABC`` has 2 open PRs, ``DEF`` has 0 (the honest-empty dash), ``GHI``
    has 5; the totals row then sums to 7.
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
            open_prs=2,
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
            open_prs=0,
        ),
        RepoRow(
            code="GHI",
            path="/abs/ghi",
            phase_id="P03",
            phase_done=2,
            phase_total=2,
            eu_consumed=1.0,
            eu_total=1.0,
            age="3h",
            open_prs=5,
        ),
    ]


def test_workspace_pr_count_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard shows a PR-count column with the open-PR total per row."""

    async def body() -> None:
        table = WorkspaceTable(id="wt")
        monkeypatch.setattr(table, "rows_data", _pr_rows)
        app = _HostApp(table)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            table._rebuild()
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "workspace_pr_count.txt")

    asyncio.run(body())


def test_pr_column_present_and_totals_sum() -> None:
    """The ``pr`` column exists in the column set and the totals sum counts."""
    from eawf.surfaces.tui.widgets.workspace_table import _COLUMNS

    assert "pr" in _COLUMNS
    totals = portfolio_totals(_pr_rows())
    assert totals.open_prs == 7
