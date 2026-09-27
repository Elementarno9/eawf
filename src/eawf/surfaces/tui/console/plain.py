"""Plain mode: the frame renderer's rows in the ASCII allocation, one row per line.

The plain-text artifact is the console's own frame, not a second layout: the same H rows
of W cells, every glyph swapped for its one-cell ASCII twin, chip markers dropped, and no
escape sequence, colour or wrapping. A non-interactive caller, a screen reader and an
export therefore read exactly what the frame says, in the order it says it. Plain mode
renders under the offline-snapshot connection value unless the caller asks for another.

Two things differ from a glyph-for-glyph copy, both because the twins carry less than the
glyphs. A keybar arrow pair twins to a word pair rather than a glyph, so the keybar is
re-laid around the words instead of aligned, unless the words would push a key off the
bar. And a frame that shows a truth token or a quality marker carries the token legend,
right-aligned on the keybar row where both fit and otherwise in the rows right above it:
without the marker set the tokens carry more of the meaning, so the frame spends its
blank rows and thin rules stating it, never a row that states a fact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.cells import spans
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, make_room, strip_chips
from eawf.surfaces.tui.console.keybar import GAP, MARGIN, budget
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import (
    CONNECTION,
    GLYPH_TABLE,
    PROSE_TWINS,
    QUALITY,
    TRUTH,
    ColumnClass,
    Glyph,
)
from eawf.surfaces.tui.console.width import cell_len, pad

OFFLINE_SNAPSHOT = "OFFLINE SNAPSHOT"
ESCAPE = "\x1b"

# Every table glyph with the twin it takes where its class is not read, then the prose
# punctuation the table leaves out.
_FRAME_TWINS: Mapping[str, str] = {
    **{row.glyph: row.twin for row in GLYPH_TABLE if not row.glyph.isascii()},
    **PROSE_TWINS,
}
# The one glyph whose twin depends on its class: a middle dot in the provenance column,
# drawn right after the cursor gutter and before a key, is the inherited marker.
_INHERITED = re.compile(r"(?:(?<=[▸ ] )|(?<=^ ))·(?= [a-z_])")
_INHERITED_TWIN = next(row for row in GLYPH_TABLE if row.glyph == "·").twins[ColumnClass.PROVENANCE]


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
    twin = _INHERITED.sub(_INHERITED_TWIN, text).translate(_TABLE)
    missing = sorted({ch for ch in twin if not ch.isascii()})
    if missing:
        raise ValueError(f"glyphs with no ASCII twin: {', '.join(missing)}")
    return twin


# The keybar tokens whose twin is a word rather than a glyph, longest run first.
_KEYBAR_WORDS: tuple[tuple[str, str], ...] = (
    ("↑↓", "up/dn"),
    ("←→", "left/right"),
    ("↑", "up"),
    ("↓", "dn"),
    ("←", "left"),
    ("→", "right"),
)

# What each truth token is read as in the legend; the zero is a value, not an absence.
_LEGEND_WORDS: Mapping[str, str] = {
    "unknown": "unknown",
    "unavailable": "unavailable",
    "denied": "denied",
    "purged": "purged",
    "invalidated": "invalidated",
    "zero": "a real zero",
}
_LEGEND_GUTTER = 14
_LEGEND_SEP = "  "


def _legend_parts() -> tuple[list[str], list[str]]:
    """Return the legend's truth-token entries, the genuine zero last, and its quality entries.

    The twins are read off the token tables, so the legend cannot name a twin the frame
    does not draw.
    """
    truth = [f"{TRUTH[name].ascii} {word}" for name, word in _LEGEND_WORDS.items()]
    quality = [
        f"{glyph.ascii} {name}" if glyph.ascii else f"bare {name}"
        for name, glyph in QUALITY.items()
    ]
    return truth, quality


def legend_line() -> str:
    """Return the whole legend as the one line it reads as where a row can hold it."""
    truth, quality = _legend_parts()
    return f"TRUTH TOKENS {' '.join(truth)} / QUALITY {' '.join(quality)}"


def _legend_rows(w: int) -> list[str]:
    """Return the legend as the rows it takes above a ``w``-cell keybar.

    One row where the whole line fits between the margins; otherwise two, the five
    absences first and the genuine zero leading the quality row, which fits 80 columns.
    """
    line = legend_line()
    if 2 * MARGIN + cell_len(line) <= w:
        return [pad(" " * MARGIN + line, w)]
    truth, quality = _legend_parts()
    rows = [
        pad(" TRUTH TOKENS", _LEGEND_GUTTER) + _LEGEND_SEP.join(truth[:-1]),
        pad("", _LEGEND_GUTTER) + _LEGEND_SEP.join([truth[-1], "QUALITY", *quality]),
    ]
    return [pad(row, w) for row in rows]


def _shared_keybar(keys: str) -> str | None:
    """Return the keybar with the legend right-aligned on it, or ``None`` when both do not fit.

    Sharing needs the keys, a gap and the whole legend inside the margins, so the legend
    never displaces a bound key.
    """
    w = cell_len(keys)
    bar = keys.rstrip()
    line = legend_line()
    if cell_len(bar) + len(GAP) + cell_len(line) + MARGIN > w:
        return None
    return pad(bar, w - MARGIN - cell_len(line)) + line + " " * MARGIN


def _shows_a_token(rows: Sequence[str]) -> bool:
    """Return whether any row draws a truth token beside its word or a quality marker."""
    return any(span.mark is not None for row in rows for span in spans(row))


def _with_legend(body: Sequence[str], keys: str) -> list[str]:
    """Return the plain frame with the token legend on the keybar row or right above it.

    The legend shares the keybar row where both fit; otherwise it takes the rows directly
    above the keybar, which the frame composer frees from blank rows and thin rules only.
    A frame with too few such rows goes without the legend rather than losing a fact.

    Args:
        body: The frame rows above the keybar, not yet twinned.
        keys: The keybar row, already re-laid and twinned.
    """
    shared = _shared_keybar(keys)
    if shared is not None:
        return [*(ascii_twin(row) for row in body), shared]
    legend = _legend_rows(cell_len(keys))
    freed = make_room(body, len(legend))
    if freed is None:
        return [*(ascii_twin(row) for row in body), keys]
    return [*(ascii_twin(row) for row in freed), *legend, keys]


def _relaid_keybar(bar: str) -> str:
    """Return the keybar with each arrow token twinned to its word pair and the bar re-laid.

    A bar with no arrow token, or one whose word pairs would push a bound key off the
    row, is returned unchanged and twinned glyph for glyph instead, so no key is lost.
    """
    pieces = [piece.strip() for piece in bar[MARGIN:].split(GAP) if piece.strip()]
    words: list[str] = []
    for piece in pieces:
        token, _, label = piece.partition(" ")
        for glyphs, word in _KEYBAR_WORDS:
            token = token.replace(glyphs, word)
        words.append(f"{token} {label}".rstrip())
    relaid = " " * MARGIN + GAP.join(words)
    w = cell_len(bar)
    if words == pieces or cell_len(relaid) > budget(w):
        return bar
    return pad(relaid, w)


def plain_rows(rows: Sequence[str]) -> list[str]:
    """Return frame rows as plain rows: markers dropped, glyphs twinned, widths kept.

    The last row is the keybar, re-laid where an arrow pair twins to words, and a frame
    that draws a truth token or a quality marker carries the token legend on the keybar
    row or directly above it.

    Raises:
        ValueError: a row carries an escape sequence or a glyph with no twin.
    """
    out: list[str] = []
    for i, row in enumerate(rows):
        if ESCAPE in row:
            raise ValueError(f"row {i} carries an escape sequence")
        out.append(strip_chips(row))
    if not out:
        return out
    body, keys = out[:-1], ascii_twin(_relaid_keybar(out[-1]))
    if _shows_a_token(out):
        return _with_legend(body, keys)
    return [*(ascii_twin(row) for row in body), keys]


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
