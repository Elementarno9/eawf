"""Tests for the optional Seal image brand mark (graphics-protocol path).

The seal is an opt-in enhancement: it renders only when the ``eawf[seal]`` extra
(``textual-image`` + Pillow) is installed AND ``resvg`` is on PATH. Without the
extra -- the default install + every CI / snapshot run -- it must report
not-capable and hand back ``None`` so callers fall back to the unicode brand
glyph and the goldens stay glyph-based. These tests pin that contract regardless
of whether the running environment happens to have the extra.
"""

from __future__ import annotations

import shutil

from eawf.surfaces.tui.widgets.seal import (
    _RESVG,
    _SEAL_SVG,
    seal_capable,
    seal_image_widget,
)


def _extra_importable() -> bool:
    try:
        import textual_image.widget  # noqa: F401

        return True
    except ImportError:
        return False


def test_seal_capable_matches_resvg_and_extra() -> None:
    # Capable iff the rasteriser, the optional extra, and the asset are all
    # present -- the exact gate callers use before reaching for the image.
    seal_capable.cache_clear()
    expected = shutil.which(_RESVG) is not None and _extra_importable() and _SEAL_SVG.exists()
    assert seal_capable() is expected


def test_seal_image_widget_none_when_not_capable() -> None:
    # When not capable the factory returns None (never raises), so a caller can
    # write ``seal_image_widget() or glyph_fallback`` safely.
    seal_capable.cache_clear()
    if not seal_capable():
        assert seal_image_widget() is None


def test_seal_asset_exists() -> None:
    # The Seal SVG ships in the package so a graphics terminal can rasterise it.
    assert _SEAL_SVG.exists()
    assert _SEAL_SVG.suffix == ".svg"
