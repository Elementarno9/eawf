"""The row widgets the console paints its frame into: header, body and keybar.

Each widget paints precomposed rows verbatim, one strip per line, and repaints only the
rows a frame changed, so a keystroke costs what it changes rather than the whole screen.
"""

from __future__ import annotations

import functools
from typing import ClassVar

from rich.segment import Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget

from eawf.surfaces.tui.console.paint import Part, Stroke, paint
from eawf.surfaces.tui.console.token_map import SURFACES, TOKEN_MAP

# The style-metadata key a painted span's mark travels under.
MARK_META = "mark"


def row_classes(*surfaces: str) -> str:
    """Return the stylesheet classes that give a widget these surfaces' colours."""
    return " ".join(SURFACES[surface].css_class for surface in surfaces)


@functools.lru_cache(maxsize=4096)
def _plain_strokes(row: str, part: Part) -> tuple[Stroke, ...]:
    """Return a plain row's runs; a scrolled window repaints rows it has painted before."""
    return paint(row, part)


class RowsWidget(Widget):
    """A widget that paints precomposed rows verbatim, one strip per line.

    A row is never wrapped and never parsed as markup, and nothing here takes focus: the
    app dispatches every key itself. Each run of a row is drawn as the surface the row
    painter reads off its words, in the colour the token map's stylesheet gives that
    surface under the active theme.
    """

    can_focus = False
    DEFAULT_CSS = """
    RowsWidget { padding: 0; margin: 0; border: none; }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = {row.css_class for row in TOKEN_MAP}
    ROW_STYLE = Style()
    PART: ClassVar[Part] = Part.BODY

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: list[str] = []
        self._styles: dict[tuple[object, ...], Style] = {}
        self._bases: dict[tuple[str, frozenset[str]], Style] = {}

    def set_rows(self, rows: list[str]) -> None:
        """Replace the rows and repaint the ones that changed.

        A key usually moves a caret across two rows, so repainting only changed rows
        keeps a keystroke's paint cost flat however many rows the frame holds. A row's
        type is part of what changed: a receded or disabled row paints differently from
        a plain one with the same text.
        """
        old, self.rows = self.rows, rows
        if len(old) != len(rows):
            self.refresh()
            return
        width = self.size.width
        changed = [
            Region(0, y, width, 1)
            for y, (before, after) in enumerate(zip(old, rows, strict=True))
            if before != after or type(before) is not type(after)
        ]
        if changed:
            self.refresh(*changed)

    def render_line(self, y: int) -> Strip:
        """Return row ``y`` as one strip of the widget's width, one segment per span.

        A marked span carries its mark in the segment's style metadata under
        :data:`MARK_META`, so the theme that colours a token reads what the cell is rather
        than guessing it from the glyph.
        """
        text = self.rows[y] if y < len(self.rows) else ""
        theme_key = (self.app.theme, frozenset(self.classes))
        base = self._bases.get(theme_key)
        if base is None:
            base = self._bases[theme_key] = self.rich_style + self.ROW_STYLE
        # A subclassed row carries more than its text, so only a plain row is cached.
        strokes = _plain_strokes(text, self.PART) if type(text) is str else paint(text, self.PART)
        segments = [Segment(stroke.text, self._stroke_style(base, stroke)) for stroke in strokes]
        return Strip(segments).adjust_cell_length(self.size.width)

    def _stroke_style(self, base: Style, stroke: Stroke) -> Style:
        """Return the style one run is drawn in: the band's, then its surfaces over it.

        The component styles follow the theme, so the theme is part of the cache key;
        a frame repeats a handful of stroke kinds across every row it paints.
        """
        key = (
            self.app.theme,
            base,
            stroke.ground,
            stroke.surface,
            stroke.bold,
            stroke.underline,
            stroke.mark,
        )
        cached = self._styles.get(key)
        if cached is None:
            cached = self._styles[key] = self._composed_style(base, stroke)
        return cached

    def _composed_style(self, base: Style, stroke: Stroke) -> Style:
        style = base
        for surface in (stroke.ground, stroke.surface):
            if surface is not None:
                style += self.get_component_rich_style(SURFACES[surface].css_class, partial=True)
        if stroke.bold or stroke.underline:
            style += Style(bold=stroke.bold or None, underline=stroke.underline or None)
        if stroke.mark is not None:
            style += Style(meta={MARK_META: stroke.mark.value})
        return style
