"""Golden snapshot for the workspace table's cross-repo attention chips.

The workspace table renders an attention chip in each repo's cell when
that repo trips its blocker or stale-band threshold: a ``[blocked]`` chip
for a repo with a wave needing the operator now, a ``[stale]`` chip for a
repo whose state has gone stale, and both for a repo that trips both. A
calm repo renders just its code. This suite drives a populated table to a
golden ASCII frame so the chip layout is pinned byte-for-byte.

The table is mounted under a bare themed host (mirroring the bar-swap
suite) with its ``rows_data`` stubbed to fixture rows, so the chip render
is a pure function of the typed :class:`RepoRow` flags -- no off-disk
state read, deterministic across workers. Repo codes are abstract
placeholders (``ABC`` / ``DEF`` / ``GHI`` / ``JKL``), never real-looking
project names.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_workspace_chips.py
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
    BLOCKER_CHIP,
    STALE_CHIP,
    RepoRow,
    WorkspaceTable,
    attention_chip,
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


def _chip_rows() -> list[RepoRow]:
    """Return four repo rows spanning every chip combination.

    ``ABC`` is calm (no chip), ``DEF`` trips the blocker chip, ``GHI``
    trips the stale chip, and ``JKL`` trips both.
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
            blocker=True,
        ),
        RepoRow(
            code="GHI",
            path="/abs/ghi",
            phase_id="P03",
            phase_done=0,
            phase_total=2,
            eu_consumed=0.0,
            eu_total=1.0,
            age="20d",
            stale=True,
        ),
        RepoRow(
            code="JKL",
            path="/abs/jkl",
            phase_id="P04",
            phase_done=4,
            phase_total=4,
            eu_consumed=3.0,
            eu_total=3.0,
            age="21d",
            blocker=True,
            stale=True,
        ),
    ]


def test_workspace_chips_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each repo row renders an attention chip when its threshold trips."""

    async def body() -> None:
        table = WorkspaceTable(id="wt")
        monkeypatch.setattr(table, "rows_data", _chip_rows)
        app = _HostApp(table)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            table._rebuild()
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "workspace_chips.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# attention_chip -- pure chip-text contract behind the snapshot
# --------------------------------------------------------------------------


def _row(*, blocker: bool, stale: bool) -> RepoRow:
    """Build a minimal :class:`RepoRow` carrying only the chip flags."""
    return RepoRow(
        code="ABC",
        path="/abs/abc",
        phase_id=None,
        phase_done=0,
        phase_total=0,
        eu_consumed=0.0,
        eu_total=0.0,
        age="—",
        blocker=blocker,
        stale=stale,
    )


def test_attention_chip_calm_is_none() -> None:
    """A repo tripping neither threshold renders no chip."""
    assert attention_chip(_row(blocker=False, stale=False)) is None


def test_attention_chip_blocker_only() -> None:
    """A blocker-only repo renders just the blocker chip."""
    assert attention_chip(_row(blocker=True, stale=False)) == BLOCKER_CHIP


def test_attention_chip_stale_only() -> None:
    """A stale-only repo renders just the stale chip."""
    assert attention_chip(_row(blocker=False, stale=True)) == STALE_CHIP


def test_attention_chip_both_blocker_first() -> None:
    """A repo tripping both renders both chips, blocker first."""
    assert attention_chip(_row(blocker=True, stale=True)) == f"{BLOCKER_CHIP} {STALE_CHIP}"
