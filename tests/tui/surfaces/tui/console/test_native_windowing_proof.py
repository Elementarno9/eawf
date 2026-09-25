"""The windowing journey reds when the window is taken away.

The journey in :mod:`test_native_windowing` passes only if it can fail: with every native
frame drawing its whole table again, the fifty-press walk must lose its caret off the
foot of the screen and say so. The bypass stands in for the frames as they were before
windowing, a window as tall as the table.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.frame import RowWindow, View
from eawf.surfaces.tui.console.renderers import read_model, registers, spine
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console.test_native_windowing import FAMILIES, journey


def _whole_table(view: View, *, total: int, cursor: int, chrome: int) -> RowWindow:
    """Return a window over the whole table, however tall the frame is."""
    view.session.visible = total
    return RowWindow(start=0, stop=total, total=total)


@pytest.fixture
def unwindowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Draw every native table whole, as the frames did before they were windowed."""
    for module in (registers, spine, read_model):
        monkeypatch.setattr(module, "window_rows", _whole_table)


@pytest.mark.parametrize("family", FAMILIES)
def test_the_journey_reds_on_an_off_screen_cursor_without_windowing(
    family: str, unwindowed: None
) -> None:
    """With the window bypassed, the walk loses the caret before its fiftieth press."""
    view = FAMILIES[family](Session())
    with pytest.raises(AssertionError, match="off screen"):
        journey(view)


@pytest.mark.parametrize("family", FAMILIES)
def test_the_journey_passes_with_windowing(family: str) -> None:
    """The same walk, windowed, keeps the caret; the red above is the bypass's alone."""
    view = FAMILIES[family](Session())
    journey(view)
    assert view.session.sel == 50
