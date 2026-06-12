"""Render the Eä Seal as a terminal image (graphics-protocol brand mark).

Textual draws cells, but a graphics-capable terminal (Ghostty / Kitty / iTerm2
/ WezTerm via the Kitty graphics protocol, or sixel, with a unicode-halfcell
fallback) can display an inline raster image. This module wires the real Seal
SVG into the TUI: :func:`~eawf.surfaces.render` ``assets/ea-seal.svg`` is
rasterised to a PNG via the ``resvg`` CLI (already a repo dependency for the
``svg_pixel_diff`` audit kind), and :class:`textual_image.widget.Image` embeds
it through whatever protocol the terminal supports.

The asset fills with ``currentColor`` so it inherits the surrounding text
colour in a live document, but ``resvg`` rasterises with no document context
and so resolves ``currentColor`` to **black** -- the seal would render as a
black blob on a dark theme. The rasterise path therefore substitutes the
resolved theme accent hex into the SVG text BEFORE invoking ``resvg``, and the
PNG cache keys on ``(px, accent_hex, bg_hex)`` (overwriting any stale PNG) so a
pre-existing black PNG from before the fix can never survive into the render.

The asset is also a fisheye disc with a star *punched out* (an ``evenodd``
knockout), so the corners around the disc and the star cut-out are
**transparent**. A terminal graphics protocol composites those transparent
regions onto the cell's own background -- on Ghostty that paints **black**, so
the seal reads as a green disc inside a black square. The rasterise therefore
mattes the PNG onto the resolved theme surface hex via ``resvg --background``:
the transparent regions are filled with the surface colour, so they blend into
the live TUI surface the seal sits on (``Screen { background: $surface }``)
rather than rendering black. The surface hex is part of the cache key alongside
the accent so a stale fully-transparent PNG cannot survive into the render.

The capability **degrades cleanly**. ``textual-image`` + Pillow now ship in the
default dependency set, so the import is no longer an invisible-extra trap: a
default install can render the seal whenever the host terminal supports a
graphics protocol AND ``resvg`` is on PATH. When any precondition is absent --
including a CI / snapshot run, which has no graphics-capable terminal -- callers
fall back to the unicode ``◉`` brand glyph. The :data:`SEAL_DISABLE_ENV` kill
switch forces the capability ``False`` regardless; the snapshot harness sets it
autouse so the goldens stay glyph-based and deterministic.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.surfaces.render.brand import ACCENT_HEX

if TYPE_CHECKING:
    from textual.widget import Widget

logger = logging.getLogger(__name__)

#: The Seal SVG asset (``currentColor`` fill, so it inherits the text colour).
#: ``seal.py`` lives in ``surfaces/tui/widgets/``; the asset in
#: ``surfaces/render/assets/`` -- two parents up from ``widgets`` to
#: ``surfaces``, then into ``render/assets``.
_SEAL_SVG: Path = Path(__file__).resolve().parents[2] / "render" / "assets" / "ea-seal.svg"

#: The SVG colour token the asset fills with so it inherits the surrounding text
#: colour in a live document. ``resvg`` rasterises with no surrounding document
#: context, so it resolves ``currentColor`` to BLACK -- the live-render defect
#: this module forecloses by substituting the resolved theme accent hex into the
#: SVG text BEFORE invoking ``resvg``.
_CURRENT_COLOR: str = "currentColor"

#: The ``resvg`` CLI binary name (the same renderer the ``svg_pixel_diff`` audit
#: kind pins). Absent on most machines -- :func:`resvg_present` checks for it.
_RESVG: str = "resvg"

#: The main content surface background hex the seal is matted onto. It is the
#: Wong dark theme's ``$surface`` (the light surface behind the hero seal, which
#: ``Screen { background: $surface }`` paints), so the seal's transparent
#: corners + star knockout blend into the live TUI surface instead of the black
#: a graphics terminal composites a transparent region onto. Single-homed here
#: the same way :func:`resolve_surface_hex` mirrors :func:`resolve_accent_hex`:
#: a future per-theme surface only has to feed the one resolver.
SURFACE_HEX: str = "#1E1E1E"

#: Kill switch. When set to any non-empty value, :func:`seal_capable` returns
#: ``False`` unconditionally so the image path is never taken. The snapshot
#: harness sets it autouse so CI goldens stay glyph-based and deterministic.
SEAL_DISABLE_ENV: str = "EAWF_SEAL_DISABLE"

#: ``TERM_PROGRAM`` values whose terminals support an inline graphics protocol
#: (Kitty graphics protocol or sixel). Detection is non-interactive on purpose:
#: a capability probe must never write an escape query to the live terminal.
_GRAPHICS_TERM_PROGRAMS: frozenset[str] = frozenset(
    {"ghostty", "iterm.app", "wezterm", "vscode", "kitty"}
)


def deps_present() -> bool:
    """Return whether the ``textual_image`` import (and thus Pillow) resolves.

    ``textual-image`` + Pillow ship in the default dependency set, so this is
    normally ``True``; it only fails on a broken / partial install.

    Returns:
        ``True`` when ``textual_image.widget`` imports, else ``False``.
    """
    try:
        import textual_image.widget  # noqa: F401  (capability probe only)
    except ImportError:
        return False
    return True


def resvg_present() -> bool:
    """Return whether the ``resvg`` rasteriser is on PATH and the asset exists.

    Returns:
        ``True`` when ``resvg`` resolves on PATH AND the Seal SVG asset is on
        disk, else ``False``.
    """
    return shutil.which(_RESVG) is not None and _SEAL_SVG.exists()


def terminal_supports_images() -> bool:
    """Return whether the host terminal advertises an inline graphics protocol.

    Detection is **non-interactive** -- it reads environment variables only and
    never writes an escape-sequence query, so it is safe to call from a doctor
    check or a capability probe. A terminal qualifies when stdout is a TTY AND
    one of:

    - ``KITTY_WINDOW_ID`` is set (Kitty graphics protocol);
    - ``TERM`` contains ``kitty`` (Kitty / Ghostty terminfo);
    - ``TERM_PROGRAM`` is a known graphics-capable terminal
      (:data:`_GRAPHICS_TERM_PROGRAMS`).

    A non-TTY stdout (pipe / CI / snapshot harness) is never image-capable.

    Returns:
        ``True`` when the terminal advertises a graphics protocol, else
        ``False``.
    """
    stdout = sys.__stdout__
    if stdout is None or not stdout.isatty():
        return False
    if os.environ.get("KITTY_WINDOW_ID"):
        return True
    if "kitty" in os.environ.get("TERM", "").lower():
        return True
    return os.environ.get("TERM_PROGRAM", "").lower() in _GRAPHICS_TERM_PROGRAMS


@lru_cache(maxsize=1)
def seal_capable() -> bool:
    """Return whether the TUI can render the Seal as an inline image.

    ``True`` only when the :data:`SEAL_DISABLE_ENV` kill switch is unset AND the
    ``textual_image`` import resolves (:func:`deps_present`) AND the ``resvg``
    rasteriser is on PATH with the asset on disk (:func:`resvg_present`) AND the
    host terminal advertises a graphics protocol (:func:`terminal_supports_images`).
    A CI / snapshot run has no graphics-capable terminal (and the harness sets
    the kill switch), so this returns ``False`` and callers fall back to the
    unicode brand glyph, keeping the goldens glyph-based.

    Returns:
        ``True`` when the seal image path is fully available, else ``False``.
    """
    if os.environ.get(SEAL_DISABLE_ENV):
        return False
    return deps_present() and resvg_present() and terminal_supports_images()


def resolve_accent_hex() -> str:
    """Return the resolved theme accent hex the seal rasterises in.

    The seal's ``currentColor`` fill resolves to the same canonical reskin-green
    accent the header wordmark + offline frame carry
    (:data:`~eawf.surfaces.render.brand.ACCENT_HEX`), so the rasterised mark
    tracks the brand accent rather than ``resvg``'s black default. Centralising
    the lookup keeps the accent single-homed: a future per-theme accent only has
    to feed this one resolver.

    Returns:
        The ``#rrggbb`` accent hex the seal renders in.
    """
    return ACCENT_HEX


def resolve_surface_hex() -> str:
    """Return the resolved theme surface hex the seal is matted onto.

    The seal's transparent corners + star knockout matte onto the main content
    surface (:data:`SURFACE_HEX`, the Wong dark theme's ``$surface``), the same
    surface ``Screen { background: $surface }`` paints behind the hero seal, so
    the matted PNG blends into the live TUI surface rather than the black a
    graphics terminal composites a transparent region onto. Centralising the
    lookup keeps the surface single-homed: a future per-theme surface only has
    to feed this one resolver -- the mirror of :func:`resolve_accent_hex`.

    Returns:
        The ``#rrggbb`` surface hex the seal is matted onto.
    """
    return SURFACE_HEX


def _substitute_current_color(svg_text: str, accent_hex: str) -> str:
    """Return *svg_text* with every ``currentColor`` token replaced by *accent_hex*.

    ``resvg`` has no surrounding document, so it resolves ``currentColor`` to
    black; substituting the resolved accent hex into the SVG text before
    rendering makes the rasterised mark carry the theme accent instead. The
    substitution is a literal token replacement (deterministic), so the same
    ``(svg_text, accent_hex)`` pair always yields the same SVG.

    Args:
        svg_text: The raw Seal SVG markup.
        accent_hex: The ``#rrggbb`` accent to substitute in.

    Returns:
        The SVG text with ``currentColor`` replaced by *accent_hex*.
    """
    return svg_text.replace(_CURRENT_COLOR, accent_hex)


def _seal_png_path(px: int, accent_hex: str, bg_hex: str) -> Path:
    """Return the per-user cache path for the ``px``/``accent_hex``/``bg_hex`` PNG.

    The filename keys on the pixel size, the accent hex, AND the matte
    background hex so a re-theme (or the transparent-to-matted fix itself)
    writes a fresh PNG rather than reusing a stale one -- a pre-existing
    fully-transparent PNG from before the matte fix cannot survive on a path
    that now carries the bg hex. The directory is namespaced by the current
    user so two operators sharing a host never collide on (or read) each
    other's tmp PNG.

    Args:
        px: The square pixel size the PNG was rendered at.
        accent_hex: The ``#rrggbb`` accent the PNG was rendered in.
        bg_hex: The ``#rrggbb`` surface the PNG's transparent regions were
            matted onto.

    Returns:
        The per-user, per-(px, accent, bg) cache path.
    """
    try:
        user = getpass.getuser()
    except KeyError, OSError:
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    cache_dir = Path(tempfile.gettempdir()) / f"eawf-seal-{user}"
    # The accent + bg hexes lead with ``#``, which is filename-safe but reads
    # cleaner stripped; keep both in the stem so the cache key is visible on disk.
    accent_key = accent_hex.lstrip("#")
    bg_key = bg_hex.lstrip("#")
    return cache_dir / f"ea-seal-{px}-{accent_key}-bg{bg_key}.png"


@lru_cache(maxsize=8)
def _rasterised_seal(px: int, accent_hex: str, bg_hex: str) -> Path | None:
    """Rasterise the accent-substituted Seal SVG to a matted ``px``-square PNG.

    Substitutes *accent_hex* for the SVG's ``currentColor`` token (so ``resvg``
    cannot render it black), then rasterises the substituted SVG to a per-user
    PNG keyed on ``(px, accent_hex, bg_hex)`` with ``resvg --background
    <bg_hex>`` so the seal's transparent corners + star knockout are matted onto
    the surface hex (the live-render defect a graphics terminal otherwise
    composites onto black). A stale PNG at the same path is OVERWRITTEN, so a
    pre-existing black / fully-transparent PNG from before this fix can never
    survive into the render -- correctness wins over the read-cache shortcut.
    The in-process :func:`functools.lru_cache` still elides the ``resvg``
    round-trip on a repeat call for the same ``(px, accent_hex, bg_hex)`` within
    the run.

    Args:
        px: The square pixel size to render the seal at.
        accent_hex: The ``#rrggbb`` accent to substitute for ``currentColor``.
        bg_hex: The ``#rrggbb`` surface to matte the transparent regions onto
            via ``resvg --background``.

    Returns:
        The cached PNG path, or ``None`` when ``resvg`` is absent / the asset is
        missing / the rasterise fails.
    """
    if shutil.which(_RESVG) is None or not _SEAL_SVG.exists():
        return None
    out = _seal_png_path(px, accent_hex, bg_hex)
    out.parent.mkdir(parents=True, exist_ok=True)
    svg_text = _SEAL_SVG.read_text(encoding="utf-8")
    coloured = _substitute_current_color(svg_text, accent_hex)
    coloured_svg = out.with_suffix(".svg")
    try:
        coloured_svg.write_text(coloured, encoding="utf-8")
        subprocess.run(
            [
                _RESVG,
                "-w",
                str(px),
                "-h",
                str(px),
                "--background",
                bg_hex,
                str(coloured_svg),
                str(out),
            ],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.info(
            f"_rasterised_seal failed px={px} accent={accent_hex!r} bg={bg_hex!r} err={exc!r}"
        )
        return None
    return out


def seal_image_widget(px: int = 96, *, bg_hex: str | None = None) -> Widget | None:
    """Return a :class:`textual_image.widget.Image` of the Seal, or ``None``.

    Returns ``None`` (the caller renders the unicode glyph instead) whenever the
    seal is not capable or the rasterisation fails, so a caller can write
    ``widget = seal_image_widget() or fallback`` and never crash on a terminal
    or install that cannot show the image. The seal rasterises in the resolved
    theme accent (:func:`resolve_accent_hex`) so the mark carries the brand
    green rather than ``resvg``'s black default, and is matted onto *bg_hex*
    (defaulting to the resolved theme surface, :func:`resolve_surface_hex`) so
    its transparent regions blend into the live TUI surface rather than the
    black a graphics terminal composites a transparent region onto.

    Args:
        px: The square pixel size to rasterise the seal at; the widget scales it
            to its CSS cell box.
        bg_hex: The ``#rrggbb`` surface to matte the transparent regions onto,
            or ``None`` to use the resolved theme surface
            (:func:`resolve_surface_hex`).

    Returns:
        The image widget when capable, else ``None``.
    """
    if not seal_capable():
        return None
    matte = bg_hex if bg_hex is not None else resolve_surface_hex()
    png = _rasterised_seal(px, resolve_accent_hex(), matte)
    if png is None:
        return None
    from textual_image.widget import Image

    return Image(str(png))


__all__ = [
    "SEAL_DISABLE_ENV",
    "SURFACE_HEX",
    "deps_present",
    "resolve_accent_hex",
    "resolve_surface_hex",
    "resvg_present",
    "seal_capable",
    "seal_image_widget",
    "terminal_supports_images",
]
