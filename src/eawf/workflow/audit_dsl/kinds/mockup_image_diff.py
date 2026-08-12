"""Layout-shape image-diff falsifier for mockup vs live-TUI renders (VIS-1).

The sibling ``mockup_golden_diff`` text path captures the live TUI as a
**normalised ASCII frame** and byte-compares it to an approved text golden.
That path is blind to the failure mode that shipped the P30-I15 redesign
"faithfully" and yet wrong: a *square* border where the mockup asked for a
*round* one, and a *one-column* body where the mockup asked for *two
columns*. ASCII normalisation collapses round-vs-square box-drawing glyphs
and column gutters into the same diffable text, so the text gate passed a
visually divergent screen.

This module is the **image** falsifier those misses demand. It rasterises a
reference MOCKUP frame and a screenshot of the LIVE TUI as IMAGES (PNG) and
scores their divergence with a rubric that weights **layout SHAPE above
token fidelity**: border-corner shape (round vs square) and content column
count dominate the score, so a square-vs-round or one-vs-two-column
divergence FAILS even when most glyphs match, while a faithful pair that
differs only in incidental token rendering PASSES.

Dependency-free decode
-----------------------

Pillow / numpy are NOT runtime dependencies of eawf, and adding one for a
single gate would violate the project's YAGNI floor. resvg emits 8-bit
non-interlaced RGBA PNGs (verified: ``IHDR ctype=6 depth=8 interlace=0``),
which the PNG spec lets a small ``zlib``-only decoder defilter without any
third-party image library. :func:`decode_png_luma_grid` does exactly that:
it inflates the IDAT stream, reverses the five PNG row filters, converts to
luma, and down-samples to a coarse ink-occupancy grid. The layout-shape
features are read off that grid, so the whole comparison is stdlib-only.

CSS-to-Textual mapping (pinned)
-------------------------------

The redesign lesson is that a CSS mockup maps onto a Textual surface through
a fixed vocabulary; an "un-renderable" claim must cite the row that governs
it. :data:`CSS_TO_TEXTUAL_MAPPING` pins that table so the gate spec and a
reviewer share one canonical reference: ``border-radius`` round corners map
to Textual ``border: round`` (a square ``border: solid`` is the divergence
this gate catches), a CSS flex/grid two-column row maps to a Textual ``Grid``
/ ``Horizontal`` with two children (a collapsed single column is the other
divergence), and so on.
"""

from __future__ import annotations

import logging
import struct
import zlib
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Pinned CSS-to-Textual mapping table. A mockup authored in CSS is realised
#: on the Textual surface through this fixed vocabulary; an "un-renderable"
#: claim (a CSS property with no Textual realisation) must cite the row that
#: governs it. The two rows the VIS-1 gate actively falsifies are the FIRST
#: (border-radius -> ``border: round``) and the THIRD (two-column flex/grid
#: row -> a ``Grid`` with two children); the others document the remaining
#: vocabulary so the table is a complete reference, not a stub.
CSS_TO_TEXTUAL_MAPPING: dict[str, str] = {
    "border-radius:<r>": "border: round  (round corners; a square border: solid is the divergence)",
    "border:1px solid": "border: solid  (square corners)",
    "display:flex; flex-direction:row (2 cols)": (
        "Grid / Horizontal with two child containers "
        "(a single child is the one-vs-two-column divergence)"
    ),
    "display:flex; flex-direction:column": "Vertical container",
    "display:grid; grid-template-columns": "Grid with grid-size-columns",
    "padding": "padding: <cells>",
    "background-color": "background: <color>",
    "color": "color: <color>",
    "font-weight:bold": "text-style: bold",
}

#: Side length (in cells) of the coarse ink-occupancy grid the layout-shape
#: features are read off. 32 is coarse enough that incidental per-glyph
#: differences average out (so a faithful pair scores ~0 on token noise) yet
#: fine enough to resolve the four box corners and a central column gutter.
_GRID_N: int = 32

#: RGB bucket size for finding the dominant background color. The live Textual
#: screenshot uses a dark terminal background while the old synthetic fixtures
#: use a light canvas; a fixed luma threshold makes the whole dark screenshot
#: look like ink. The dominant opaque color is the background in both cases.
_BACKGROUND_BUCKET: int = 16

#: Minimum RGB distance from the dominant background for a source pixel to
#: count as foreground ink. Tuned below visible text / border contrast and
#: above anti-aliased background noise.
_INK_BACKGROUND_DISTANCE_MIN: float = 35.0

#: Ink fraction the EXTREME corner cell must reach for the corner to count as
#: "filled" (a SQUARE border). A square border runs straight into the corner,
#: filling the outermost border cell; a ROUND border clips the arc away from
#: the corner, leaving that extreme cell near-empty. Reading the single
#: extreme corner cell -- not a wide window -- is what separates the two
#: shapes: both shapes have ink a few cells in, but only the square fills the
#: very corner.
_CORNER_FILL_MIN: float = 0.4

#: Layout-shape weight. The border-shape and column-count features each
#: contribute this much to the divergence score, so a single layout-shape
#: mismatch (e.g. round-vs-square) already exceeds :data:`_FAIL_THRESHOLD`
#: while a pile of token-level pixel noise (weighted at 1.0 over the whole
#: grid) cannot, by itself, cross it. This IS the "layout shape above token
#: fidelity" rubric.
_LAYOUT_WEIGHT: float = 1.0

#: Token-fidelity weight. Per-cell ink differences that are NOT explained by a
#: layout-shape feature contribute their mean at this (much smaller) weight,
#: so faithful pairs with incidental token noise stay under threshold.
_TOKEN_WEIGHT: float = 0.25

#: Divergence score at or above which the gate FAILS. Set below a single
#: layout-shape feature's weight (:data:`_LAYOUT_WEIGHT`) so one shape
#: mismatch fails, and above the worst plausible token-only noise
#: (``_TOKEN_WEIGHT`` * a small ink-fraction delta) so a faithful pair passes.
_FAIL_THRESHOLD: float = 0.5

#: Weight of a broadened secondary falsifier (contrast-on-selection and
#: alignment/right-edge). Set between :data:`_FAIL_THRESHOLD` and
#: :data:`_LAYOUT_WEIGHT` so a single secondary mismatch fails on its own
#: (it crosses the threshold) yet stays BELOW a border/column layout-shape
#: mismatch -- keeping the "layout shape above token fidelity" rubric with
#: the border/column pair the dominant signal.
_SECONDARY_WEIGHT: float = 0.6

#: WCAG-style contrast-ratio floor a selected-row band must clear. A live
#: render that drops the selected row's foreground/background contrast below
#: this (the W33-class defect: dim text on a dim highlight) diverges from a
#: mockup that clears it. 2.5 sits under the ~5+ a faithful white-on-accent
#: highlight scores and well above the ~1.2 a dim-on-dim regression scores.
_SELECTION_CONTRAST_FLOOR: float = 2.5

#: Minimum row-mean-luma deviation (0..1) from the frame's typical background
#: for a horizontal band to count as a SELECTED highlight bar. Frames with no
#: highlight (the flat synthetic layout fixtures) never clear this, so their
#: contrast is reported as :data:`_NO_SELECTION_BAND_CONTRAST` -- neutral, so
#: they raise no contrast regression.
_SELECTION_BAND_DEVIATION_MIN: float = 0.06

#: Minimum per-pixel luma delta (0..1) from a band's background for the pixel
#: to count as foreground text within that band.
_SELECTION_INK_LUMA_DELTA: float = 0.15

#: Contrast reported when no selected-row band is detected. The WCAG maximum
#: (white on black) so a bandless frame always clears
#: :data:`_SELECTION_CONTRAST_FLOOR` and raises no regression.
_NO_SELECTION_BAND_CONTRAST: float = 21.0

#: Max spread (in grid cells) of body-row right edges for a column to read as
#: RIGHT-ALIGNED. A right-aligned column (the W10 class) ends every row at the
#: same x, so its edge spread is tiny; a left-aligned / ragged column varies.
_RIGHT_EDGE_ALIGN_TOLERANCE: int = 1

#: Sentinel value for :class:`MockupGoldenDiffArgs.tui_png` that selects the
#: LIVE-capture image mode: instead of decoding a committed TUI-render PNG,
#: the gate mounts the surface under Pilot, exports the screen SVG, rasterises
#: it through the pinned ``resvg`` chain, and diffs THAT live render against
#: the mockup golden. The surface is chosen by the reused text-mode selectors
#: (``scope`` / ``state_path`` / ``mode`` / ``key_sequence`` / ``size``), so no
#: separate live-surface arg is needed.
LIVE_CAPTURE_SENTINEL: str = "<live>"


def _paeth(a: int, b: int, c: int) -> int:
    """PNG Paeth predictor over the left/up/up-left bytes."""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _defilter_rgba(raw: bytes, width: int, height: int) -> bytearray:
    """Reverse the five PNG row filters over an 8-bit RGBA scanline stream.

    Args:
        raw: The inflated IDAT bytes: ``height`` rows, each a 1-byte filter
            tag followed by ``width * 4`` channel bytes.
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        The defiltered RGBA pixel bytes (``width * height * 4`` long).

    Raises:
        ValueError: When the stream length or a filter tag is invalid.
    """
    stride = width * 4
    need = height * (stride + 1)
    if len(raw) < need:
        raise ValueError(f"idat too short for {width}x{height} rgba: have {len(raw)} need {need}")
    out = bytearray(stride * height)
    pos = 0
    for row in range(height):
        ftype = raw[pos]
        pos += 1
        line = raw[pos : pos + stride]
        pos += stride
        out_off = row * stride
        prev_off = out_off - stride
        for i in range(stride):
            x = line[i]
            a = out[out_off + i - 4] if i >= 4 else 0
            b = out[prev_off + i] if row > 0 else 0
            c = out[prev_off + i - 4] if (row > 0 and i >= 4) else 0
            if ftype == 0:
                val = x
            elif ftype == 1:
                val = x + a
            elif ftype == 2:
                val = x + b
            elif ftype == 3:
                val = x + ((a + b) >> 1)
            elif ftype == 4:
                val = x + _paeth(a, b, c)
            else:
                raise ValueError(f"unknown png filter type {ftype} at row {row}")
            out[out_off + i] = val & 0xFF
    return out


def _parse_png_chunks(png_bytes: bytes) -> tuple[int, int, bytes]:
    """Parse a PNG into ``(width, height, idat)`` for the supported shape.

    Walks the chunk stream collecting the IHDR dimensions and the
    concatenated IDAT payload, stopping at IEND.

    Args:
        png_bytes: The PNG file bytes.

    Returns:
        A ``(width, height, idat)`` triple; ``idat`` is the raw (still
        zlib-compressed) IDAT payload.

    Raises:
        ValueError: When the signature is bad, the IHDR is not the 8-bit
            non-interlaced RGBA shape, or no IHDR is present.
    """
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png (bad signature)")
    pos = 8
    width = height = 0
    idat = bytearray()
    while pos < len(png_bytes):
        (length,) = struct.unpack(">I", png_bytes[pos : pos + 4])
        ctype = png_bytes[pos + 4 : pos + 8]
        body = png_bytes[pos + 8 : pos + 8 + length]
        if ctype == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color != 6 or interlace != 0:
                raise ValueError(
                    f"unsupported png: depth={depth} color={color} interlace={interlace} "
                    "(need 8-bit non-interlaced rgba)"
                )
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if width == 0 or height == 0:
        raise ValueError("png has no IHDR / zero dimensions")
    return width, height, bytes(idat)


def _dominant_background_rgb(pixels: bytearray, width: int, height: int) -> tuple[int, int, int]:
    """Return the dominant opaque RGB bucket center for a rendered PNG.

    Both supported sources have a large flat background: the synthetic VIS-1
    fixtures use light paper, and Textual ``export_screenshot`` uses dark
    terminal/surface colors. Bucketed dominance survives antialiased text and
    box borders without pinning the gate to either palette.
    """
    counts: Counter[tuple[int, int, int]] = Counter()
    for off in range(0, width * height * 4, 4):
        alpha = pixels[off + 3]
        if alpha >= 128:
            counts[
                (
                    pixels[off] // _BACKGROUND_BUCKET,
                    pixels[off + 1] // _BACKGROUND_BUCKET,
                    pixels[off + 2] // _BACKGROUND_BUCKET,
                )
            ] += 1
    if not counts:
        return (255, 255, 255)
    bucket = counts.most_common(1)[0][0]
    half = _BACKGROUND_BUCKET // 2
    red, green, blue = bucket
    return (
        min(255, red * _BACKGROUND_BUCKET + half),
        min(255, green * _BACKGROUND_BUCKET + half),
        min(255, blue * _BACKGROUND_BUCKET + half),
    )


def _is_foreground_ink(
    *,
    r: int,
    g: int,
    b: int,
    alpha: int,
    background: tuple[int, int, int],
) -> bool:
    """Whether one source pixel differs enough from the background to be ink."""
    if alpha < 128:
        return False
    dr = r - background[0]
    dg = g - background[1]
    db = b - background[2]
    distance_sq = dr * dr + dg * dg + db * db
    return distance_sq >= _INK_BACKGROUND_DISTANCE_MIN * _INK_BACKGROUND_DISTANCE_MIN


def _downsample_ink(pixels: bytearray, width: int, height: int, grid_n: int) -> list[list[float]]:
    """Down-sample defiltered RGBA pixels to a ``grid_n`` ink-fraction grid.

    A source pixel is "ink" when it is opaque and its RGB color differs from
    the dominant background color. This keeps the gate palette-neutral: dark
    marks on light mockups and light / accent marks on dark Textual screenshots
    both become foreground, while the background itself stays empty.
    """
    stride = width * 4
    background = _dominant_background_rgb(pixels, width, height)
    grid = [[0.0 for _ in range(grid_n)] for _ in range(grid_n)]
    counts = [[0 for _ in range(grid_n)] for _ in range(grid_n)]
    for y in range(height):
        gy = min(grid_n - 1, y * grid_n // height)
        row_off = y * stride
        for x in range(width):
            off = row_off + x * 4
            r, g, b, alpha = pixels[off], pixels[off + 1], pixels[off + 2], pixels[off + 3]
            gx = min(grid_n - 1, x * grid_n // width)
            counts[gy][gx] += 1
            if _is_foreground_ink(r=r, g=g, b=b, alpha=alpha, background=background):
                grid[gy][gx] += 1.0
    for gy in range(grid_n):
        for gx in range(grid_n):
            if counts[gy][gx]:
                grid[gy][gx] /= counts[gy][gx]
    return grid


def _decode_pixels(png_bytes: bytes) -> tuple[bytearray, int, int]:
    """Inflate + defilter a PNG to raw RGBA pixels and its dimensions.

    Shared by the ink-fraction grid and the selection-contrast probe so a
    caller scoring both features off one image defilters the (potentially
    large) pixel buffer only once.

    Raises:
        ValueError: When the bytes are not a PNG, or the IHDR is not the
            8-bit non-interlaced RGBA shape this decoder supports.
    """
    width, height, idat = _parse_png_chunks(png_bytes)
    pixels = _defilter_rgba(zlib.decompress(idat), width, height)
    return pixels, width, height


def decode_png_luma_grid(png_bytes: bytes, grid_n: int = _GRID_N) -> list[list[float]]:
    """Decode an 8-bit RGBA PNG to a coarse ``grid_n x grid_n`` ink-fraction grid.

    Dependency-free: inflates the IDAT stream with :mod:`zlib`, reverses the
    PNG row filters, estimates the dominant background color, and downsamples
    into a ``grid_n x grid_n`` grid whose cells hold the fraction of source
    pixels that are foreground "ink" (far enough from that background).

    Args:
        png_bytes: The PNG file bytes (8-bit, RGBA, non-interlaced -- the
            shape resvg emits).
        grid_n: Side length of the output occupancy grid.

    Returns:
        A ``grid_n``-row list of ``grid_n``-column ink-fraction floats in
        ``[0.0, 1.0]``.

    Raises:
        ValueError: When the bytes are not a PNG, or the IHDR is not the
            8-bit non-interlaced RGBA shape this decoder supports.
    """
    pixels, width, height = _decode_pixels(png_bytes)
    return _downsample_ink(pixels, width, height, grid_n)


def _luma(r: int, g: int, b: int) -> float:
    """Perceptual luma of an 8-bit RGB pixel, normalised to ``[0.0, 1.0]``."""
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _wcag_contrast(l1: float, l2: float) -> float:
    """WCAG-style contrast ratio between two ``[0,1]`` luminances.

    ``(L_light + 0.05) / (L_dark + 0.05)`` -- 1.0 for identical luminances,
    up to 21.0 for pure white on pure black.
    """
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


def _median(values: list[float]) -> float:
    """Median of a non-empty float list (mean of the two central items)."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _row_mean_luma(pixels: bytearray, width: int, y: int) -> float:
    """Mean luma of source row ``y``."""
    base = y * width * 4
    total = 0.0
    for x in range(width):
        off = base + x * 4
        total += _luma(pixels[off], pixels[off + 1], pixels[off + 2])
    return total / width


def _selection_band_contrast(pixels: bytearray, width: int, height: int) -> float:
    """Foreground/background contrast ratio over the selected-row band.

    Scans the INTERIOR rows (a border margin is skipped so a full-width dark
    frame edge is not mistaken for the highlight), locates the horizontal
    highlight band -- the contiguous interior rows whose mean luma deviates
    most from the frame's typical background luma -- then scores the WCAG-style
    contrast between the band's foreground text pixels and its background fill.
    This is the W33-class falsifier: a live render that dims the selected
    row's text into its highlight fails the ratio a faithful mockup clears.

    Returns:
        The contrast ratio, or :data:`_NO_SELECTION_BAND_CONTRAST` when the
        interior is too thin, no interior band clears
        :data:`_SELECTION_BAND_DEVIATION_MIN`, or the band carries no separable
        foreground text (so a bandless frame raises no contrast regression).
    """
    margin = max(1, height // 10)
    lo, hi = margin, height - margin
    if hi - lo < 2:
        return _NO_SELECTION_BAND_CONTRAST

    row_luma = [_row_mean_luma(pixels, width, y) for y in range(lo, hi)]
    background_luma = _median(row_luma)
    deviations = [abs(value - background_luma) for value in row_luma]
    peak = max(deviations)
    if peak < _SELECTION_BAND_DEVIATION_MIN:
        return _NO_SELECTION_BAND_CONTRAST

    band_floor = peak / 2.0
    peak_index = deviations.index(peak)
    top = peak_index
    while top > 0 and deviations[top - 1] >= band_floor:
        top -= 1
    bottom = peak_index
    while bottom + 1 < len(deviations) and deviations[bottom + 1] >= band_floor:
        bottom += 1

    stride = width * 4
    band_luma: list[float] = []
    for y in range(lo + top, lo + bottom + 1):
        base = y * stride
        for x in range(width):
            off = base + x * 4
            band_luma.append(_luma(pixels[off], pixels[off + 1], pixels[off + 2]))
    fill_luma = _median(band_luma)
    foreground = [
        value for value in band_luma if abs(value - fill_luma) > _SELECTION_INK_LUMA_DELTA
    ]
    if not foreground:
        return _NO_SELECTION_BAND_CONTRAST
    text_luma = sum(foreground) / len(foreground)
    return _wcag_contrast(text_luma, fill_luma)


def decode_png_selection_contrast(png_bytes: bytes) -> float:
    """Decode a PNG and score its selected-row foreground/background contrast.

    Dependency-free companion to :func:`decode_png_luma_grid`; see
    :func:`_selection_band_contrast` for the band-location + ratio rubric.

    Raises:
        ValueError: When the bytes are not the supported 8-bit non-interlaced
            RGBA PNG shape.
    """
    pixels, width, height = _decode_pixels(png_bytes)
    return _selection_band_contrast(pixels, width, height)


@dataclass(frozen=True)
class LayoutFeatures:
    """Layout-shape features read off an ink-occupancy grid.

    Attributes:
        corners_filled: For ``(top-left, top-right, bottom-left,
            bottom-right)``, whether the corner probe-window is ink-filled --
            ``True`` for a SQUARE border corner, ``False`` for a ROUND one
            (a round corner clips the very corner cell).
        column_count: Number of distinct vertical content bands separated by
            an empty gutter -- ``1`` for a single-column body, ``2`` for a
            two-column body.
        right_edge_aligned: Whether the body rows share a common right edge
            -- ``True`` for a RIGHT-ALIGNED column (every row ends at the same
            x, the W10 class), ``False`` for a left-aligned / ragged column.
        selection_contrast_ok: Whether the selected-row band clears the
            foreground/background contrast floor -- ``True`` when the highlight
            text is legible (or no highlight band is present), ``False`` for
            the W33-class dim-on-dim regression.
    """

    corners_filled: tuple[bool, bool, bool, bool]
    column_count: int
    right_edge_aligned: bool
    selection_contrast_ok: bool


@dataclass(frozen=True)
class _BorderBands:
    """Top/bottom/left/right border bands in grid coordinates."""

    top: int
    bottom: int
    left: int
    right: int


def _first_band(counts: list[int], *, start: int, stop: int, threshold: int, default: int) -> int:
    """Return the first index in ``[start, stop)`` with enough ink."""
    for idx in range(start, stop):
        if counts[idx] >= threshold:
            return idx
    return default


def _last_band(counts: list[int], *, start: int, stop: int, threshold: int, default: int) -> int:
    """Return the last index in ``[start, stop)`` with enough ink."""
    for idx in range(stop - 1, start - 1, -1):
        if counts[idx] >= threshold:
            return idx
    return default


def _border_bands(grid: list[list[float]]) -> _BorderBands:
    """Locate horizontal and vertical border bands independently.

    The original VIS-1 fixtures put the frame at a symmetric offset, but real
    TUI screenshots often have different top and left margins. Reading one
    offset for both axes makes the corner probe miss the actual corner. This
    scans rows for horizontal runs and columns for vertical runs separately.
    """
    n = len(grid)
    ink = [[cell >= 0.4 for cell in row] for row in grid]
    row_counts = [sum(row) for row in ink]
    col_counts = [sum(ink[r][c] for r in range(n)) for c in range(n)]
    row_threshold = max(2, n // 3)
    col_threshold = max(2, n // 6)
    edge_window = max(4, n // 3)
    return _BorderBands(
        top=_first_band(row_counts, start=0, stop=edge_window, threshold=row_threshold, default=1),
        bottom=_last_band(
            row_counts, start=n - edge_window, stop=n, threshold=row_threshold, default=n - 2
        ),
        left=_first_band(col_counts, start=0, stop=edge_window, threshold=col_threshold, default=1),
        right=_last_band(
            col_counts, start=n - edge_window, stop=n, threshold=col_threshold, default=n - 2
        ),
    )


def _corner_filled(grid: list[list[float]], *, top: bool, left: bool, bands: _BorderBands) -> bool:
    """Whether the EXTREME corner cell of the border band is ink-filled.

    Reads the single cell at the intersection of the border band row and the
    border band column on the chosen side. A SQUARE border fills it (the two
    straight edges meet there); a ROUND border clips the arc inward, leaving
    that extreme cell near-empty -- so the boolean is ``True`` for square and
    ``False`` for round.
    """
    r = bands.top if top else bands.bottom
    c = bands.left if left else bands.right
    return grid[r][c] >= _CORNER_FILL_MIN


def _column_count(grid: list[list[float]]) -> int:
    """Count content columns by finding empty vertical gutters in the body.

    Sums ink down each column over the body rows (excluding the top/bottom
    border bands), then counts runs of ink-bearing INTERIOR columns separated
    by an empty gutter. The left/right border verticals are excluded so they
    do not register as content columns. One contiguous interior run -> single
    column; an interior empty gutter splitting two runs -> two columns.
    """
    n = len(grid)
    body = range(4, n - 4)
    col_ink = [sum(grid[r][c] for r in body) for c in range(n)]
    # Drop the border verticals: trim a margin from each side before scanning.
    margin = max(3, n // 10)
    interior = col_ink[margin : n - margin]
    peak = max(interior) if interior else 0.0
    if peak <= 0.0:
        return 0
    threshold = peak * 0.3
    runs = 0
    in_run = False
    for v in interior:
        if v > threshold:
            if not in_run:
                runs += 1
                in_run = True
        else:
            in_run = False
    return runs


def _right_edge_aligned(grid: list[list[float]]) -> bool:
    """Whether body-row content shares a common right edge (W10 alignment).

    Scans each body row (excluding the top/bottom border bands) right-to-left
    for its rightmost interior ink cell, then reports whether those per-row
    right edges cluster within :data:`_RIGHT_EDGE_ALIGN_TOLERANCE` cells. A
    right-aligned column ends every row at the same x (tight cluster -> True);
    a left-aligned or ragged column varies (wide spread -> False). Fewer than
    two inked body rows is treated as aligned (nothing to falsify).
    """
    n = len(grid)
    threshold = 0.4
    margin = max(3, n // 10)
    right_edges: list[int] = []
    for r in range(4, n - 4):
        for c in range(n - margin - 1, margin - 1, -1):
            if grid[r][c] >= threshold:
                right_edges.append(c)
                break
    if len(right_edges) < 2:
        return True
    return (max(right_edges) - min(right_edges)) <= _RIGHT_EDGE_ALIGN_TOLERANCE


def extract_layout_features(
    grid: list[list[float]],
    *,
    selection_contrast: float | None = None,
) -> LayoutFeatures:
    """Read the layout-shape + broadened features off an ink grid.

    Args:
        grid: The ink-occupancy grid the border/column/alignment features are
            read from.
        selection_contrast: Pre-computed selected-row contrast ratio (see
            :func:`decode_png_selection_contrast`). ``None`` -- the caller has
            no luminance data -- reports the contrast feature as OK so a
            grid-only caller raises no contrast regression.
    """
    bands = _border_bands(grid)
    corners = (
        _corner_filled(grid, top=True, left=True, bands=bands),
        _corner_filled(grid, top=True, left=False, bands=bands),
        _corner_filled(grid, top=False, left=True, bands=bands),
        _corner_filled(grid, top=False, left=False, bands=bands),
    )
    contrast_ok = selection_contrast is None or selection_contrast >= _SELECTION_CONTRAST_FLOOR
    return LayoutFeatures(
        corners_filled=corners,
        column_count=_column_count(grid),
        right_edge_aligned=_right_edge_aligned(grid),
        selection_contrast_ok=contrast_ok,
    )


@dataclass(frozen=True)
class LayoutDiff:
    """The scored divergence between a mockup grid and a TUI grid.

    Attributes:
        score: Total weighted divergence; the gate fails at or above
            :data:`_FAIL_THRESHOLD`.
        border_shape_mismatch: ``True`` when corner fill (round vs square)
            differs -- the dominant layout-shape signal.
        column_count_mismatch: ``True`` when the body column count differs.
        alignment_mismatch: ``True`` when the body right-edge alignment
            differs (the W10 class) -- a secondary falsifier.
        contrast_regression: ``True`` when the mockup clears the selected-row
            contrast floor but the TUI render does not (the W33 class) -- a
            secondary falsifier.
        token_divergence: Mean per-cell ink delta NOT explained by a
            layout-shape feature -- the low-weight token-fidelity term.
        reasons: Human-readable reasons backing the score.
    """

    score: float
    border_shape_mismatch: bool
    column_count_mismatch: bool
    alignment_mismatch: bool
    contrast_regression: bool
    token_divergence: float
    reasons: tuple[str, ...]


def _fmt_contrast(value: float | None) -> str:
    """Format an optional contrast ratio for a reason string."""
    return f"{value:.2f}" if value is not None else "n/a"


def score_layout_diff(
    mockup_grid: list[list[float]],
    tui_grid: list[list[float]],
    *,
    mockup_contrast: float | None = None,
    tui_contrast: float | None = None,
) -> LayoutDiff:
    """Score mockup-vs-TUI divergence weighting layout shape over token fidelity.

    The rubric: a border-shape mismatch (round vs square corners) and a
    column-count mismatch each add :data:`_LAYOUT_WEIGHT` to the score, so a
    single layout-shape divergence crosses :data:`_FAIL_THRESHOLD` on its
    own. The broadened secondary falsifiers -- a right-edge alignment mismatch
    (W10) and a selected-row contrast regression -- each add the smaller
    :data:`_SECONDARY_WEIGHT`, which still crosses the threshold alone yet
    stays below the dominant layout-shape pair. The residual per-cell ink
    delta -- the token-fidelity term -- is averaged and added at the much
    smaller :data:`_TOKEN_WEIGHT`, so glyph noise alone cannot fail the gate.

    Args:
        mockup_grid: Ink-occupancy grid of the reference mockup frame.
        tui_grid: Ink-occupancy grid of the live-TUI screenshot.
        mockup_contrast: Selected-row contrast ratio of the mockup, or
            ``None`` when no luminance data is available.
        tui_contrast: Selected-row contrast ratio of the TUI render, or
            ``None`` when no luminance data is available.

    Returns:
        A :class:`LayoutDiff` whose ``score`` drives the pass/fail decision.

    Raises:
        ValueError: When the two grids are not the same shape.
    """
    if len(mockup_grid) != len(tui_grid) or any(
        len(a) != len(b) for a, b in zip(mockup_grid, tui_grid, strict=True)
    ):
        raise ValueError("mockup and tui grids must share shape")

    mockup_features = extract_layout_features(mockup_grid, selection_contrast=mockup_contrast)
    tui_features = extract_layout_features(tui_grid, selection_contrast=tui_contrast)

    reasons: list[str] = []
    score = 0.0

    border_mismatch = mockup_features.corners_filled != tui_features.corners_filled
    if border_mismatch:
        score += _LAYOUT_WEIGHT
        reasons.append(
            "border-shape mismatch: mockup corners_filled="
            f"{mockup_features.corners_filled} tui corners_filled="
            f"{tui_features.corners_filled} (square-vs-round)"
        )

    column_mismatch = mockup_features.column_count != tui_features.column_count
    if column_mismatch:
        score += _LAYOUT_WEIGHT
        reasons.append(
            "column-count mismatch: mockup columns="
            f"{mockup_features.column_count} tui columns={tui_features.column_count}"
        )

    alignment_mismatch = mockup_features.right_edge_aligned != tui_features.right_edge_aligned
    if alignment_mismatch:
        score += _SECONDARY_WEIGHT
        reasons.append(
            "alignment mismatch: mockup right_edge_aligned="
            f"{mockup_features.right_edge_aligned} tui right_edge_aligned="
            f"{tui_features.right_edge_aligned} (W10 right-edge)"
        )

    contrast_regression = (
        mockup_features.selection_contrast_ok and not tui_features.selection_contrast_ok
    )
    if contrast_regression:
        score += _SECONDARY_WEIGHT
        reasons.append(
            "contrast-on-selection regression: mockup_contrast="
            f"{_fmt_contrast(mockup_contrast)} tui_contrast={_fmt_contrast(tui_contrast)} "
            f"floor={_SELECTION_CONTRAST_FLOOR} (W33 dim-on-dim)"
        )

    n = len(mockup_grid)
    total_delta = sum(abs(mockup_grid[r][c] - tui_grid[r][c]) for r in range(n) for c in range(n))
    token_divergence = total_delta / (n * n)
    score += _TOKEN_WEIGHT * token_divergence
    if not reasons:
        reasons.append(f"layout shape matches; token_divergence={token_divergence:.4f}")

    return LayoutDiff(
        score=score,
        border_shape_mismatch=border_mismatch,
        column_count_mismatch=column_mismatch,
        alignment_mismatch=alignment_mismatch,
        contrast_regression=contrast_regression,
        token_divergence=token_divergence,
        reasons=tuple(reasons),
    )


def compare_mockup_png_to_tui_png(mockup_png: bytes, tui_png: bytes) -> LayoutDiff:
    """Decode both PNGs and score their layout-weighted divergence.

    Each PNG is defiltered once, yielding both its ink-occupancy grid and its
    selected-row contrast ratio, so the broadened contrast falsifier costs no
    extra decode of the (potentially large) pixel buffer.

    Args:
        mockup_png: Reference mockup frame PNG bytes.
        tui_png: Live-TUI screenshot PNG bytes.

    Returns:
        The scored :class:`LayoutDiff`.

    Raises:
        ValueError: When either PNG is malformed or not the supported shape,
            or the decoded grids do not share shape.
    """
    mockup_pixels, mockup_w, mockup_h = _decode_pixels(mockup_png)
    tui_pixels, tui_w, tui_h = _decode_pixels(tui_png)
    mockup_grid = _downsample_ink(mockup_pixels, mockup_w, mockup_h, _GRID_N)
    tui_grid = _downsample_ink(tui_pixels, tui_w, tui_h, _GRID_N)
    mockup_contrast = _selection_band_contrast(mockup_pixels, mockup_w, mockup_h)
    tui_contrast = _selection_band_contrast(tui_pixels, tui_w, tui_h)
    return score_layout_diff(
        mockup_grid,
        tui_grid,
        mockup_contrast=mockup_contrast,
        tui_contrast=tui_contrast,
    )


def layout_diff_fails(diff: LayoutDiff) -> bool:
    """Whether a scored :class:`LayoutDiff` crosses the fail threshold."""
    return diff.score >= _FAIL_THRESHOLD


__all__ = [
    "CSS_TO_TEXTUAL_MAPPING",
    "LIVE_CAPTURE_SENTINEL",
    "LayoutDiff",
    "LayoutFeatures",
    "compare_mockup_png_to_tui_png",
    "decode_png_luma_grid",
    "decode_png_selection_contrast",
    "extract_layout_features",
    "layout_diff_fails",
    "score_layout_diff",
]
