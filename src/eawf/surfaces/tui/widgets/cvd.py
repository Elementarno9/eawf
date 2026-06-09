"""Colour-vision-deficiency (CVD) simulation + status-tint legend SVG.

Two small dependency-free helpers backing the colourblind-safety gate for
the cosmic-terminal reskin:

* :func:`simulate_cvd` runs the Vienot 1999 linear dichromat simulation
  (deuteranopia / protanopia) over an ``#rrggbb`` colour, returning the
  ``#rrggbb`` a dichromat perceives. It is plain-math (no numpy): the
  3x3 colour-space matrices are multiplied by hand so the helper carries
  no runtime dependency.
* :func:`render_status_legend_svg` exports the five EA_CB lifecycle band
  swatches (pending / claimed / in_progress / closed / failed) as a small
  ``<rect>``-per-band SVG. This is the legend an ``svg_pixel_diff`` golden
  pins, so a palette edit that collapses two bands reds the visual gate.

Why CVD simulation here: the lifecycle ``status-*`` tints must stay
*pairwise hue-distinct under dichromacy*, not merely byte-stable. Byte
stability proves nobody retyped a hex; CVD-distinctness proves a future
palette edit cannot quietly collapse two bands into the same perceived
colour for a colourblind operator. :func:`colour_distance` over the
simulated swatches is the cheap structural gate that backstops the SVG
golden.

The maths. The Vienot 1999 model works in linear-light LMS space:

1. sRGB ``#rrggbb`` -> linearised sRGB (inverse gamma).
2. linear sRGB -> LMS (Hunt-Pointer-Estevez transform).
3. project onto the dichromat plane (the deuteranope / protanope matrix).
4. LMS -> linear sRGB.
5. linear sRGB -> sRGB (forward gamma) -> ``#rrggbb``.

The dichromat-plane matrices are the canonical Vienot 1999 simulation
matrices (already expressed in linear-sRGB space, so steps 2-4 collapse
into one 3x3 multiply per CVD type).
"""

from __future__ import annotations

from typing import Final

#: The two dichromacy types the gate simulates. Tritanopia is omitted:
#: the lifecycle palette is tuned against the red-green axis (the common
#: deuteranopia / protanopia), which is the axis a band collapse is most
#: likely to hide.
CVD_TYPES: Final[tuple[str, ...]] = ("deuteranopia", "protanopia")

#: Canonical Vienot 1999 dichromat simulation matrices, expressed directly
#: in **linear** sRGB space (row-major 3x3). Multiplying a linearised
#: ``(r, g, b)`` column by one of these yields the linearised colour the
#: dichromat perceives. Sourced from the Vienot, Brettel & Mollon 1999
#: model as popularised by the colorblind-simulation literature.
_CVD_MATRICES: Final[dict[str, tuple[tuple[float, float, float], ...]]] = {
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
}


def _srgb_to_linear(channel: float) -> float:
    """Linearise one 0..1 sRGB channel via the inverse sRGB gamma.

    Args:
        channel: An sRGB channel in ``[0, 1]``.

    Returns:
        The linear-light channel in ``[0, 1]``.
    """
    if channel <= 0.04045:
        return channel / 12.92
    return float(((channel + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(channel: float) -> float:
    """Apply the forward sRGB gamma to one 0..1 linear channel.

    Args:
        channel: A linear-light channel in ``[0, 1]``.

    Returns:
        The sRGB channel in ``[0, 1]``.
    """
    if channel <= 0.0031308:
        return channel * 12.92
    return float(1.055 * (channel ** (1.0 / 2.4)) - 0.055)


def _parse_hex(hex_colour: str) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` string into an 8-bit ``(r, g, b)`` tuple.

    Args:
        hex_colour: A 7-character ``#rrggbb`` colour string.

    Returns:
        The ``(r, g, b)`` channels, each in ``0..255``.

    Raises:
        ValueError: When *hex_colour* is not a 7-character ``#rrggbb``
            string or carries non-hex digits.
    """
    text = hex_colour.strip()
    if len(text) != 7 or not text.startswith("#"):
        raise ValueError(f"expected a #rrggbb colour: {hex_colour!r}")
    try:
        r = int(text[1:3], 16)
        g = int(text[3:5], 16)
        b = int(text[5:7], 16)
    except ValueError as exc:
        raise ValueError(f"non-hex digit in colour: {hex_colour!r}") from exc
    return r, g, b


def _format_hex(r: int, g: int, b: int) -> str:
    """Format an 8-bit ``(r, g, b)`` triple as a ``#rrggbb`` string.

    Args:
        r: Red channel ``0..255``.
        g: Green channel ``0..255``.
        b: Blue channel ``0..255``.

    Returns:
        The lowercase ``#rrggbb`` colour string.
    """
    return f"#{r:02x}{g:02x}{b:02x}"


def simulate_cvd(hex_colour: str, cvd_type: str) -> str:
    """Simulate how a dichromat perceives *hex_colour* via the Vienot 1999 model.

    Linearises the sRGB colour, multiplies it by the *cvd_type* dichromat
    matrix (already in linear-sRGB space), then re-applies the sRGB gamma
    and clamps back into ``#rrggbb``. Pure function, no I/O, no numpy.

    Args:
        hex_colour: The source colour as a ``#rrggbb`` string.
        cvd_type: One of :data:`CVD_TYPES` (``"deuteranopia"`` /
            ``"protanopia"``).

    Returns:
        The ``#rrggbb`` colour the dichromat perceives.

    Raises:
        ValueError: When *hex_colour* is malformed, or *cvd_type* is not a
            recognised dichromacy type.
    """
    if cvd_type not in _CVD_MATRICES:
        raise ValueError(f"unknown cvd type: {cvd_type!r}")
    r8, g8, b8 = _parse_hex(hex_colour)
    lin = (
        _srgb_to_linear(r8 / 255.0),
        _srgb_to_linear(g8 / 255.0),
        _srgb_to_linear(b8 / 255.0),
    )
    matrix = _CVD_MATRICES[cvd_type]
    simulated_lin = tuple(
        matrix[row][0] * lin[0] + matrix[row][1] * lin[1] + matrix[row][2] * lin[2]
        for row in range(3)
    )
    out = []
    for value in simulated_lin:
        srgb = _linear_to_srgb(max(0.0, min(1.0, value)))
        out.append(max(0, min(255, round(srgb * 255.0))))
    return _format_hex(out[0], out[1], out[2])


def colour_distance(hex_a: str, hex_b: str) -> float:
    """Return the Euclidean RGB distance between two ``#rrggbb`` colours.

    A coarse perceptual proxy good enough for the band-collapse gate: two
    swatches that simulate to within a small distance are treated as
    indistinct. Operates on the 8-bit channels (range ``0..441.67`` for
    the full black-white diagonal).

    Args:
        hex_a: First ``#rrggbb`` colour.
        hex_b: Second ``#rrggbb`` colour.

    Returns:
        The Euclidean distance in 8-bit RGB space.

    Raises:
        ValueError: When either argument is not a ``#rrggbb`` string.
    """
    ra, ga, ba = _parse_hex(hex_a)
    rb, gb, bb = _parse_hex(hex_b)
    return float(((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5)


#: The five EA_CB (IBM colourblind-safe) lifecycle band swatches, in
#: lifecycle order, sourced from the canonical
#: :data:`eawf.surfaces.tui.theme._IBM_VARIABLES` ``status-*`` hexes. Kept
#: as an ordered tuple of ``(label, hex)`` so the legend SVG renders the
#: swatches left-to-right in a stable order (the golden pins that order).
EA_CB_BANDS: Final[tuple[tuple[str, str], ...]] = (
    ("pending", "#8a8a8a"),
    ("claimed", "#648fff"),
    ("in_progress", "#ffb000"),
    ("closed", "#1a9988"),
    ("failed", "#dc267f"),
)

#: Per-swatch square edge (px) in the rendered legend SVG.
_SWATCH_PX: Final[int] = 32


def render_status_legend_svg(bands: tuple[tuple[str, str], ...] = EA_CB_BANDS) -> str:
    """Render the lifecycle status-tint legend as a row of SVG swatches.

    Emits one ``<rect>`` per band, laid out left-to-right at
    :data:`_SWATCH_PX` per cell, so the produced SVG is a compact colour
    legend an ``svg_pixel_diff`` golden can pin byte-for-byte. The markup
    is deterministic (no timestamps, no font dependency) so the rendered
    PNG is stable under the pinned resvg.

    Args:
        bands: The ordered ``(label, hex)`` swatches to render. Defaults to
            :data:`EA_CB_BANDS` (the EA_CB lifecycle palette).

    Returns:
        A well-formed SVG document string.
    """
    width = _SWATCH_PX * len(bands)
    height = _SWATCH_PX
    rects = "".join(
        f'<rect x="{index * _SWATCH_PX}" y="0" '
        f'width="{_SWATCH_PX}" height="{_SWATCH_PX}" fill="{hex_colour}"/>'
        for index, (_label, hex_colour) in enumerate(bands)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f"{rects}"
        "</svg>\n"
    )


__all__ = [
    "CVD_TYPES",
    "EA_CB_BANDS",
    "colour_distance",
    "render_status_legend_svg",
    "simulate_cvd",
]
