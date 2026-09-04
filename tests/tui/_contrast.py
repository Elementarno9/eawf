"""WCAG relative-luminance + contrast-ratio helpers for the palette suites.

Textual's :class:`~textual.color.Color` exposes ``get_contrast_text`` (pick a
readable foreground) but no ratio between two chosen colours, and the palette
claims this package pins are stated AS ratios -- "muted clears 4.5:1 on the
panel", "the focus ring separates from the dim border". So the ratio is
computed here, once, against the WCAG 2.x definition:

* linearise each sRGB channel (the ``/12.92`` vs ``((c+0.055)/1.055)**2.4``
  split at ``0.04045``),
* weight them ``0.2126 / 0.7152 / 0.0722`` into a relative luminance,
* ratio the pair as ``(L_light + 0.05) / (L_dark + 0.05)``.

Gamma linearisation is the load-bearing step: a naive luma average scores the
same palette pair a full point differently, which is the difference between a
passing and a failing 4.5:1 gate.
"""

from __future__ import annotations

#: sRGB channel value below which the transfer function is linear.
_LINEAR_CUTOFF: float = 0.04045


def relative_luminance(hex_colour: str) -> float:
    """WCAG relative luminance of an ``#rrggbb`` colour.

    Args:
        hex_colour: A ``#rrggbb`` string; the leading ``#`` is optional and
            the digits may be either case.

    Returns:
        The sRGB relative luminance in ``[0.0, 1.0]`` -- ``0.0`` for black,
        ``1.0`` for white.

    Raises:
        ValueError: When *hex_colour* carries a non-hexadecimal channel.
    """
    raw = hex_colour.lstrip("#")
    channels = [int(raw[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [
        channel / 12.92 if channel <= _LINEAR_CUTOFF else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours.

    Args:
        first: One ``#rrggbb`` colour.
        second: The other ``#rrggbb`` colour.

    Returns:
        The ratio in ``[1.0, 21.0]`` -- ``1.0`` for identical luminances,
        ``21.0`` for black against white. Order-independent.

    Raises:
        ValueError: When either argument carries a non-hexadecimal channel.
    """
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
