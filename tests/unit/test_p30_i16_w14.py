"""VIS-1: image-diff gate over mockup vs TUI renders.

The ``mockup_golden_diff`` ASCII-text mode normalises round-vs-square box
glyphs and column gutters into the same diffable text, so it passed the
P30-I15 redesign "faithfully" while the live screen was actually
square-where-round and one-column-where-two. This suite exercises the
IMAGE mode that catches those misses: it runs the kind end-to-end over
committed fixture PNG pairs and asserts a faithful pair PASSES while a
square-vs-round AND a one-vs-two-column divergence FAIL, with the rubric
weighting layout shape ABOVE token fidelity. It also asserts the committed
reference PNGs are CI-readable and the gate spec pins the CSS-to-Textual
mapping table.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.kinds.mockup_image_diff import (
    CSS_TO_TEXTUAL_MAPPING,
    compare_mockup_png_to_tui_png,
    decode_png_luma_grid,
    decode_png_selection_contrast,
    extract_layout_features,
    layout_diff_fails,
    score_layout_diff,
)

#: Repo root resolved from this test file (tests/unit/test_p30_i16_w14.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Committed, CI-readable fixture PNG pair directory. (The gitignored brand
#: corpus is NOT CI-readable, so the gate's references live here instead.)
_FIXTURES_REL = "tests/fixtures/mockup_image_diff"
_FIXTURES = _REPO_ROOT / _FIXTURES_REL

_MOCKUP = "mockup_round_1col.png"
_FAITHFUL = "tui_round_1col_faithful.png"
_SQUARE = "tui_square_1col_divergent.png"
_TWO_COL = "tui_round_2col_divergent.png"


def _run_image_check(args: dict[str, object]) -> CheckResult:
    spec = CheckSpec(kind="mockup_golden_diff", name="vis1_image", args=args)
    return CHECK_REGISTRY["mockup_golden_diff"](spec, _REPO_ROOT)


# --- committed, CI-readable reference frames ---------------------------------


def test_reference_pngs_are_committed_and_ci_readable() -> None:
    for name in (_MOCKUP, _FAITHFUL, _SQUARE, _TWO_COL):
        png = _FIXTURES / name
        assert png.is_file(), f"missing committed fixture {name}"
        data = png.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
        # Small so the large-file pre-commit hook accepts them.
        assert len(data) < 32_000, f"{name} too large for a committed fixture"


# --- end-to-end: faithful PASSES, layout divergence FAILS --------------------


def test_image_gate_passes_on_faithful_pair() -> None:
    result = _run_image_check(
        {"golden_path": f"{_FIXTURES_REL}/{_FAITHFUL}", "mockup_png": f"{_FIXTURES_REL}/{_MOCKUP}"}
    )
    assert result.status == "pass"
    assert result.passed is True
    assert result.details is not None
    assert "border_shape_mismatch=False" in result.details
    assert "column_count_mismatch=False" in result.details


def test_image_gate_fails_on_square_vs_round_border() -> None:
    result = _run_image_check(
        {"golden_path": f"{_FIXTURES_REL}/{_SQUARE}", "mockup_png": f"{_FIXTURES_REL}/{_MOCKUP}"}
    )
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "border_shape_mismatch=True" in result.details
    assert "square-vs-round" in result.details


def test_image_gate_fails_on_one_vs_two_column() -> None:
    result = _run_image_check(
        {"golden_path": f"{_FIXTURES_REL}/{_TWO_COL}", "mockup_png": f"{_FIXTURES_REL}/{_MOCKUP}"}
    )
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "column_count_mismatch=True" in result.details
    assert "tui columns=2" in result.details


# --- rubric: layout shape dominates token fidelity ---------------------------


def test_rubric_weights_layout_shape_above_token_fidelity() -> None:
    mockup = (_FIXTURES / _MOCKUP).read_bytes()
    faithful_diff = compare_mockup_png_to_tui_png(mockup, (_FIXTURES / _FAITHFUL).read_bytes())
    square_diff = compare_mockup_png_to_tui_png(mockup, (_FIXTURES / _SQUARE).read_bytes())

    # A single layout-shape mismatch outscores the entire token-noise budget:
    # the square pair fails purely on border shape, and its score dwarfs the
    # tiny token divergence between the two near-identical frames.
    assert square_diff.border_shape_mismatch is True
    assert square_diff.score >= 1.0
    assert square_diff.token_divergence < 0.1
    assert square_diff.score > 4 * square_diff.token_divergence

    # The faithful pair has near-zero layout divergence and PASSES.
    assert faithful_diff.border_shape_mismatch is False
    assert faithful_diff.column_count_mismatch is False
    assert layout_diff_fails(faithful_diff) is False
    assert layout_diff_fails(square_diff) is True


def test_layout_features_distinguish_round_from_square() -> None:
    round_grid = decode_png_luma_grid((_FIXTURES / _MOCKUP).read_bytes())
    square_grid = decode_png_luma_grid((_FIXTURES / _SQUARE).read_bytes())
    round_feats = extract_layout_features(round_grid)
    square_feats = extract_layout_features(square_grid)
    # Round border clips the extreme corner cell; square fills it.
    assert round_feats.corners_filled == (False, False, False, False)
    assert square_feats.corners_filled == (True, True, True, True)
    assert round_feats.column_count == 1
    assert square_feats.column_count == 1


def test_layout_features_distinguish_one_from_two_columns() -> None:
    one_col = extract_layout_features(decode_png_luma_grid((_FIXTURES / _MOCKUP).read_bytes()))
    two_col = extract_layout_features(decode_png_luma_grid((_FIXTURES / _TWO_COL).read_bytes()))
    assert one_col.column_count == 1
    assert two_col.column_count == 2


# --- CSS-to-Textual mapping table is pinned (un-renderable claim must cite) ---


def test_css_to_textual_mapping_pins_round_and_two_column_rows() -> None:
    keys = list(CSS_TO_TEXTUAL_MAPPING)
    assert any("border-radius" in k for k in keys)
    assert "border: round" in CSS_TO_TEXTUAL_MAPPING["border-radius:<r>"]
    two_col_row = next(v for k, v in CSS_TO_TEXTUAL_MAPPING.items() if "2 cols" in k)
    assert "two child" in two_col_row.lower() or "two children" in two_col_row.lower()


def test_gate_spec_pins_css_to_textual_table() -> None:
    spec = _REPO_ROOT / "tests/fixtures/mockup_image_diff/VIS1_gate_spec.md"
    assert spec.is_file(), "gate spec pinning the CSS-to-Textual table must be committed"
    body = spec.read_text(encoding="utf-8")
    assert "border-radius" in body
    assert "border: round" in body
    assert "un-renderable" in body.lower()


# --- never raises: a malformed fixture degrades to fail -----------------------


def test_image_mode_missing_mockup_png_fails_not_raises() -> None:
    result = _run_image_check(
        {"golden_path": f"{_FIXTURES_REL}/{_FAITHFUL}", "mockup_png": f"{_FIXTURES_REL}/nope.png"}
    )
    assert result.status == "fail"
    assert "mockup_png" in (result.details or "")


def test_image_mode_corrupt_png_fails_not_raises(tmp_path: Path) -> None:
    bad = tmp_path / "tests/fixtures/mockup_image_diff"
    bad.mkdir(parents=True)
    (bad / "bad.png").write_bytes(b"not a png")
    spec = CheckSpec(
        kind="mockup_golden_diff",
        name="vis1_image",
        args={
            "golden_path": "tests/fixtures/mockup_image_diff/bad.png",
            "mockup_png": "tests/fixtures/mockup_image_diff/bad.png",
        },
    )
    result = CHECK_REGISTRY["mockup_golden_diff"](spec, tmp_path)
    assert result.status == "fail"
    assert "image diff failed" in (result.details or "")


def test_score_layout_diff_rejects_mismatched_grid_shapes() -> None:
    small = [[0.0, 0.0], [0.0, 0.0]]
    big = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    with pytest.raises(ValueError, match="share shape"):
        score_layout_diff(small, big)


# --- the text mode is untouched (regression guard) ---------------------------


def test_mockup_golden_diff_still_registered_at_t5() -> None:
    assert "mockup_golden_diff" in CHECK_REGISTRY
    assert _tier_for_gate_kind("mockup_golden_diff") is OracleTier.T5_GOLDEN


# --- W30: broadened falsifiers (contrast-on-selection + right-edge alignment) ---
#
# These fixtures are synthesised in-process as tiny dependency-free RGBA PNGs
# (no resvg, no committed binaries) so the two new LayoutFeatures falsify on
# every host: each pair holds border / column / (the non-target feature) FIXED
# so only the feature under test diverges.

_FRAME_W = 160
_FRAME_H = 100
_WHITE = (245, 245, 245, 255)
_BORDER_INK = (20, 20, 20, 255)
_BODY_INK = (40, 40, 40, 255)


def _encode_rgba_png(pixels: bytearray, width: int, height: int) -> bytes:
    """Encode raw RGBA pixels to an 8-bit non-interlaced PNG (filter 0 rows)."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: none
        raw += pixels[y * stride : (y + 1) * stride]

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _blank_frame() -> bytearray:
    px = bytearray()
    for _ in range(_FRAME_W * _FRAME_H):
        px += bytes(_WHITE)
    return px


def _fill_rect(
    px: bytearray, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int, int]
) -> None:
    for y in range(y0, y1):
        for x in range(x0, x1):
            off = (y * _FRAME_W + x) * 4
            px[off : off + 4] = bytes(color)


def _draw_border(px: bytearray, thickness: int = 4) -> None:
    _fill_rect(px, 0, 0, _FRAME_W, thickness, _BORDER_INK)
    _fill_rect(px, 0, _FRAME_H - thickness, _FRAME_W, _FRAME_H, _BORDER_INK)
    _fill_rect(px, 0, 0, thickness, _FRAME_H, _BORDER_INK)
    _fill_rect(px, _FRAME_W - thickness, 0, _FRAME_W, _FRAME_H, _BORDER_INK)


def _contrast_frame(text: tuple[int, int, int, int], fill: tuple[int, int, int, int]) -> bytes:
    """A bordered single-column frame with a highlighted selected-row band."""
    px = _blank_frame()
    _draw_border(px)
    _fill_rect(px, 8, 44, _FRAME_W - 8, 56, fill)  # selected-row highlight band
    for x in range(20, 140, 10):  # sparse "glyph" columns of selected-row text
        _fill_rect(px, x, 47, x + 3, 53, text)
    return _encode_rgba_png(px, _FRAME_W, _FRAME_H)


def _alignment_frame(right_edges: list[int]) -> bytes:
    """A bordered single-column frame; each body row ends at its given x edge."""
    px = _blank_frame()
    _draw_border(px)
    y = 20
    for edge in right_edges:
        _fill_rect(px, edge - 40, y, edge, y + 3, _BODY_INK)
        y += 8
    return _encode_rgba_png(px, _FRAME_W, _FRAME_H)


#: White text on a dark-blue highlight -- a legible selected row (WCAG ~4.5).
_FAITHFUL_CONTRAST = _contrast_frame(text=(250, 250, 250, 255), fill=(30, 40, 110, 255))
#: Dim grey text on a grey highlight -- the W33-class low-contrast regression.
_LOW_CONTRAST = _contrast_frame(text=(120, 120, 120, 255), fill=(85, 85, 85, 255))
#: Every body row ends at the same x -- a faithful right-aligned column.
_RIGHT_ALIGNED = _alignment_frame([_FRAME_W - 12] * 7)
#: Ragged right edges -- the W10-class alignment regression.
_MISALIGNED = _alignment_frame(
    [_FRAME_W - 12, _FRAME_W - 45, _FRAME_W - 22, _FRAME_W - 60, _FRAME_W - 15, _FRAME_W - 52]
)


def test_contrast_on_selection_feature_reads_luminance_ratio() -> None:
    faithful = decode_png_selection_contrast(_FAITHFUL_CONTRAST)
    low = decode_png_selection_contrast(_LOW_CONTRAST)
    # The legible selected row clears the floor; the dim-on-dim one does not.
    assert faithful >= 2.5
    assert low < 2.5
    assert faithful > low


def test_contrast_on_selection_feature_falsifies_low_contrast_render() -> None:
    diff = compare_mockup_png_to_tui_png(_FAITHFUL_CONTRAST, _LOW_CONTRAST)
    assert diff.contrast_regression is True
    # Only contrast diverges: border / column / alignment stay matched.
    assert diff.border_shape_mismatch is False
    assert diff.column_count_mismatch is False
    assert diff.alignment_mismatch is False
    assert layout_diff_fails(diff) is True
    assert any("contrast-on-selection" in reason for reason in diff.reasons)


def test_contrast_on_selection_feature_passes_faithful_render() -> None:
    diff = compare_mockup_png_to_tui_png(_FAITHFUL_CONTRAST, _FAITHFUL_CONTRAST)
    assert diff.contrast_regression is False
    assert layout_diff_fails(diff) is False


def test_alignment_feature_falsifies_misaligned_render() -> None:
    diff = compare_mockup_png_to_tui_png(_RIGHT_ALIGNED, _MISALIGNED)
    assert diff.alignment_mismatch is True
    # Only alignment diverges: border / column / contrast stay matched.
    assert diff.border_shape_mismatch is False
    assert diff.column_count_mismatch is False
    assert diff.contrast_regression is False
    assert layout_diff_fails(diff) is True
    assert any("alignment mismatch" in reason for reason in diff.reasons)


def test_alignment_feature_passes_faithful_render() -> None:
    diff = compare_mockup_png_to_tui_png(_RIGHT_ALIGNED, _RIGHT_ALIGNED)
    assert diff.alignment_mismatch is False
    assert layout_diff_fails(diff) is False


def test_layout_features_expose_broadened_fields() -> None:
    aligned = extract_layout_features(
        decode_png_luma_grid(_RIGHT_ALIGNED),
        selection_contrast=decode_png_selection_contrast(_RIGHT_ALIGNED),
    )
    misaligned = extract_layout_features(
        decode_png_luma_grid(_MISALIGNED),
        selection_contrast=decode_png_selection_contrast(_MISALIGNED),
    )
    low = extract_layout_features(
        decode_png_luma_grid(_LOW_CONTRAST),
        selection_contrast=decode_png_selection_contrast(_LOW_CONTRAST),
    )
    assert aligned.right_edge_aligned is True
    assert misaligned.right_edge_aligned is False
    assert low.selection_contrast_ok is False


def test_layout_shape_weight_dominates_broadened_falsifiers() -> None:
    # A pure border (layout-shape) divergence must outscore a pure secondary
    # divergence, so layout shape stays the dominant signal.
    border_diff = compare_mockup_png_to_tui_png(
        (_FIXTURES / _MOCKUP).read_bytes(), (_FIXTURES / _SQUARE).read_bytes()
    )
    contrast_diff = compare_mockup_png_to_tui_png(_FAITHFUL_CONTRAST, _LOW_CONTRAST)
    alignment_diff = compare_mockup_png_to_tui_png(_RIGHT_ALIGNED, _MISALIGNED)

    assert border_diff.border_shape_mismatch is True
    assert border_diff.score > contrast_diff.score
    assert border_diff.score > alignment_diff.score


def test_grid_only_score_raises_no_secondary_falsifier() -> None:
    # A caller with no luminance data (grid-only) never flags a contrast
    # regression: the feature defaults to OK.
    grid = decode_png_luma_grid(_LOW_CONTRAST)
    diff = score_layout_diff(grid, grid)
    assert diff.contrast_regression is False
    assert diff.alignment_mismatch is False


def test_image_gate_reports_contrast_regression_end_to_end(tmp_path: Path) -> None:
    fixtures = tmp_path / _FIXTURES_REL
    fixtures.mkdir(parents=True)
    (fixtures / "faithful_contrast.png").write_bytes(_FAITHFUL_CONTRAST)
    (fixtures / "low_contrast.png").write_bytes(_LOW_CONTRAST)
    spec = CheckSpec(
        kind="mockup_golden_diff",
        name="vis1_contrast",
        args={
            "golden_path": f"{_FIXTURES_REL}/low_contrast.png",
            "mockup_png": f"{_FIXTURES_REL}/faithful_contrast.png",
        },
    )
    result = CHECK_REGISTRY["mockup_golden_diff"](spec, tmp_path)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "contrast_regression=True" in result.details
