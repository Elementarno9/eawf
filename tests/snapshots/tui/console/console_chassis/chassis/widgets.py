"""Four widgets, each rendering precomposed rows through ``render_line`` so a row is never
wrapped, never markup-parsed and always exactly W cells. Nothing is focusable: the App
dispatches every key itself."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget


class RowsWidget(Widget):
    """A widget that paints ``rows`` verbatim, one Strip per line."""

    can_focus = False
    DEFAULT_CSS = """
    RowsWidget { padding: 0; margin: 0; border: none; }
    """

    def __init__(self, rows: list[str] | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: list[str] = rows or []
        self.row_style: Style = Style()

    def set_rows(self, rows: list[str]) -> None:
        self.rows = rows
        self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        text = self.rows[y] if y < len(self.rows) else ""
        return Strip([Segment(text, self.row_style)]).adjust_cell_length(width)


class ProjectionHeader(RowsWidget):
    DEFAULT_CSS = """
    ProjectionHeader { height: 1; dock: top; }
    """


class Body(RowsWidget):
    DEFAULT_CSS = """
    Body { height: 1fr; }
    """


class Keybar(RowsWidget):
    DEFAULT_CSS = """
    Keybar { height: 1; dock: bottom; }
    """

    def __init__(self, rows: list[str] | None = None, *, id: str | None = None) -> None:
        super().__init__(rows, id=id)
        self.row_style = Style(bold=True)
