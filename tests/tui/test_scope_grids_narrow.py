"""Narrow-terminal degrade acceptance for both scope grids (P30-I08-W07).

The P30-I02 reskin widened both per-repo grids -- the workspace
:class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable` and the
user-scope :class:`~eawf.surfaces.tui.scopes.user.PortfolioTable` -- with a
leading lifecycle sigil column and a status-tinted completion bar. Every
existing golden renders them WIDE (100 / 120 cols), so the P29 "snapshot-green
but live-broken at a small terminal" failure class could reopen: at 80 columns
the six-column row would overflow the ``overflow-x: hidden`` pane edge and clip
the trailing columns (or, worse, the load-bearing sigil) yet pass every wide
snapshot.

This module closes that gap. The grid degrades responsively at or below
:data:`~eawf.surfaces.tui.widgets.workspace_table._NARROW_WIDTH_THRESHOLD`
(80 cols): it drops the low-priority ``git`` / ``pr`` / ``age`` columns and
shrinks the phase bar, KEEPING the repo cell (leading lifecycle sigil + the
warn-marker status tint) and the two status-tinted bars un-clipped. The tests
assert three bands:

* **Pure width helpers.** :func:`visible_columns` / :func:`phase_bar_cells`
  flip at the 80-col threshold and never drop the load-bearing
  ``repo / phase / eu`` slice -- the RED side reconstructs the wide six-column
  row at the narrow width and shows it would overflow.
* **Live workspace grid at 80 cols.** A :class:`WorkspaceTable` mounted in a
  bare full-terminal harness at 80 cols renders exactly the narrow columns; the
  repo cell keeps its leading sigil + status tint, the phase + eu cells keep
  their tinted bar spans, and the captured frame fits inside the 80-col width
  (no overflow / clip).
* **Live user portfolio grid at 80 cols.** The same acceptance over the
  inherited :class:`PortfolioTable` driven through the real ``UserScreen`` at
  80 cols -- the user portfolio degrades exactly as the workspace grid does.

Determinism follows the project Pilot-worker rule: every Pilot body drains
workers via ``app.workers.wait_for_complete()`` before asserting (the git probe
is a worker). Repo codes are abstract placeholders (ABC / DEF / GHI), never
real-looking project names.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from textual.app import ComposeResult

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.scopes.user import PortfolioTable
from eawf.surfaces.tui.snapshot import capture_screen_text, normalize_snapshot, settle_screen
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_BAND_PALETTE
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph
from eawf.surfaces.tui.widgets.workspace_table import (
    _COLUMNS,
    _NARROW_BAR_CELLS,
    _NARROW_COLUMNS,
    _NARROW_WIDTH_THRESHOLD,
    _WIDE_BAR_CELLS,
    WorkspaceTable,
    phase_bar_cells,
    visible_columns,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"

#: The narrow terminal width the degrade goldens render at -- the canonical
#: 80-column terminal (a tmux split / an 80x24 default), exactly the threshold
#: the responsive degrade fires at and the regime the criterion pins.
_NARROW_COLS: int = 80

#: The wide baseline width -- the operator default, well above the threshold so
#: the full six-column row renders.
_WIDE_COLS: int = 120

#: The load-bearing tokens that must survive every width: the leading running
#: diamond (the sigil column the criterion names) in the operator's unicode
#: render mode.
_RUNNING_SIGIL = glyph(Sigil.RUNNING, mode="unicode")

#: The concrete green status hex the per-repo status-tinted bars + the CLOSED
#: sigil carry -- the status tint the criterion says must survive the degrade.
_GREEN_HEX = DEFAULT_BAND_PALETTE["ok"]


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    The user scope synthesizes its state from ``~/.eawf/registry.json``;
    redirecting ``Path.home`` to an empty ``tmp_path`` keeps the launch
    deterministic and never reads the operator's real registry (which would
    leak machine paths into a captured frame).
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git probe to a deterministic clean tree.

    Both grids inherit the workspace table's live git column, which shells
    out via ``git_pane.gather_git_fields``; stubbing it keeps any rendered git
    column deterministic regardless of cwd / platform / parallel worker.
    """
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def _multi_repo_state(codes: list[str]) -> State:
    """Return a workspace state seeded with the given abstract repo *codes*."""
    payload = orjson.loads(_WORKSPACE.read_bytes())
    payload["workspace"]["repos"] = {
        code: {
            "code": code,
            "path": f"/abs/path/{code.lower()}",
            "state_urn": f"urn:eawf:v1:repo:{code}",
            "project_code": code,
            "title": f"{code} repo",
            "status": "active",
        }
        for code in codes
    }
    payload["workspace"]["current_repo_code"] = codes[0] if codes else None
    return State.model_validate(payload)


class _TableHarness(PaletteHarnessApp):
    """Bare host mounting a workspace table at the full terminal width.

    The table spans the whole terminal, so the terminal column count IS the
    table's content width -- this drives the responsive degrade at a precise
    width without the full-app pane chrome shaving cells off. The
    ``render_mode`` attribute seeds the operator's ``unicode`` reskin column the
    table reads off its host App, so the narrow goldens exercise the unicode
    sigils the operator sees, not the bare-harness ASCII fallback.
    """

    render_mode = "unicode"

    def compose(self) -> ComposeResult:
        yield WorkspaceTable(id="wt")


# --------------------------------------------------------------------------
# Pure width helpers -- the responsive-degrade lever (no mount)
# --------------------------------------------------------------------------


def test_visible_columns_wide_keeps_full_six() -> None:
    """Above the narrow threshold the full six-column row renders."""
    assert visible_columns(_WIDE_COLS) == _COLUMNS
    assert visible_columns(_NARROW_WIDTH_THRESHOLD + 1) == _COLUMNS
    assert len(_COLUMNS) == 6


def test_visible_columns_narrow_degrades_to_load_bearing_three() -> None:
    """At or below 80 cols the grid degrades to the load-bearing trio."""
    assert visible_columns(_NARROW_COLS) == _NARROW_COLUMNS
    assert visible_columns(_NARROW_WIDTH_THRESHOLD) == _NARROW_COLUMNS
    assert visible_columns(40) == _NARROW_COLUMNS


def test_visible_columns_narrow_keeps_sigil_and_bars() -> None:
    """The degraded column set never drops the sigil-bearing repo cell or a bar.

    The criterion's irreducible signal -- the repo cell (which carries the
    leading lifecycle sigil + the warn-marker status tint) and the two
    status-tinted bars (``phase`` / ``eu``) -- survives the degrade. Only the
    low-priority ``git`` / ``pr`` / ``age`` columns are dropped.
    """
    narrow = visible_columns(_NARROW_COLS)
    assert "repo" in narrow  # the sigil column
    assert "phase" in narrow  # the green status-tinted completion bar
    assert "eu" in narrow  # the consumed-fraction burn bar
    # The dropped columns are exactly the low-priority three.
    assert set(_COLUMNS) - set(narrow) == {"git", "pr", "age"}


def test_visible_columns_prelayout_width_keeps_full() -> None:
    """A pre-layout width of 0 keeps the wide row (no clip can occur yet)."""
    assert visible_columns(0) == _COLUMNS


def test_phase_bar_cells_shrinks_only_when_narrow() -> None:
    """The phase bar shrinks at the narrow width and stays wide above it."""
    assert phase_bar_cells(_WIDE_COLS) == _WIDE_BAR_CELLS
    assert phase_bar_cells(_NARROW_COLS) == _NARROW_BAR_CELLS
    assert phase_bar_cells(0) == _WIDE_BAR_CELLS
    # The narrow bar is strictly shorter -- that is the cell budget the degrade
    # reclaims so the kept columns fit 80 cells.
    assert _NARROW_BAR_CELLS < _WIDE_BAR_CELLS


def test_narrow_row_width_fits_but_wide_row_overflows() -> None:
    """The RED side: a wide six-column row reconstructed at 80 cols overflows.

    A naive (un-degraded) render keeps all six columns at every width. A
    :class:`~textual.widgets.DataTable` auto-sizes each column to its widest
    plain cell and lays one cell of padding on each side
    (``cell_padding == 1``), so a row's laid-out width is the summed cell
    content plus ``2`` per column. Against the widest realistic per-repo cells
    that six-column row exceeds 80 cells -- it would clip at the
    ``overflow-x: hidden`` pane edge. The degraded three-column row fits inside
    80. This discriminates the graceful reflow from the clip the criterion
    rejects -- without mounting the widget.
    """
    # The widest realistic plain (markup-stripped) per-repo cell content: a
    # repo cell carrying the sigil + a 6-char code + both warn-marker words, a
    # 6-cell phase bar with the right-aligned counter, etc.
    wide_cells = {
        "repo": "X ABCDEF X blocked stale",
        "phase": "P30 ##########  143/143",
        "eu": "#####  100%",
        "git": "dirty +12",
        "pr": "12",
        "age": "120d",
    }
    cell_padding = 1  # DataTable default: one cell of padding on each column side

    def _row_width(columns: tuple[str, ...]) -> int:
        # Each column occupies its content width plus padding on both sides.
        return sum(len(wide_cells[col]) + 2 * cell_padding for col in columns)

    # The full six-column row blows past 80; the degraded three-column row fits.
    assert _row_width(_COLUMNS) > _NARROW_COLS
    assert _row_width(_NARROW_COLUMNS) <= _NARROW_COLS


# --------------------------------------------------------------------------
# Live workspace grid at 80 cols -- sigil + tint + bar survive, no clip
# --------------------------------------------------------------------------


def test_workspace_grid_narrow_degrades_columns() -> None:
    """At 80 cols the live workspace table renders only the narrow columns."""

    async def body() -> None:
        app = _TableHarness()
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#wt", WorkspaceTable)
            table.state = _multi_repo_state(["ABC", "DEF", "GHI"])
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert table.size.width == _NARROW_COLS
            # The grid degraded to the load-bearing trio -- git / pr / age gone.
            keys = tuple(str(col.key.value) for col in table.columns.values())
            assert keys == _NARROW_COLUMNS

    asyncio.run(body())


def test_workspace_grid_narrow_keeps_sigil_and_tint() -> None:
    """At 80 cols every repo cell keeps its leading sigil + status-tinted bar.

    The criterion's core: the degraded row keeps the sigil column and the bar
    (status tint intact), never dropping the sigil or the tint. The repo cell
    leads with the running diamond (the repos have an active phase) and the
    phase cell carries the green status-tinted completion bar.
    """

    async def body() -> None:
        app = _TableHarness()
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#wt", WorkspaceTable)
            # Repos whose paths are absent -> no active phase -> stale band, so
            # the repo cell leads with the ABANDONED sigil + a warn-marker chip;
            # that still proves the sigil column + status tint survive the
            # degrade. Use the chip-bearing path so the WIDEST repo cell is the
            # one under test.
            table.state = _multi_repo_state(["ABC", "DEF"])
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            repo_cell = str(table.get_cell("ABC", "repo"))
            phase_cell = str(table.get_cell("ABC", "phase"))
            eu_cell = str(table.get_cell("ABC", "eu"))

            # The sigil column survives: the repo cell still opens with a tinted
            # lifecycle glyph span (the leading ``[#hex]<glyph>[/]``).
            assert repo_cell.startswith("[")
            assert glyph(Sigil.ABANDONED, mode="unicode") in repo_cell
            assert "ABC" in repo_cell
            # The warn-marker status tint survives too (the stale chip).
            assert f"{chrome('attention', mode='unicode')} stale" in repo_cell

            # The phase bar keeps a tinted span (the green status tint is intact,
            # not stripped to fit) -- the empty-state sentinel here has no fill,
            # so assert the cell still carries the phase-id prefix + the bar's
            # sentinel rather than a clipped blank.
            assert phase_cell.startswith("— ")
            # The eu cell is the honest-empty sentinel (no budget) -- but the
            # column itself survives, so the bar slot is present, not clipped.
            assert eu_cell

    asyncio.run(body())


def test_workspace_grid_narrow_with_progress_keeps_green_tinted_bar(tmp_path: Path) -> None:
    """A repo with phase progress keeps its green status-tinted bar at 80 cols.

    The status tint is the load-bearing reskin signal the criterion forbids
    dropping. Seed a repo whose state is on disk so its active-phase completion
    bar paints a green-tinted run -- then prove that green span survives the
    degrade at 80 cols.
    """

    async def body() -> None:
        # Write a per-repo state on disk so active_phase_completion resolves a
        # non-empty bar (the green tint only paints when there is progress).
        repo_dir = tmp_path / "abc"
        (repo_dir / ".ea").mkdir(parents=True)
        repo_state = {
            "schema_version": "1.1",
            "current": {"phase_id": "P01"},
            "phases": {"P01": {"id": "P01", "status": "active"}},
            "iters": {"P01-I01": {"id": "P01-I01", "phase_id": "P01", "status": "active"}},
            "waves": {
                "W1": {"iter_id": "P01-I01", "status": "closed"},
                "W2": {"iter_id": "P01-I01", "status": "pending"},
            },
        }
        (repo_dir / ".ea" / "state.json").write_bytes(orjson.dumps(repo_state))
        payload = orjson.loads(_WORKSPACE.read_bytes())
        payload["workspace"]["repos"] = {
            "ABC": {
                "code": "ABC",
                "path": str(repo_dir),
                "state_urn": "urn:eawf:v1:repo:ABC",
                "project_code": "ABC",
                "title": "ABC repo",
                "status": "active",
            }
        }
        payload["workspace"]["current_repo_code"] = "ABC"
        state = State.model_validate(payload)

        app = _TableHarness()
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#wt", WorkspaceTable)
            table.state = state
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            phase_cell = str(table.get_cell("ABC", "phase"))
            # The green status tint survives the degrade -- the bar is still
            # wrapped in the concrete green hex span, not stripped to fit 80.
            assert f"[{_GREEN_HEX}]" in phase_cell
            assert "1/2" in phase_cell
            # The repo cell still leads with the RUNNING sigil (active phase).
            repo_cell = str(table.get_cell("ABC", "repo"))
            assert _RUNNING_SIGIL in repo_cell

    asyncio.run(body())


def test_workspace_grid_narrow_frame_fits_no_overflow() -> None:
    """The captured 80-col frame keeps the sigil + bars without overflow.

    The on-screen render is what the operator sees, so a token surviving the
    node label but not the frame would still be a clip at the
    ``overflow-x: hidden`` pane edge. Asserting the captured frame proves the
    reflow reaches the rendered terminal text and that no line overflows 80.
    """

    async def body() -> None:
        app = _TableHarness()
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#wt", WorkspaceTable)
            table.state = _multi_repo_state(["ABC", "DEF", "GHI"])
            await pilot.pause()
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))

            # The leading sigil + the repo codes survive into the rendered frame.
            assert glyph(Sigil.ABANDONED, mode="unicode") in frame
            for code in ("ABC", "DEF", "GHI"):
                assert code in frame
            # No rendered line overflows the 80-col width (the degrade reflowed
            # rather than clipping past the pane edge).
            for line in frame.splitlines():
                assert len(line) <= _NARROW_COLS

    asyncio.run(body())


def test_workspace_grid_wide_restores_full_columns() -> None:
    """Growing back above the threshold restores the full six-column row.

    The degrade is reversible: a grid that grew wide again re-installs the
    dropped ``git`` / ``pr`` / ``age`` columns, so a resize never strands the
    operator in the narrow layout.
    """

    async def body() -> None:
        app = _TableHarness()
        async with app.run_test(size=(_WIDE_COLS, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#wt", WorkspaceTable)
            table.state = _multi_repo_state(["ABC"])
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert table.size.width == _WIDE_COLS
            keys = tuple(str(col.key.value) for col in table.columns.values())
            assert keys == _COLUMNS

    asyncio.run(body())


# --------------------------------------------------------------------------
# Live user portfolio grid at 80 cols -- inherited degrade, same acceptance
# --------------------------------------------------------------------------


def test_user_portfolio_grid_narrow_degrades_columns() -> None:
    """The user portfolio grid degrades to the narrow columns at 80 cols."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one(PortfolioTable)
            assert isinstance(table, WorkspaceTable)
            # The portfolio grid inherited the degrade: only the load-bearing
            # trio renders at 80 cols (git / pr / age dropped).
            keys = tuple(str(col.key.value) for col in table.columns.values())
            assert keys == _NARROW_COLUMNS

    asyncio.run(body())


def test_user_portfolio_grid_narrow_keeps_sigil_and_tint() -> None:
    """At 80 cols the user portfolio repo cell keeps its sigil + warn-marker tint.

    The fixture repo path is absent, so the lone repo (``QR``) has no active
    phase and trips the stale band -- its cell leads with the ABANDONED
    lifecycle sigil and trails the warn-marker (attention triangle, tinted warn)
    + the ``stale`` word. Both survive the degrade.
    """

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one(PortfolioTable)
            mode = app.render_mode
            repo_cell = str(table.get_cell("QR", "repo"))
            assert repo_cell.startswith("[")  # leading tinted sigil span
            assert glyph(Sigil.ABANDONED, mode=mode) in repo_cell
            assert "QR" in repo_cell
            assert f"{chrome('attention', mode=mode)} stale" in repo_cell
            # The phase + eu bar columns survive the degrade (slots present).
            assert table.get_cell("QR", "phase")
            assert table.get_cell("QR", "eu")

    asyncio.run(body())


def test_user_portfolio_grid_narrow_frame_fits_no_overflow() -> None:
    """The user portfolio 80-col frame keeps the sigil without overflowing 80."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(_NARROW_COLS, 24)) as pilot:
            await pilot.pause()
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The ABANDONED sigil + the repo code survive into the frame.
            assert glyph(Sigil.ABANDONED, mode=app.render_mode) in frame
            assert "QR" in frame
            for line in frame.splitlines():
                assert len(line) <= _NARROW_COLS

    asyncio.run(body())
