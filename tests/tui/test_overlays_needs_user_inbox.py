"""Tests for the global needs_user inbox overlay (P29-I02-W18).

Two layers: the pure urgency-ranking helper
(:func:`rank_pauses_by_urgency`) without Textual, and Pilot-driven
mounting + navigation of the :class:`NeedsUserInbox` overlay through the
modal-stack cap — ranked order, honest-empty, and selecting a row
opening the right pause's :class:`NeedsUserModal`.

Pauses are seeded into the event store before the app launches (mirroring
``test_overlays_needs_user_autoopen``); the inbox reads them across every
scope via ``list_open_pauses(scope_id=None)``.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from textual.widgets import Static

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.needs_user import NeedsUserModal
from eawf.surfaces.tui.screens.overlays.needs_user_inbox import (
    EMPTY_INBOX_TEXT,
    NeedsUserInbox,
    open_needs_user_inbox,
    rank_pauses_by_urgency,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import OpenPause, list_open_pauses, record_pause

#: Fixed reference instant so a pause row's relative ``time-ago`` is stable.
_FIXED_NOW = datetime(2099, 1, 1, tzinfo=UTC)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SCOPE = "urn:eawf:v1:state:QR"  # matches the fixture's State.urn
_OTHER_SCOPE = "urn:eawf:v1:state:ZZ"
_SESSION = "urn:eawf:v1:session:cli/SES-tui"


def _question(text: str) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[
            UserQuestionOption(label="apply"),
            UserQuestionOption(label="cancel"),
        ],
    )


def _pause(*, urgency: Urgency, question: str, pause_urn: str, scope_id: str = _SCOPE) -> OpenPause:
    return OpenPause(
        pause_urn=pause_urn,
        scope_id=scope_id,
        session=_SESSION,
        question=_question(question),
        urgency=urgency,
    )


def _temp_state(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    shutil.copyfile(_PHASE_ITER_WAVE, path)
    return path


# --------------------------------------------------------------------------
# rank_pauses_by_urgency — pure ordering
# --------------------------------------------------------------------------


def test_rank_pauses_by_urgency_orders_most_immediate_first() -> None:
    pauses = (
        _pause(urgency=Urgency.LOW, question="low", pause_urn="urn:eawf:v1:event:QR/p-low"),
        _pause(urgency=Urgency.URGENT, question="urgent", pause_urn="urn:eawf:v1:event:QR/p-urg"),
        _pause(urgency=Urgency.NORMAL, question="normal", pause_urn="urn:eawf:v1:event:QR/p-nrm"),
        _pause(urgency=Urgency.HIGH, question="high", pause_urn="urn:eawf:v1:event:QR/p-hi"),
    )
    ranked = rank_pauses_by_urgency(pauses)
    assert [p.urgency for p in ranked] == [
        Urgency.URGENT,
        Urgency.HIGH,
        Urgency.NORMAL,
        Urgency.LOW,
    ]


def test_rank_pauses_by_urgency_is_stable_within_a_tier() -> None:
    # Same tier keeps input (append) order so the oldest pause stays on top.
    first = _pause(urgency=Urgency.HIGH, question="first", pause_urn="urn:eawf:v1:event:QR/p-1")
    second = _pause(urgency=Urgency.HIGH, question="second", pause_urn="urn:eawf:v1:event:QR/p-2")
    ranked = rank_pauses_by_urgency((first, second))
    assert [p.pause_urn for p in ranked] == [first.pause_urn, second.pause_urn]


def test_rank_pauses_by_urgency_empty_is_empty() -> None:
    assert rank_pauses_by_urgency(()) == ()


# --------------------------------------------------------------------------
# NeedsUserInbox — mounting, ranked list, empty note (Pilot)
# --------------------------------------------------------------------------


def test_inbox_modal_lists_pauses_ranked_by_urgency() -> None:
    async def body() -> None:
        ranked = rank_pauses_by_urgency(
            (
                _pause(urgency=Urgency.LOW, question="low q", pause_urn="urn:eawf:v1:event:QR/p-l"),
                _pause(
                    urgency=Urgency.URGENT,
                    question="urgent q",
                    pause_urn="urn:eawf:v1:event:QR/p-u",
                ),
                _pause(
                    urgency=Urgency.NORMAL,
                    question="normal q",
                    pause_urn="urn:eawf:v1:event:QR/p-n",
                ),
            )
        )
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, ranked)
            await pilot.pause()
            assert isinstance(app.screen, NeedsUserInbox)
            rows = app.screen.query(".inbox-row")
            rendered = [str(row.render()) for row in rows]
            # Most-immediate first: urgent, then normal, then low.
            assert "urgent q" in rendered[0]
            assert "normal q" in rendered[1]
            assert "low q" in rendered[2]

    asyncio.run(body())


def test_inbox_modal_empty_shows_honest_empty_note() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, ())
            await pilot.pause()
            empties = app.screen.query(".inbox-empty")
            assert empties
            assert EMPTY_INBOX_TEXT in str(empties.first(Static).render())

    asyncio.run(body())


def test_inbox_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, ())
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_inbox_enter_opens_the_highlighted_pause_modal(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        # Seed two pauses with distinct urgency; the urgent one ranks first
        # so the initial highlight (index 0) is the urgent pause.
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("the low one"),
            urgency=Urgency.LOW,
        )
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("the urgent one"),
            urgency=Urgency.URGENT,
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            # An active-scope pause auto-opens a NeedsUserModal; clear it so
            # the test drives the inbox path explicitly.
            while any(isinstance(s, NeedsUserModal) for s in app.screen_stack):
                await pilot.press("escape")
                await pilot.pause()
            app.action_open_inbox()
            await pilot.pause()
            assert isinstance(app.screen, NeedsUserInbox)
            await pilot.press("enter")
            await pilot.pause()
            # The inbox dismissed and the highlighted (urgent) pause's modal
            # is now on the stack rendering its question.
            assert isinstance(app.screen, NeedsUserModal)
            question_cell = app.screen.query_one(".needs-user-question", Static)
            assert "the urgent one" in str(question_cell.render())

    asyncio.run(body())


def test_inbox_open_routes_through_modal_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            pushed = open_needs_user_inbox(app, ())
            await pilot.pause()
            assert pushed is True
            assert app.modal_depth() == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# Inbox spans every scope (badge + inbox agree on the cross-scope set)
# --------------------------------------------------------------------------


def test_inbox_action_lists_pauses_across_scopes(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("this scope"),
            urgency=Urgency.NORMAL,
        )
        record_pause(
            state_path,
            scope_id=_OTHER_SCOPE,
            session=_SESSION,
            question=_question("other scope"),
            urgency=Urgency.URGENT,
        )
        # Both pauses are open across scopes.
        assert len(list_open_pauses(state_path, scope_id=None)) == 2
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            while any(isinstance(s, NeedsUserInbox) for s in app.screen_stack) is False and any(
                isinstance(s, NeedsUserModal) for s in app.screen_stack
            ):
                await pilot.press("escape")
                await pilot.pause()
            app.action_open_inbox()
            await pilot.pause()
            assert isinstance(app.screen, NeedsUserInbox)
            # The inbox lists BOTH scopes' pauses (cross-scope scan).
            assert len(app.screen.query(".inbox-row")) == 2

    asyncio.run(body())


# --------------------------------------------------------------------------
# D3 -- each inbox row renders its relative time-ago (deterministic now)
# --------------------------------------------------------------------------


def test_inbox_row_renders_time_ago() -> None:
    async def body() -> None:
        pause = _pause(urgency=Urgency.URGENT, question="q", pause_urn="urn:eawf:v1:event:QR/p")
        pause.occurred_at = datetime(2098, 6, 1, tzinfo=UTC)  # before _FIXED_NOW
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, (pause,), now=_FIXED_NOW)
            await pilot.pause()
            rows = app.screen.query(".inbox-row")
            assert rows
            assert "ago" in str(rows.first(Static).render())

    asyncio.run(body())


# --------------------------------------------------------------------------
# D4 -- dismiss hides the selected pause + records it on the app set
# --------------------------------------------------------------------------


def test_inbox_dismiss_hides_selected_pause(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("the urgent one"),
            urgency=Urgency.URGENT,
        )
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("the low one"),
            urgency=Urgency.LOW,
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            while any(isinstance(s, NeedsUserModal) for s in app.screen_stack):
                await pilot.press("escape")
                await pilot.pause()
            app.action_open_inbox()
            await pilot.pause()
            inbox = app.screen
            assert isinstance(inbox, NeedsUserInbox)
            assert len(inbox.query(".inbox-row")) == 2
            # Dismiss the highlighted (urgent, index 0) pause.
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # The dismissed row is gone from the inbox; the survivor remains.
            assert len(inbox.query(".inbox-row")) == 1
            assert len(app.attention_dismissed()) == 1
            assert "the low one" in str(inbox.query(".inbox-row").first(Static).render())

    asyncio.run(body())


def test_inbox_excludes_already_dismissed_pause(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("dismiss me"),
            urgency=Urgency.URGENT,
        )
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("keep me"),
            urgency=Urgency.LOW,
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            while any(isinstance(s, NeedsUserModal) for s in app.screen_stack):
                await pilot.press("escape")
                await pilot.pause()
            # Pre-dismiss the urgent pause via the app set, then open the inbox.
            urgent = next(p for p in app._all_open_pauses() if "dismiss me" in p.question.question)
            from eawf.surfaces.tui.screens.overlays.needs_user_inbox import _pause_dismiss_key

            app.dismiss_attention(_pause_dismiss_key(urgent))
            await pilot.pause()
            app.action_open_inbox()
            await pilot.pause()
            inbox = app.screen
            assert isinstance(inbox, NeedsUserInbox)
            # Only the non-dismissed pause is listed.
            rows = inbox.query(".inbox-row")
            assert len(rows) == 1
            assert "keep me" in str(rows.first(Static).render())

    asyncio.run(body())
