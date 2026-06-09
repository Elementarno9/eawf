"""``TourModal`` -- the first-run onboarding tour carousel.

A small centred :class:`~textual.screen.ModalScreen` that walks the operator
through a short ordered deck of :class:`~eawf.surfaces.tui.modals.tour_cards.TourCard`
cards, one card at a time. The advance key (``right`` / ``space``) steps
forward through the deck; the dismiss key (``escape`` / ``q``) closes the
tour and persists the ``ui.tour_completed`` config leaf so the tour does not
re-open on the next launch.

The deck and the index advancement are computed by pure module functions
(:func:`default_tour_deck` / :func:`advance_index`) so they are unit-testable
without mounting Textual; the screen is a thin carousel view over them. The
modal holds no domain logic beyond walking the deck and persisting the
completed flag on dismissal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.modals.tour_cards import TourCard, render_tour_card
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: The config leaf the dismiss path persists so the tour does not re-open.
TOUR_COMPLETED_KEY: str = "ui.tour_completed"

#: The default first-run deck -- a short ordered walk through the dashboard's
#: top-level concepts, written in the cosmic-terminal reskin language so the
#: operator meets the sigil + green vocabulary on first launch. Kept brief so
#: the operator can step through it fast.
_DEFAULT_DECK: tuple[TourCard, ...] = (
    TourCard(
        title="Welcome to Ea",
        body=(
            "This is the eawf dashboard. It shows your phases, waves, and what needs you, "
            "lit in the cosmic-terminal green."
        ),
    ),
    TourCard(
        title="Sigils",
        body=(
            "Lifecycle marks lead each row: a hollow ring is pending, a half-filled ring "
            "claimed, a diamond running, a filled circle closed, a cross failed. The green "
            "accent threads the live state."
        ),
    ),
    TourCard(
        title="Modes",
        body="Press the digit keys 1-8 to switch modes -- Home, Autopilot, Trust, and more.",
    ),
    TourCard(
        title="Help",
        body="Press ? any time for the full keymap. Press / for the command palette.",
    ),
)


def default_tour_deck() -> tuple[TourCard, ...]:
    """Return the default first-run tour deck (one card per concept)."""
    return _DEFAULT_DECK


def advance_index(index: int, deck_size: int) -> int:
    """Return the next card index, clamped at the last card.

    The tour is a forward-only walk: the advance key steps toward the last
    card and stops there (it does not wrap), so the operator reads the deck
    once and dismisses from the end. An *index* already at the last card
    stays put.

    Args:
        index: The current 0-based card index.
        deck_size: The number of cards in the deck (must be positive).

    Returns:
        ``min(index + 1, deck_size - 1)`` -- the next card, clamped.

    Raises:
        ValueError: When *deck_size* is not positive (an empty deck has no
            card to advance to).
    """
    if deck_size <= 0:
        raise ValueError(f"deck_size must be positive: {deck_size!r}")
    return min(index + 1, deck_size - 1)


class TourModal(ModalScreen[None]):
    """First-run onboarding carousel: walk the deck, persist completed on dismiss.

    Renders one :class:`~eawf.surfaces.tui.modals.tour_cards.TourCard` at a
    time. The advance key (``right`` / ``space``) steps forward through the
    deck (clamping at the last card); the dismiss key (``escape`` / ``q``)
    closes the tour and persists :data:`TOUR_COMPLETED_KEY`. Built thin over
    the pure :func:`advance_index` helper so the walk is testable.
    """

    #: One tour at a time -- a re-fired tour over an already-open tour is
    #: deduped by :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` on the key.
    dedupe_singleton: ClassVar[bool] = True

    DEFAULT_CSS: ClassVar[str] = """
    TourModal {
        align: center middle;
    }
    TourModal > #tour-box {
        width: 70%;
        max-width: 80;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    TourModal .tour-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    TourModal .tour-body {
        height: auto;
        color: $text;
        margin-top: 1;
    }
    TourModal .tour-progress {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    TourModal .tour-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``right`` / ``space`` advance; ``escape`` / ``q`` dismiss (+ persist).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("right", "next", "next", show=False),
        Binding("space", "next", "next", show=False),
        Binding("escape", "dismiss_tour", "dismiss", show=False),
        Binding("q", "dismiss_tour", "dismiss", show=False),
    ]

    #: Index of the displayed card; the watcher repaints on a move.
    index: reactive[int] = reactive(0)

    def __init__(self, deck: Sequence[TourCard] | None = None) -> None:
        """Construct the tour over *deck* (defaults to the first-run deck).

        Args:
            deck: The ordered cards to walk; ``None`` uses
                :func:`default_tour_deck`. An empty deck is rejected -- a
                tour with no card has nothing to show.

        Raises:
            ValueError: When *deck* is supplied empty.
        """
        super().__init__()
        cards = tuple(default_tour_deck() if deck is None else deck)
        if not cards:
            raise ValueError("tour deck must be non-empty")
        self._deck = cards
        #: Dedupe key so the App push chokepoint suppresses a duplicate tour.
        self.dedupe_key = "tour"

    def compose(self) -> ComposeResult:
        """Yield the carousel card slot, the progress line, and the hint."""
        with VerticalScroll(id="tour-box"):
            yield Static("", classes="tour-title", id="tour-title")
            yield Static("", classes="tour-body", id="tour-body")
            yield Static("", classes="tour-progress", id="tour-progress")
            yield Static(
                "[ Right/Space next - Esc/q dismiss ]",
                classes="tour-hint",
            )

    def on_mount(self) -> None:
        """Paint the first card on mount."""
        self._repaint_card()

    def watch_index(self) -> None:
        """Repaint the displayed card when the index moves."""
        if self.is_mounted:
            self._repaint_card()

    def _repaint_card(self) -> None:
        """Update the title / body / progress for the current card.

        The title leads with the shared dispatch sigil
        (:func:`~eawf.surfaces.tui.widgets.sigils.chrome`) tinted ``$accent``
        green so the carousel reads in the cosmic-terminal reskin language;
        the card body is escaped so an arbitrary card text renders verbatim
        through Textual content markup.
        """
        cursor = chrome("dispatch", mode=getattr(self.app, "render_mode", DEFAULT_RENDER_MODE))
        title, body = render_tour_card(self._deck[self.index])
        self.query_one("#tour-title", Static).update(f"[$accent]{cursor}[/] {escape_markup(title)}")
        self.query_one("#tour-body", Static).update(body)
        self.query_one("#tour-progress", Static).update(
            f"card {self.index + 1} of {len(self._deck)}"
        )

    def action_next(self) -> None:
        """Advance to the next card (clamped at the last card)."""
        self.index = advance_index(self.index, len(self._deck))

    def action_dismiss_tour(self) -> None:
        """Persist the completed flag, then close the tour."""
        self._persist_completed()
        logger.info(f"tour_dismiss card={self.index + 1} cards={len(self._deck)}")
        self.dismiss(None)

    def _persist_completed(self) -> None:
        """Persist :data:`TOUR_COMPLETED_KEY` so the tour does not re-open.

        Best-effort: routes through the daemon-mediated layered writer (the
        same mutator the config modal uses). On any failure the tour still
        closes -- a non-persisted flag only means the tour may re-open, not a
        broken dismissal -- and the failure is logged + toasted.
        """
        from eawf.kernel.config.layered import global_config_path
        from eawf.surfaces.cli.commands.config import _save_value_to_layer

        try:
            _save_value_to_layer(
                target_path=global_config_path(), key=TOUR_COMPLETED_KEY, value=True
            )
        except Exception as exc:
            logger.warning(f"_persist_completed not saved exc={exc!r}")
            notify = getattr(self.app, "notify", None)
            if callable(notify):
                notify(f"tour dismissed (not saved: {exc})", severity="warning")


__all__ = [
    "TOUR_COMPLETED_KEY",
    "TourModal",
    "advance_index",
    "default_tour_deck",
]
