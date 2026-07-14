"""Tests for the footer needs_user attention badge (P29-I02-W18).

Three layers: the pure badge formatter
(:func:`format_needs_user_badge`) without Textual, a Pilot-driven paint
of the Footer badge cell (quiet at 0, attention-coloured + counted at N),
and an App-level wiring check that a seeded pause drives the badge count
off the same pause source the auto-open path reads.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from textual.app import ComposeResult
from textual.widgets import Static

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.widgets.footer import (
    NEEDS_USER_BADGE_LABEL,
    Footer,
    format_needs_user_badge,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import record_pause

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-tui"
_QUESTION = UserQuestion(
    question="Apply the proposed roadmap?",
    options=[UserQuestionOption(label="apply"), UserQuestionOption(label="cancel")],
)


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Footer(id="ftr")


# --------------------------------------------------------------------------
# format_needs_user_badge — quiet at 0, counted at N, clamped negative
# --------------------------------------------------------------------------


def test_format_needs_user_badge_zero_is_quiet() -> None:
    # Idle: the empty string (truly quiet, no footer space stolen).
    assert format_needs_user_badge(0) == ""


def test_format_needs_user_badge_positive_shows_count() -> None:
    # Trailing space separates the badge from the heartbeat on the row.
    assert format_needs_user_badge(3) == f"{NEEDS_USER_BADGE_LABEL} 3 "


def test_format_needs_user_badge_single() -> None:
    assert format_needs_user_badge(1) == f"{NEEDS_USER_BADGE_LABEL} 1 "


def test_format_needs_user_badge_negative_clamps_to_quiet() -> None:
    # A stray decrement never renders a nonsensical figure.
    assert format_needs_user_badge(-2) == ""


# --------------------------------------------------------------------------
# Footer badge cell — quiet (0) vs attention (N) paint
# --------------------------------------------------------------------------


def _badge_text(footer: Footer) -> str:
    return str(footer.query_one(".footer-needs-user", Static).render())


def test_footer_badge_quiet_when_zero() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            footer = app.query_one("#ftr", Footer)
            # Default count is 0 -> quiet (empty text, no attention class).
            cell = footer.query_one(".footer-needs-user", Static)
            assert _badge_text(footer) == ""
            assert not cell.has_class("-attention")

    asyncio.run(body())


def test_footer_badge_attention_when_positive() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            footer = app.query_one("#ftr", Footer)
            footer.pending_pauses = 2
            await pilot.pause()
            cell = footer.query_one(".footer-needs-user", Static)
            assert f"{NEEDS_USER_BADGE_LABEL} 2" in _badge_text(footer)
            # The attention class flips on so the cell draws the eye.
            assert cell.has_class("-attention")

    asyncio.run(body())


def test_footer_badge_repaints_back_to_quiet_on_zero() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            footer = app.query_one("#ftr", Footer)
            footer.pending_pauses = 4
            await pilot.pause()
            assert footer.query_one(".footer-needs-user", Static).has_class("-attention")
            # Resolving every pause flips the badge back to quiet in place.
            footer.pending_pauses = 0
            await pilot.pause()
            cell = footer.query_one(".footer-needs-user", Static)
            assert _badge_text(footer) == ""
            assert not cell.has_class("-attention")

    asyncio.run(body())


# --------------------------------------------------------------------------
# App wiring — a seeded pause drives the footer badge count
# --------------------------------------------------------------------------


def _temp_state(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    shutil.copyfile(_PHASE_ITER_WAVE, path)
    return path


def _scope_footer(app: EaApp) -> Footer:
    """Return the Footer on the scope screen (which may sit beneath a modal)."""
    for screen in app.screen_stack:
        found = screen.query(Footer)
        if found:
            return found.first()
    raise AssertionError("no Footer mounted on any screen in the stack")


def test_footer_badge_reflects_seeded_pause_count(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_QUESTION,
            urgency=Urgency.HIGH,
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # The state refresh recomputes the cross-scope pause count.
            assert app.pending_pauses == 1
            footer = _scope_footer(app)
            assert footer.pending_pauses == 1
            assert footer.query_one(".footer-needs-user", Static).has_class("-attention")

    asyncio.run(body())


def test_footer_badge_quiet_with_no_pauses() -> None:
    async def body() -> None:
        # The fixture has no seeded pauses; the badge stays quiet.
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.pending_pauses == 0
            footer = _scope_footer(app)
            assert not footer.query_one(".footer-needs-user", Static).has_class("-attention")

    asyncio.run(body())
