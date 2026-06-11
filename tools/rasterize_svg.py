"""Rasterize an SVG string/file to PNG bytes via the pinned ``resvg`` CLI.

Shared by the VIS-1 image-diff gate (``mockup_golden_diff`` image mode):
the live TUI is captured as an SVG via Textual ``App.export_screenshot``,
then handed here to become the PNG the layout-shape rubric scores. The
mockup reference path is the same -- a static SVG mock rasterised through
the identical resvg invocation -- so both sides of the diff share one
renderer.

The resvg invocation mirrors :mod:`eawf.workflow.audit_dsl.kinds.svg_pixel_diff`:
system fonts are disabled and a vendored font directory is passed when
available, so the render is host-independent. ``resvg`` is absent on most
developer machines; callers that need a clean skip should check
:func:`resvg_available` first.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: The renderer. Pinned to a concrete version in the CI tool manifest; a
#: version bump is a golden-refresh event (see ``svg_pixel_diff``).
_RESVG: str = "resvg"


def resvg_available() -> bool:
    """Whether the ``resvg`` CLI is on PATH."""
    return shutil.which(_RESVG) is not None


def rasterize_svg_to_png(
    svg: str,
    *,
    fonts_dir: Path | None = None,
    width: int | None = None,
) -> bytes:
    """Render an SVG string to 8-bit RGBA PNG bytes via ``resvg``.

    Args:
        svg: The SVG document as a string.
        fonts_dir: Optional vendored font directory passed to
            ``resvg --use-fonts-dir``; system fonts are always disabled so
            the render does not depend on host fonts.
        width: Optional output width in pixels (``resvg -w``). Height scales
            to preserve aspect ratio.

    Returns:
        The rendered PNG bytes.

    Raises:
        FileNotFoundError: When ``resvg`` is not installed.
        RuntimeError: When the render exits non-zero or produces no file.
    """
    if not resvg_available():
        raise FileNotFoundError("resvg not installed")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.svg"
        out = Path(tmp) / "out.png"
        src.write_text(svg, encoding="utf-8")
        argv = [_RESVG, "--skip-system-fonts"]
        if fonts_dir is not None:
            argv += ["--use-fonts-dir", str(fonts_dir)]
        if width is not None:
            argv += ["-w", str(width)]
        argv += [str(src), str(out)]
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        if completed.returncode != 0 or not out.is_file():
            diagnostic = completed.stderr.strip() or completed.stdout.strip() or "render failed"
            raise RuntimeError(f"resvg render failed: {diagnostic}")
        return out.read_bytes()


__all__ = ["rasterize_svg_to_png", "resvg_available"]
