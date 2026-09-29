"""A junction reads as the lines it joins, and the native lanes draw solid lines only.

Every tee, cross and corner takes the rail's tone, as the packet's settings join does, so
none is drawn in the text colour no line around it has. The native Timeline crosses each
lane with the plain keyline its week row draws and draws no dotted or cross glyph; a lane
crossing is not a pane edge, so the cursor lane is grounded whole.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.paint import Part, paint
from tests.tui.surfaces.tui.console.test_spine_frames_jury import _frame


@pytest.mark.parametrize("glyph", list("├┤┬┴┼╤╧╪┌┐└┘"))
def test_every_junction_takes_the_rail_tone(glyph: str) -> None:
    strokes = paint(f"   docs           {glyph}──────────────", Part.BODY)
    assert [s.surface for s in strokes if s.text == glyph] == ["rail"]


def test_the_native_lanes_draw_no_cross_or_dotted_glyph() -> None:
    frame = _frame("timeline", w=160)
    assert not [row for row in frame if set(row) & set("┼┄┊")]
    assert not any("uncertain" in row for row in frame)


def test_the_dependency_region_is_a_labelled_row() -> None:
    frame = _frame("timeline", w=160)
    assert any(row.startswith(" DEPENDS     ") for row in frame)


def test_the_cursor_lane_is_grounded_across_the_keyline() -> None:
    row = "▸TRK-CORE    ─────────────────│─────────────────" + " " * 40
    assert {stroke.ground for stroke in paint(row, Part.BODY)} == {"cursor"}


def test_a_rail_between_panes_still_grounds_only_the_caret_pane() -> None:
    row = "  agents     │ ▸ – approval          ask             built-in           "  # noqa: RUF001
    strokes = paint(row, Part.BODY)
    assert [s.ground for s in strokes if s.text.strip() == "agents"] == [None]
