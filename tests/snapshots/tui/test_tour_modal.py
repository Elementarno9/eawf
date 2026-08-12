"""Golden snapshot + carousel behaviour for the first-run tour.

The first-run
:class:`~eawf.surfaces.tui.modals.tour.TourModal` walks the operator through
a short ordered deck of
:class:`~eawf.surfaces.tui.modals.tour_cards.TourCard` cards, one card at a
time. The advance key steps forward through the deck; the dismiss key closes
the tour and persists the ``ui.tour_completed`` config leaf.

This module pins the wave's CR-01 gate:

* the carousel **advances across its cards** -- pressing the advance key
  steps the displayed card forward (the progress line + card body follow);
* the dismiss key **closes the tour and sets ui.tour_completed** -- the
  dismiss path routes the completed flag through the layered writer (the same
  mutator the config modal uses) and pops the modal.

The pure deck + index helpers are unit-tested without a Textual mount; the
carousel behaviour is driven through a real Pilot keypress.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_tour_modal.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modals.tour import (
    TOUR_COMPLETED_KEY,
    TourModal,
    advance_index,
    default_tour_deck,
)
from eawf.surfaces.tui.modals.tour_cards import TourCard
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


def _deck() -> tuple[TourCard, ...]:
    """A two-card deck so the advance + clamp steps are observable."""
    return (
        TourCard(title="First card", body="The opening card body."),
        TourCard(title="Second card", body="The closing card body."),
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_default_tour_deck_is_non_empty() -> None:
    """The shipped first-run deck has at least one card to walk."""
    assert len(default_tour_deck()) >= 1


def test_advance_index_steps_forward() -> None:
    """Advance steps the index toward the last card."""
    assert advance_index(0, 3) == 1
    assert advance_index(1, 3) == 2


def test_advance_index_clamps_at_last_card() -> None:
    """boundary: advancing from the last card stays put (forward-only walk)."""
    assert advance_index(2, 3) == 2


def test_advance_index_single_card_stays() -> None:
    """boundary: a one-card deck has nowhere to advance to."""
    assert advance_index(0, 1) == 0


def test_advance_index_rejects_empty_deck() -> None:
    """error-path: an empty deck has no card to advance to."""
    with pytest.raises(ValueError, match="deck_size must be positive"):
        advance_index(0, 0)


def test_tour_modal_rejects_empty_deck() -> None:
    """error-path: a tour over an empty deck is rejected at construction."""
    with pytest.raises(ValueError, match="tour deck must be non-empty"):
        TourModal(deck=())


# --------------------------------------------------------------------------
# CR-01: the carousel advances + dismiss closes and sets ui.tour_completed
# --------------------------------------------------------------------------


def test_tour_modal_snapshot_first_card() -> None:
    """The tour renders the first card + progress on mount."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(TourModal(deck=_deck()))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "First card" in frame
            assert "card 1 of 2" in frame
            assert_screen_snapshot(app, _GOLDEN / "tour_modal.txt")

    asyncio.run(body())


def test_tour_modal_advances_across_cards() -> None:
    """Pressing the advance key steps the carousel forward through the deck."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(TourModal(deck=_deck()))
            await settle_screen(pilot)
            first = normalize_snapshot(capture_screen_text(app))
            assert "First card" in first
            assert "card 1 of 2" in first
            await pilot.press("right")  # advance to the second card
            await settle_screen(pilot)
            second = normalize_snapshot(capture_screen_text(app))
            assert "Second card" in second
            assert "card 2 of 2" in second

    asyncio.run(body())


def test_tour_modal_advance_clamps_at_last_card() -> None:
    """Advancing past the last card stays on it (forward-only carousel)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(TourModal(deck=_deck()))
            await settle_screen(pilot)
            await pilot.press("right")
            await pilot.press("right")  # already last -- clamps
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "card 2 of 2" in frame

    asyncio.run(body())


def test_tour_modal_dismiss_closes_and_sets_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dismiss key closes the tour and persists ui.tour_completed=True."""
    saved: list[tuple[str, object]] = []

    def _capture(
        *, target_path: Path, key: str, value: object, repo_root: Path | None = None
    ) -> None:
        saved.append((key, value))

    monkeypatch.setattr("eawf.surfaces.cli.commands.config._save_value_to_layer", _capture)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(TourModal(deck=_deck()))
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press("escape")  # dismiss
            await settle_screen(pilot)
            assert app.modal_depth() == depth_before - 1

    asyncio.run(body())
    assert (TOUR_COMPLETED_KEY, True) in saved


def test_tour_modal_dismiss_via_q_sets_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``q`` dismiss alias also persists ui.tour_completed=True."""
    saved: list[tuple[str, object]] = []

    def _capture(
        *, target_path: Path, key: str, value: object, repo_root: Path | None = None
    ) -> None:
        saved.append((key, value))

    monkeypatch.setattr("eawf.surfaces.cli.commands.config._save_value_to_layer", _capture)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(TourModal(deck=_deck()))
            await settle_screen(pilot)
            await pilot.press("q")  # dismiss alias
            await settle_screen(pilot)

    asyncio.run(body())
    assert (TOUR_COMPLETED_KEY, True) in saved
