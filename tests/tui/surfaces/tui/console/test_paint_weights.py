"""The painter's weights: which runs of a row are bold, and which keep their own weight.

A truth token keeps the weight of its mark whatever the heavy classes around it take; a
row of column heads is bold wherever it sits, inside a boxed card or split by a middle dot,
and says no state; and a pane label is bold on a row that ends in a state word.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.paint import Part, Stroke, paint


def _strokes(row: str) -> dict[str, Stroke]:
    return {s.text.strip(): s for s in paint(row, Part.BODY) if s.text.strip()}


# ---------- a truth token keeps its own weight and the pack's tone ----------


def test_unknown_is_drawn_in_the_info_tone_and_not_bold() -> None:
    unknown = _strokes(" PROGRESS    ? unknown")["? unknown"]
    assert (unknown.surface, unknown.bold) == ("info", False)


@pytest.mark.parametrize(("token", "surface"), [("⊘ denied", "warn"), ("! invalidated", "err")])
def test_a_denial_and_an_invalidation_keep_their_severity_at_the_mark_weight(
    token: str, surface: str
) -> None:
    stroke = _strokes(f"   READINESS    {token}")[token]
    assert (stroke.surface, stroke.bold) == (surface, False)


def test_a_state_word_beside_a_truth_token_stays_bold() -> None:
    strokes = _strokes(" ▸ RUN-00000003     EAWF-0147     RUNNING      ? unknown")
    assert strokes["RUNNING"].bold
    assert not strokes["? unknown"].bold


# ---------- heads and labels inside a boxed card ----------


def test_column_heads_inside_a_box_are_bold() -> None:
    row = "│ CLASS               TOAST               DECIDED BY                   │"
    heads = _strokes(row)["CLASS               TOAST               DECIDED BY"]
    assert heads.bold


@pytest.mark.parametrize(
    ("row", "label"),
    [
        ("│ KEY        planning.approval                      │", "KEY"),
        ("│ LENS SETS  repo sets nothing here                 │", "LENS SETS"),
        ("│ INPUT DIGEST  ? unknown · no producer states it   │", "INPUT DIGEST"),
    ],
)
def test_a_pane_label_inside_a_box_is_bold(row: str, label: str) -> None:
    assert _strokes(row)[label].bold


def test_the_box_edges_stay_in_the_rail_tone() -> None:
    edges = [s for s in paint("│ KEY       planning.approval  │", Part.BODY) if s.text == "│"]
    assert [(s.surface, s.bold) for s in edges] == [("rail", False), ("rail", False)]


# ---------- a label on a row that ends in a state word ----------


def test_the_label_of_a_chip_row_is_bold_and_keeps_its_chip() -> None:
    strokes = _strokes("  BATCHES    ▸ BAT-0101 · ACTIVE")
    assert strokes["BATCHES"].bold
    assert (strokes["ACTIVE"].surface, strokes["ACTIVE"].bold) == ("ok", True)


# ---------- a head holding a middle dot ----------


def test_every_head_of_a_row_is_bold_when_one_head_holds_a_middle_dot() -> None:
    row = "   CHECK        RESULT     REASON · LAST RESULT          ANSWERED BY    "
    heads = _strokes(row)
    assert list(heads) == ["CHECK        RESULT     REASON · LAST RESULT          ANSWERED BY"]
    assert all(stroke.bold for stroke in heads.values())


def test_a_label_and_its_prose_value_are_not_read_as_heads() -> None:
    strokes = _strokes(" CHECKS       0 checks · 0 failed · 0 warn")
    assert strokes["CHECKS"].bold
    assert not any(s.bold for text, s in strokes.items() if text != "CHECKS")


# ---------- a head row names columns, never a state ----------


def test_a_status_word_among_the_heads_takes_no_colour() -> None:
    strokes = _strokes("    AGENT           ACCEPTED   REJECTED   RATE")
    assert [(s.surface, s.bold) for s in strokes.values()] == [(None, True)]


def test_the_same_word_in_a_value_row_keeps_its_colour() -> None:
    assert _strokes("   EAWF-0101 · REJECTED")["REJECTED"].surface == "err"
