"""Render the Eä Seal as a terminal image (graphics-protocol brand mark).

Textual draws cells, but a graphics-capable terminal (Ghostty / Kitty / iTerm2
/ WezTerm via the Kitty graphics protocol, or sixel, with a unicode-halfcell
fallback) can display an inline raster image. This module wires the real Seal
SVG into the TUI: :func:`~eawf.surfaces.render` ``assets/ea-seal.svg`` is
rasterised to a PNG via the ``resvg`` CLI (already a repo dependency for the
``svg_pixel_diff`` audit kind), and :class:`textual_image.widget.Image` embeds
it through whatever protocol the terminal supports.

The capability is **opt-in + degrades cleanly**. ``textual-image`` + Pillow are
an optional extra (``eawf[seal]``), so a default install -- and every CI /
snapshot run -- has no ``textual_image`` import and :func:`seal_capable` returns
``False``; callers fall back to the unicode ``◉`` brand glyph. The image only
appears when an operator installs the extra AND ``resvg`` is on PATH, so the
golden snapshots (which run without the extra) stay glyph-based and stable.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.widget import Widget

logger = logging.getLogger(__name__)

#: The Seal SVG asset (``currentColor`` fill, so it inherits the text colour).
#: ``seal.py`` lives in ``surfaces/tui/widgets/``; the asset in
#: ``surfaces/render/assets/`` -- two parents up from ``widgets`` to
#: ``surfaces``, then into ``render/assets``.
_SEAL_SVG: Path = Path(__file__).resolve().parents[2] / "render" / "assets" / "ea-seal.svg"

#: The ``resvg`` CLI binary name (the same renderer the ``svg_pixel_diff`` audit
#: kind pins). Absent on most machines -- :func:`seal_capable` checks for it.
_RESVG: str = "resvg"


@lru_cache(maxsize=1)
def seal_capable() -> bool:
    """Return whether the TUI can render the Seal as an inline image.

    ``True`` only when the optional ``textual-image`` extra is importable AND
    the ``resvg`` rasteriser is on PATH AND the Seal asset exists. A default
    install / CI run has no ``textual_image`` and returns ``False``, so callers
    fall back to the unicode brand glyph and the goldens stay glyph-based.

    Returns:
        ``True`` when the seal image path is fully available, else ``False``.
    """
    if shutil.which(_RESVG) is None or not _SEAL_SVG.exists():
        return False
    try:
        import textual_image.widget  # noqa: F401  (capability probe only)
    except ImportError:
        return False
    return True


@lru_cache(maxsize=8)
def _rasterised_seal(px: int) -> Path | None:
    """Rasterise the Seal SVG to a ``px``-square PNG via ``resvg``; cache it.

    Args:
        px: The square pixel size to render the seal at.

    Returns:
        The cached PNG path, or ``None`` when ``resvg`` is absent / fails.
    """
    if shutil.which(_RESVG) is None or not _SEAL_SVG.exists():
        return None
    out = Path(tempfile.gettempdir()) / f"ea-seal-{px}.png"
    if out.exists():
        return out
    try:
        subprocess.run(
            [_RESVG, "-w", str(px), "-h", str(px), str(_SEAL_SVG), str(out)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.info(f"_rasterised_seal failed px={px} err={exc!r}")
        return None
    return out


def seal_image_widget(px: int = 96) -> Widget | None:
    """Return a :class:`textual_image.widget.Image` of the Seal, or ``None``.

    Returns ``None`` (the caller renders the unicode glyph instead) whenever the
    seal is not capable or the rasterisation fails, so a caller can write
    ``widget = seal_image_widget() or fallback`` and never crash on a terminal
    or install that cannot show the image.

    Args:
        px: The square pixel size to rasterise the seal at; the widget scales it
            to its CSS cell box.

    Returns:
        The image widget when capable, else ``None``.
    """
    if not seal_capable():
        return None
    png = _rasterised_seal(px)
    if png is None:
        return None
    from textual_image.widget import Image

    return Image(str(png))


__all__ = ["seal_capable", "seal_image_widget"]
