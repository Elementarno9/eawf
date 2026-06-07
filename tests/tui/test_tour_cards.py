"""Unit tests for :mod:`eawf.surfaces.tui.modals.tour_cards` (P29-I13-W26).

Covers the typed :class:`~eawf.surfaces.tui.modals.tour_cards.TourCard`
card-content model the first-run tour carousel renders and its pure
:func:`~eawf.surfaces.tui.modals.tour_cards.render_tour_card` layout helper:

- a valid card round-trips and renders title-then-body;
- boundary: a max-length title and a single-character body validate;
- error paths: an empty title / body, an over-cap title, and an extra
  field are rejected at the model boundary.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.modals.tour_cards import TourCard, render_tour_card


def test_tour_card_round_trips() -> None:
    card = TourCard(title="Welcome", body="This is the eawf dashboard.")
    assert card.title == "Welcome"
    assert card.body == "This is the eawf dashboard."


def test_render_tour_card_yields_title_then_body() -> None:
    card = TourCard(title="Modes", body="Press the digit keys to switch modes.")
    assert render_tour_card(card) == ("Modes", "Press the digit keys to switch modes.")


def test_tour_card_max_length_title_validates() -> None:
    # boundary: a 72-character title is the cap and must validate.
    title = "x" * 72
    card = TourCard(title=title, body="body")
    assert card.title == title


def test_tour_card_single_char_body_validates() -> None:
    # boundary: a one-character body is the minimum and must validate.
    card = TourCard(title="t", body="b")
    assert card.body == "b"


def test_tour_card_empty_title_rejected() -> None:
    # error-path: an empty title violates min_length=1.
    with pytest.raises(ValidationError):
        TourCard(title="", body="body")


def test_tour_card_empty_body_rejected() -> None:
    # error-path: an empty body violates min_length=1.
    with pytest.raises(ValidationError):
        TourCard(title="title", body="")


def test_tour_card_over_cap_title_rejected() -> None:
    # error-path: a 73-character title exceeds max_length=72.
    with pytest.raises(ValidationError):
        TourCard(title="x" * 73, body="body")


def test_tour_card_rejects_extra_field() -> None:
    # error-path: extra="forbid" per project-wide Pydantic rule 2.
    with pytest.raises(ValidationError):
        TourCard(title="t", body="b", unknown="oops")  # type: ignore[call-arg]


def test_tour_card_is_frozen() -> None:
    # A frozen model -- mutating an instance after construction must fail.
    card = TourCard(title="t", body="b")
    with pytest.raises(ValidationError):
        card.title = "mutated"  # type: ignore[misc]
