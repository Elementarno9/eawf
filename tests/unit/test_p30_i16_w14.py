"""VIS-1 (P30-I16-W14): image-diff gate over mockup vs TUI renders.

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

from pathlib import Path

import pytest

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.kinds.mockup_image_diff import (
    CSS_TO_TEXTUAL_MAPPING,
    compare_mockup_png_to_tui_png,
    decode_png_luma_grid,
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
