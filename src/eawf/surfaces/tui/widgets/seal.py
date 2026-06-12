"""Render the Eä Seal as a deterministic half-block ASCII-art brand mark.

The Seal is the project's brand mark. Rather than a terminal graphics protocol
(which needs a graphics-capable terminal, leaks a probe-reply into the live
stdin reader, and renders non-deterministically across machines), the hero
surfaces draw the Seal as a hand-tuned half-block TEXT block in the theme
accent. The art is theme-portable (correct on both the light and dark themes
from one ``$accent`` colour), centers like any other text, and renders
identically in CI, a pipe, and a live terminal -- so the goldens stay stable.
"""

from __future__ import annotations

import logging

from textual.widgets import Static

logger = logging.getLogger(__name__)

#: The hand-tuned half-block ASCII-art Seal -- 19 rows x 42 cols of ``▀▄█`` +
#: spaces, 4-fold symmetric with a continuous outer green ring + a continuous
#: white separator ring, copied verbatim from the operator-approved
#: ``.ea/local/dispatch/seal42_lines.txt`` (each row padded to the full 42 so
#: the disc centers within the block). It is the deterministic TEXT brand mark
#: the hero surfaces render: drawn with the half-block glyphs in the accent
#: colour and the empty halves / spaces left UNstyled, the cell's own
#: ``$surface`` background shows through. That makes it theme-portable (correct
#: on BOTH the light and dark themes) on one colour (``$accent``) with no
#: graphics protocol -- no Kitty transmit lag, no terminal probe-reply leak, no
#: alpha matte, and it centers like any other text.
SEAL_ART_LINES: tuple[str, ...] = (
    "              ▄▄▄▄██████▄▄▄▄              ",
    "         ▄▄███▀▀▀▀      ▀▀▀▀███▄▄         ",
    "       ▄██▀▀   ▄▄▄▄████▄▄▄▄   ▀▀██▄       ",
    "     ▄██▀  ▄▄███████▀▀███████▄▄  ▀██▄     ",
    "   ▄██▀  ▄██████████  ██████████▄  ▀██▄   ",
    "  ▄█▀  ▄███████████▀  ▀███████████▄  ▀█▄  ",
    " ▄█▀  ▄███▀▀▀▀█████    █████▀▀▀▀███▄  ▀█▄ ",
    " ██  ▄█████▄                  ▄█████▄  ██ ",
    "▄██  ████████▄     ▄▄▄▄     ▄████████  ██▄",
    "██   ██████████    ████    ██████████   ██",
    "▀██  ████████▀     ▀▀▀▀     ▀████████  ██▀",
    " ██  ▀█████▀                  ▀█████▀  ██ ",
    " ▀█▄  ▀███▄▄▄▄█████    █████▄▄▄▄███▀  ▄█▀ ",
    "  ▀█▄  ▀███████████▄  ▄███████████▀  ▄█▀  ",
    "   ▀██▄  ▀██████████  ██████████▀  ▄██▀   ",
    "     ▀██▄  ▀▀███████▄▄███████▀▀  ▄██▀     ",
    "       ▀██▄▄   ▀▀▀▀████▀▀▀▀   ▄▄██▀       ",
    "         ▀▀███▄▄▄▄      ▄▄▄▄███▀▀         ",
    "              ▀▀▀▀██████▀▀▀▀              ",
)

#: Stable id for the art-seal :class:`~textual.widgets.Static` so a hero
#: stylesheet can size + center its box.
SEAL_ART_ID: str = "seal-art"

#: Stable class on the art-seal Static, shared with the legacy
#: ``.research-empty-seal`` hero CSS hook so the art's box keeps the centered
#: ~width-42 / height-19 layout the heroes rely on.
SEAL_ART_CLASS: str = "research-empty-seal"


def seal_art_markup() -> str:
    """Return the ASCII-art Seal as Textual content-markup in the theme accent.

    Joins :data:`SEAL_ART_LINES` with newlines and wraps the whole block in a
    single ``[$accent]...[/]`` span, so the half-block glyphs render in the
    theme accent while the empty halves / spaces stay UNstyled and the cell's
    own ``$surface`` background shows through. Wrapping on the ``$accent`` token
    rather than a fixed hex keeps the mark theme-portable: it is correct on BOTH
    the light and dark themes from one colour, with no graphics protocol.

    The art is pure ``▀▄█`` + spaces (no ``[``/``]`` content), so there is no
    markup-escaping concern -- the only brackets in the returned string are the
    accent span this function adds.

    Returns:
        The accent-wrapped 19-row art block as a Textual content-markup string.
    """
    block = "\n".join(SEAL_ART_LINES)
    return f"[$accent]{block}[/]"


def seal_art_widget() -> Static:
    """Return a :class:`textual.widgets.Static` rendering the ASCII-art Seal.

    The deterministic TEXT brand mark the hero surfaces mount. The Static
    carries the :data:`SEAL_ART_ID` id + the :data:`SEAL_ART_CLASS` class so a
    hero stylesheet can size + center its box (~width 31, height 14). It never
    returns ``None`` and never touches a graphics protocol: the art renders
    identically in CI, a pipe, and a live terminal.

    Returns:
        The art Static, ready to mount.
    """
    return Static(seal_art_markup(), id=SEAL_ART_ID, classes=SEAL_ART_CLASS)


__all__ = [
    "SEAL_ART_CLASS",
    "SEAL_ART_ID",
    "SEAL_ART_LINES",
    "seal_art_markup",
    "seal_art_widget",
]
