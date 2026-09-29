"""The painter's legends: a key to a frame's glyphs is drawn plain, never as keys or heads."""

from __future__ import annotations

from eawf.surfaces.tui.console.frame import Titled
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.renderers.run_detail import TIMELINE_LEGEND, timeline_head

_LEGEND = "● dated  ○ forecast  │ now  ▣ release"


# ---------- the timeline pane's head ----------


def test_the_timeline_title_is_bold_and_its_legend_plain() -> None:
    strokes = {s.text.strip(): s for s in paint(timeline_head(160), Part.BODY) if s.text.strip()}
    assert strokes["TIMELINE"].bold
    assert (strokes[TIMELINE_LEGEND].surface, strokes[TIMELINE_LEGEND].bold) == (None, False)


def test_the_legend_sits_against_the_right_edge_one_cell_in() -> None:
    for w in (80, 120, 160):
        head = timeline_head(w)
        assert isinstance(head, Titled)
        assert len(head) == w
        assert head.endswith(f"{TIMELINE_LEGEND} ")
        assert head.startswith(" TIMELINE ")


def test_an_untyped_row_of_the_same_words_still_reads_as_heads() -> None:
    row = " TIMELINE          P0  P1  P2"
    assert [s.bold for s in paint(row, Part.BODY) if s.text.strip()] == [True]


# ---------- the keybar legend ----------


def test_a_legend_past_the_keys_is_one_faint_run() -> None:
    row = f"Tab section   . actions   Esc back{' ' * 40}{_LEGEND} "
    strokes = [s for s in paint(row, Part.KEYBAR) if s.text.strip()]
    legend = next(s for s in strokes if "dated" in s.text)
    assert (legend.text, legend.surface, legend.bold) == (_LEGEND, "dim", False)
    assert [s.text for s in strokes if s.bold] == ["Tab", ".", "Esc"]


def test_a_keybar_with_no_legend_keeps_its_key_grammar() -> None:
    row = "↑↓ row   Enter drill   Esc back" + " " * 40
    strokes = [(s.text, s.surface, s.bold) for s in paint(row, Part.KEYBAR) if s.text.strip()]
    assert ("↑↓", None, True) in strokes
    assert ("back", "hint", False) in strokes
    assert all(surface != "dim" for _t, surface, _b in strokes)
