"""The console draws the packet's weights: its heavy classes and its heads are bold.

The packet's stylesheet sets ``.ok``, ``.info``, ``.wn``, ``.er`` and the accent ``.br`` at
weight 600 and the brand at 700 (``stages/stage.css``), and its row painter bolds column
heads and pane labels by position, the pane right of a rail included. Colour alone left
a state word, a warn token and the settings heads at the body weight.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.paint import Part, paint


def _bold(row: str) -> dict[str, bool]:
    return {
        stroke.text.strip(): stroke.bold for stroke in paint(row, Part.BODY) if stroke.text.strip()
    }


@pytest.mark.parametrize(
    "word", ["COMPLETED", "ACTIVE", "PLANNED", "FAILED", "WAIT-USER", "? unknown", "⊘ denied"]
)
def test_a_heavy_class_is_drawn_bold(word: str) -> None:
    assert _bold(f"    MLS-0100 Close out the milestone      {word}")[word]


@pytest.mark.parametrize("word", ["∅ unavailable", "✗ purged"])
def test_a_dim_token_stays_at_the_body_weight(word: str) -> None:
    assert not _bold(f"    MLS-0100 Close out the milestone      {word}")[word]


def test_heads_right_of_the_rail_are_bold_whatever_the_rail_pane_holds() -> None:
    row = "   agents         │     KEY                               VALUE              FROM"
    assert _bold(row)["KEY                               VALUE              FROM"]


def test_the_section_name_right_of_the_rail_is_bold() -> None:
    row = "   agents         │  PLANNING                        global › workspace › repo"  # noqa: RUF001
    assert _bold(row)["PLANNING"]


def test_rules_rails_and_plain_prose_stay_at_the_body_weight() -> None:
    weights = _bold("   docs           ├──────────────  approval · literal")
    assert not any(weights.values())
