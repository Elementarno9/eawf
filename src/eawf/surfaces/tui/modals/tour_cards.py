"""``TourCard`` -- the card content shape the first-run tour carousel renders.

The first-run onboarding tour (the W27
:class:`~eawf.surfaces.tui.modals.tour.TourModal`) walks the operator through
a short ordered carousel of cards: each card is a titled body paragraph the
modal lays out one card at a time, advancing on the next key and dismissing
on the close key (which sets the ``ui.tour_completed`` config leaf so the
tour does not re-open).

This module hosts only the typed card-content shape so it is importable
without mounting Textual; the carousel modal builds thin over a tuple of
:class:`TourCard`. The model holds no behaviour -- it carries the title +
body the modal renders.
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

logger = logging.getLogger(__name__)


class TourCard(BaseModel):
    """One card in the first-run onboarding tour carousel.

    Attributes:
        title: The card's short headline, rendered bold at the top of the
            card. Bounded so it stays on one line in the modal frame.
        body: The card's body paragraph -- the one explanatory block the
            operator reads before advancing to the next card.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, StringConstraints(min_length=1, max_length=72)]
    body: Annotated[str, StringConstraints(min_length=1, max_length=500)]


def render_tour_card(card: TourCard) -> tuple[str, ...]:
    """Return the rendered lines for *card* (title row, then body).

    Lays the card out as its bold title followed by its body paragraph so
    the modal yields one ``Static`` per line. Pure -- no Textual mount -- so
    the layout is unit-testable on its own.

    Args:
        card: The tour card to render.

    Returns:
        The card's lines in render order (title first, then body).
    """
    return (card.title, card.body)


__all__ = [
    "TourCard",
    "render_tour_card",
]
