"""Active-mode highlight in the help overlay (P29-I13-W28).

The :class:`~eawf.surfaces.tui.screens.help.HelpScreen` "Modes (digit keys)"
section marks the row of the mode the operator is currently in: that row
carries a ``>`` cursor + an ``(active)`` tag and the ``help-row-active``
style class, so the help reflects the live mode at a glance.

This module pins the wave's gate: with a non-default mode active, the help
overlay's snapshot shows the active mode row carrying the highlight marker
(and only that row). The pure
:func:`~eawf.surfaces.tui.screens.help.mode_key_rows_active` helper is
unit-tested without a Textual mount; the rendered highlight is pinned by a
golden snapshot captured with Trust mode active.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_help_active_mode.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import VerticalScroll

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.help import mode_key_rows_active
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


# --------------------------------------------------------------------------
# Pure helper -- no Textual mount
# --------------------------------------------------------------------------


def test_mode_key_rows_active_marks_only_current_mode() -> None:
    """Exactly the current mode's row is tagged active."""
    rows = mode_key_rows_active("trust")
    active = [action for _, action, is_active in rows if is_active]
    assert len(active) == 1
    assert "Trust" in active[0]


def test_mode_key_rows_active_home_default() -> None:
    """boundary: the launch-default Home mode tags its own row active."""
    rows = mode_key_rows_active("home")
    active = [action for _, action, is_active in rows if is_active]
    assert active == ["switch to Home mode"]


def test_mode_key_rows_active_unknown_marks_none() -> None:
    """boundary: an unknown / unresolved mode tags no row active."""
    rows = mode_key_rows_active("no-such-mode")
    assert not any(is_active for _, _, is_active in rows)


def test_mode_key_rows_active_empty_marks_none() -> None:
    """boundary: an empty mode name (bare harness) tags no row active."""
    rows = mode_key_rows_active("")
    assert not any(is_active for _, _, is_active in rows)


# --------------------------------------------------------------------------
# Gate: the help overlay shows the active mode row with the highlight marker
# --------------------------------------------------------------------------


def test_help_active_mode_highlight_snapshot() -> None:
    """With Trust active, the help overlay marks the Trust mode row active."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust mode
            await settle_screen(pilot)
            app.action_open_help()
            await settle_screen(pilot)
            # Zero the help card's vertical scrollbar width so the capture
            # asserts the help content, not the overflowing card's sub-cell
            # scrollbar-thumb glyph -- that eighth-block glyph tracks the help
            # CONTENT height (not the fixture state), so it would drift this
            # golden on any unrelated keymap edit. Production keeps its
            # scrollbar; only the snapshot capture drops it.
            help_card = app.screen.query_one("#help-container", VerticalScroll)
            help_card.styles.scrollbar_size_vertical = 0
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The active Trust row carries the cursor + active tag; the inactive
            # Home row keeps the plain two-space indent. The cursor is the
            # chrome "dispatch" glyph (unicode column in the default mode).
            assert "\u276f 4          switch to Trust mode (active)" in frame
            assert "  1          switch to Home mode" in frame
            assert "(active)" in frame
            # Only one row is marked active.
            assert frame.count("(active)") == 1
            assert_screen_snapshot(app, _GOLDEN / "help_active_mode.txt")

    asyncio.run(body())
