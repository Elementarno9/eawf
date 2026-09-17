"""Plain mode: the frame renderer's rows in the ASCII allocation, one row per line.

The plain-text artifact is the console's own frame, not a second layout: the same H rows
of W cells, every glyph swapped for its one-cell ASCII twin, chip markers dropped, and no
escape sequence, colour or wrapping. A non-interactive caller, a screen reader and an
export therefore read exactly what the frame says, in the order it says it. Plain mode
renders under the offline-snapshot connection value unless the caller asks for another.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, strip_chips
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import CONNECTION, QUALITY, TRUTH, Glyph
from eawf.surfaces.tui.console.width import cell_len

OFFLINE_SNAPSHOT = "OFFLINE SNAPSHOT"
ESCAPE = "\x1b"

# The frame glyphs beside the value tokens, each with its one-cell ASCII twin.
_FRAME_TWINS: Mapping[str, str] = {
    "°": "o",
    "±": "+",
    "¶": "P",
    "·": ".",
    "»": ">",
    "×": "x",  # noqa: RUF001
    "ä": "a",
    "–": "-",  # noqa: RUF001
    "—": "-",
    "’": "'",  # noqa: RUF001
    "“": '"',
    "”": '"',
    "•": "*",
    "…": ".",
    "‹": "<",  # noqa: RUF001
    "›": ">",  # noqa: RUF001
    "←": "<",
    "↑": "^",
    "→": ">",
    "↓": "v",
    "↳": ">",
    "−": "-",  # noqa: RUF001
    "≠": "!",
    "≤": "<",
    "≥": ">",
    "⋯": ".",
    "─": "-",
    "│": "|",
    "┄": "-",
    "┊": ":",
    "┌": "+",
    "┐": "+",
    "└": "+",
    "┘": "+",
    "├": "+",
    "┤": "+",
    "┬": "+",
    "┴": "+",
    "┼": "+",
    "═": "=",
    "█": "#",
    "▏": "|",
    "▣": "#",
    "▸": ">",
    "▾": "v",
    "○": "o",
    "✓": "v",
}


def _token_twins(*tables: Mapping[str, Glyph]) -> dict[str, str]:
    """Return the single-glyph value tokens' ASCII twins, one character each."""
    return {
        glyph.unicode: glyph.ascii
        for table in tables
        for glyph in table.values()
        if len(glyph.unicode) == 1 and not glyph.unicode.isascii()
    }


def _merge(frame: Mapping[str, str], tokens: Mapping[str, str]) -> Mapping[str, str]:
    """Return one twin table, refusing a glyph whose twins disagree or take two cells.

    Raises:
        ValueError: a glyph has two different twins, or a twin is not one ASCII cell.
    """
    clashes = sorted(g for g in frame.keys() & tokens.keys() if frame[g] != tokens[g])
    if clashes:
        raise ValueError(f"glyphs with two ASCII twins: {', '.join(clashes)}")
    merged = {**frame, **tokens}
    wide = sorted(g for g, twin in merged.items() if not (twin.isascii() and cell_len(twin) == 1))
    if wide:
        raise ValueError(f"ASCII twins that are not one ASCII cell: {', '.join(wide)}")
    return MappingProxyType(merged)


ASCII_TWINS: Mapping[str, str] = _merge(_FRAME_TWINS, _token_twins(CONNECTION, TRUTH, QUALITY))
_TABLE = str.maketrans(dict(ASCII_TWINS))


def ascii_twin(text: str) -> str:
    """Return ``text`` in the ASCII allocation, cell for cell.

    Raises:
        ValueError: ``text`` carries a glyph with no ASCII twin.
    """
    twin = text.translate(_TABLE)
    missing = sorted({ch for ch in twin if not ch.isascii()})
    if missing:
        raise ValueError(f"glyphs with no ASCII twin: {', '.join(missing)}")
    return twin


def plain_rows(rows: Sequence[str]) -> list[str]:
    """Return frame rows as plain rows: markers dropped, glyphs twinned, widths kept.

    Raises:
        ValueError: a row carries an escape sequence or a glyph with no twin.
    """
    out: list[str] = []
    for i, row in enumerate(rows):
        if ESCAPE in row:
            raise ValueError(f"row {i} carries an escape sequence")
        out.append(ascii_twin(strip_chips(row)))
    return out


def plain_text(rows: Sequence[str]) -> str:
    """Return plain rows written one row per line."""
    return "\n".join(plain_rows(rows))


class _Headless:
    """The host a plain render dispatches keys against: a held clock and no quit."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def quit(self) -> None:
        """Do nothing: a plain render has no session to end."""


def render_plain(
    fixture: Fixture,
    setup: SessionSetup,
    *,
    keys: Sequence[str] = (),
    conn: str = OFFLINE_SNAPSHOT,
    verbose: bool = False,
) -> list[str]:
    """Render one frame state in plain mode through the console's frame renderer.

    The rack is left empty: a toast is a timed notice the console sweeps off the frame
    after its dwell, and a plain artifact is read long after any of them stood.

    Args:
        fixture: The registers.
        setup: The reset argument; its size index fixes the frame size.
        keys: Keys pressed after the reset, each followed by a render as the app does.
        conn: The connection value to render under.
        verbose: Whether the trace row naming each key's handler is painted.

    Raises:
        ValueError: ``conn`` is not a connection value, or the frame is off its grid.
    """
    if conn not in CONNECTION:
        raise ValueError(f"{conn!r} is not a connection value")
    host = _Headless()
    session = Session()
    session.reset(
        setup.model_copy(update={"conn": conn}),
        settings_section_order=fixture.settings.section_order,
        now=host.clock.now(),
    )
    w, h = SIZES[session.size]
    view = View(session=session, fixture=fixture, w=w, h=h, held=True, verbose=verbose)
    rows = compose_frame(view)
    for key in keys:
        dispatch(Ctx(session=session, fixture=fixture, host=host, w=w, h=h, verbose=verbose), key)
        rows = compose_frame(view)
    return plain_rows(rows)
