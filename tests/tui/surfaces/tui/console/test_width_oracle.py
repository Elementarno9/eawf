"""The console width oracle gives every ratified glyph a known width class.

A Wide or Fullwidth glyph is rejected, each Ambiguous glyph in the token set resolves to
one cell only because the console declares the narrow policy, padding and clipping
measure in cells, and no console module measures glyph-bearing text with ``len()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import tokens, width
from eawf.surfaces.tui.console.tokens import Glyph, truth_cell
from eawf.surfaces.tui.console.width import (
    AmbiguousWidth,
    EastAsianWidth,
    assert_known_width,
    cell_len,
    clip_words,
    eaw_class,
    pad,
    rule,
)

CONSOLE_DIR = Path(width.__file__).resolve().parent
CHASSIS = "tests.snapshots.tui.console.console_chassis.chassis"

RATIFIED = sorted(tokens.all_glyphs())
AMBIGUOUS = [ch for ch in RATIFIED if eaw_class(ch) == EastAsianWidth.AMBIGUOUS]
TABLES = {"connection": tokens.CONNECTION, "truth": tokens.TRUTH, "quality": tokens.QUALITY}

# Names that hold frame text in console code; len() over one counts code points, not cells.
_TEXT_NAMES = frozenset(
    {
        "bc",
        "crumb",
        "ctx",
        "head",
        "label",
        "line",
        "plain",
        "raw",
        "right",
        "row",
        "s",
        "text",
        "title",
        "value",
        "why",
    }
)


def _len_over_text(tree: ast.AST) -> list[int]:
    """Return the lines where ``len()`` measures a string literal or a frame-text name."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "len"
            and node.args
        ):
            continue
        arg = node.args[0]
        literal = isinstance(arg, ast.JoinedStr) or (
            isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        )
        if literal or ast.unparse(arg) in _TEXT_NAMES:
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("ch", RATIFIED)
def test_eaw_class_ratified_glyph_is_known_and_one_cell(ch: str) -> None:
    assert eaw_class(ch) in {
        EastAsianWidth.NARROW,
        EastAsianWidth.NEUTRAL,
        EastAsianWidth.HALFWIDTH,
        EastAsianWidth.AMBIGUOUS,
    }
    assert_known_width(ch)
    assert cell_len(ch) == 1


def test_all_glyphs_ambiguous_subset_is_the_declared_list() -> None:
    # the narrow policy is what admits these, so the list is pinned rather than inferred
    assert "".join(AMBIGUOUS) == "≈─│┄═▲◇◈●◐◑"
    assert width.POLICY == AmbiguousWidth.NARROW


@pytest.mark.parametrize("ch", AMBIGUOUS)
def test_assert_known_width_ambiguous_glyph_resolves_only_under_narrow_policy(ch: str) -> None:
    assert_known_width(ch, policy=AmbiguousWidth.NARROW)
    assert cell_len(ch) == 1
    with pytest.raises(ValueError, match="Ambiguous under the wide policy"):
        assert_known_width(ch, policy=AmbiguousWidth.WIDE)


@pytest.mark.parametrize("ch", [c for c in RATIFIED if c not in AMBIGUOUS])
def test_assert_known_width_unambiguous_glyph_passes_any_policy(ch: str) -> None:
    assert_known_width(ch, policy=AmbiguousWidth.WIDE)


def test_assert_known_width_wide_hourglass_rejected() -> None:
    assert eaw_class("⏳") == EastAsianWidth.WIDE
    assert cell_len("⏳") == 2
    with pytest.raises(ValueError, match=r"U\+23F3.*two cells"):
        assert_known_width("⏳")


def test_assert_known_width_fullwidth_glyph_rejected() -> None:
    fullwidth_a = "\uff21"
    assert eaw_class(fullwidth_a) == EastAsianWidth.FULLWIDTH
    with pytest.raises(ValueError, match="East-Asian-Width F: two cells"):
        assert_known_width(fullwidth_a)


@pytest.mark.parametrize("text", ["", "ab"])
def test_eaw_class_not_one_character_raises_type_error(text: str) -> None:
    with pytest.raises(TypeError):
        eaw_class(text)


# the en dash is escaped because it is the one glyph here a reader cannot tell from a hyphen
@pytest.mark.parametrize("ch", "─═┄·│●↑↓\u2013…—○→¶▏≈")
def test_cell_len_frame_rule_and_separator_glyphs_are_one_cell(ch: str) -> None:
    assert cell_len(ch) == 1
    assert eaw_class(ch) in {EastAsianWidth.AMBIGUOUS, EastAsianWidth.NEUTRAL}


@pytest.mark.parametrize(
    ("table", "name"), [(t, n) for t, entries in TABLES.items() for n in entries]
)
def test_glyph_ascii_twin_is_ascii_and_same_cells(table: str, name: str) -> None:
    glyph = TABLES[table][name]
    assert glyph.ascii.isascii()
    assert cell_len(glyph.ascii) == cell_len(glyph.unicode)


def test_glyph_non_ascii_twin_rejected() -> None:
    with pytest.raises(ValueError, match="is not ASCII"):
        Glyph("●", "•")


def test_glyph_twin_of_other_width_rejected() -> None:
    with pytest.raises(ValueError, match="same cells"):
        Glyph("●", "**")


def test_all_glyphs_covers_every_table_and_chrome_glyph() -> None:
    glyphs = tokens.all_glyphs()
    assert {"●", "∅", "≈", "▸", "ä", "═", "─", "┄", "│"} <= glyphs
    assert all(not ch.isascii() for ch in glyphs)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (None, ""),
        ("unknown", "?"),
        ("unavailable", "∅"),
        ("denied", "⊘"),
        ("failed", "✗"),
        ("attention", "!"),
        ("zero", "0"),
    ],
)
def test_truth_cell_renders_its_token_or_nothing(field: str | None, expected: str) -> None:
    assert truth_cell(field) == expected


def test_truth_cell_unknown_token_raises_key_error() -> None:
    with pytest.raises(KeyError):
        truth_cell("stale")


def test_truth_cell_empty_token_raises_key_error() -> None:
    """The empty boundary: a blank name is a missing name, not the never-acted case."""
    with pytest.raises(KeyError):
        truth_cell("")


@pytest.mark.parametrize(
    ("text", "n", "expected"),
    [
        ("final report rejected by the jury", 20, "final report…"),
        ("abcdefghijklmnopqrstuvwxyz", 10, "abcdefghi…"),
        ("short", 10, "short"),
        ("exact", 5, "exact"),
        ("exactly", 6, "exact…"),
        ("ab", 1, "…"),
        ("ab", 0, ""),
        ("", 0, ""),
        ("日本語", 4, "日…"),
    ],
)
def test_clip_words_cuts_at_a_word_within_n_cells(text: str, n: int, expected: str) -> None:
    clipped = clip_words(text, n)
    assert clipped == expected
    assert cell_len(clipped) <= n


def test_clip_words_negative_width_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        clip_words("x", -1)


@pytest.mark.parametrize(
    ("text", "w"),
    [
        ("ab", 5),
        ("final report rejected by the jury", 20),
        ("", 0),
        ("ab", 0),
        ("abc", 3),
        ("日本語テキスト", 5),
        ("● LIVE ═ ≈12", 40),
    ],
)
def test_pad_returns_exactly_w_cells(text: str, w: int) -> None:
    assert cell_len(pad(text, w)) == w


def test_pad_short_value_is_right_padded() -> None:
    assert pad("ab", 5) == "ab   "


def test_pad_negative_width_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        pad("x", -1)


def test_rule_repeats_one_cell_glyph_across_the_width() -> None:
    assert rule(tokens.RULE_HEAVY, 3) == "═══"
    assert rule(tokens.RULE_THIN, 0) == ""


@pytest.mark.parametrize(("glyph", "w"), [("ab", 3), ("⏳", 2), ("", 2)])
def test_rule_glyph_not_one_cell_raises_value_error(glyph: str, w: int) -> None:
    with pytest.raises(ValueError, match="exactly one cell"):
        rule(glyph, w)


def test_rule_negative_width_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        rule("-", -1)


def test_len_over_text_scan_flags_code_point_padding() -> None:
    # the scan must red on the defect it exists for, or its green result proves nothing
    bad = ast.parse('def pad(text, w):\n    return text + " " * (w - len(text))\n')
    assert _len_over_text(bad) == [2]
    assert _len_over_text(ast.parse("n = len(rows)\n")) == []


def test_console_modules_never_measure_frame_text_with_len() -> None:
    offenders = {
        path.relative_to(CONSOLE_DIR).as_posix(): hits
        for path in sorted(CONSOLE_DIR.rglob("*.py"))
        if (hits := _len_over_text(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}


def test_width_oracle_matches_the_test_only_chassis_on_narrow_text() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    chassis_width = pytest.importorskip(f"{CHASSIS}.width", exc_type=ModuleNotFoundError)
    chassis_tokens = pytest.importorskip(f"{CHASSIS}.tokens", exc_type=ModuleNotFoundError)
    samples = ["", "ab", "short", "final report rejected by the jury", "● LIVE ▸ ≈12 ═══"]
    for text in samples:
        for n in (1, 2, 5, 10, 20, 40):
            assert clip_words(text, n) == chassis_width.clip_words(text, n)
            assert pad(text, n) == chassis_width.pad(text, n)
    for name, table in TABLES.items():
        theirs = getattr(chassis_tokens, name.upper())
        assert {k: (g.unicode, g.ascii) for k, g in table.items()} == {
            k: (g.unicode, g.ascii) for k, g in theirs.items()
        }
    assert tokens.all_glyphs() == chassis_tokens.all_glyphs()
