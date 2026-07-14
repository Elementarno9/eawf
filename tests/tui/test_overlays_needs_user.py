"""Pilot tests for the C06 ``NeedsUserModal`` overlay (P26-W20).

Covers the needs_user AskUserQuestion contract: the first-option default,
``↑`` / ``↓`` selection movement (with wrap), ``Enter`` returning the
highlighted option's label via the dismiss value, ``Esc`` deferring to
``None``, and the cap-checked ``open_needs_user`` helper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.needs_user import NeedsUserModal, open_needs_user
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_QUESTION = UserQuestion(
    question="Apply the proposed roadmap?",
    options=[
        UserQuestionOption(label="apply", description="apply as-is"),
        UserQuestionOption(label="revise"),
        UserQuestionOption(label="cancel"),
    ],
)


def _push_needs_user(app: EaApp, sink: list[str | None]) -> NeedsUserModal:
    modal = NeedsUserModal(_QUESTION)
    app.push_screen(modal, callback=lambda result: sink.append(result))
    return modal


def test_needs_user_defaults_to_first_option() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            modal = _push_needs_user(app, [])
            await pilot.pause()
            assert modal.selected == 0

    asyncio.run(body())


def test_needs_user_action_move_empty_labels_is_noop() -> None:
    """A degenerate empty-options modal does not divide by zero on move."""
    modal = NeedsUserModal(_QUESTION)
    modal._labels = ()  # degenerate: no options to move between
    # The modulo guard makes the move a no-op rather than a ZeroDivisionError.
    modal.action_move(1)
    modal.action_move(-1)
    assert modal.selected == 0


def test_needs_user_down_then_enter_returns_second_label() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            _push_needs_user(app, sink)
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
        assert sink == ["revise"]

    asyncio.run(body())


def test_needs_user_enter_on_default_returns_first_label() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            _push_needs_user(app, sink)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert sink == ["apply"]

    asyncio.run(body())


def test_needs_user_esc_defers_to_none() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            _push_needs_user(app, sink)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == [None]

    asyncio.run(body())


def test_needs_user_up_wraps_to_last_option() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            modal = _push_needs_user(app, [])
            await pilot.pause()
            await pilot.press("up")  # wraps 0 -> last (index 2)
            await pilot.pause()
            assert modal.selected == 2

    asyncio.run(body())


def test_needs_user_renders_question() -> None:
    async def body() -> None:
        from textual.widgets import Static

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            modal = _push_needs_user(app, [])
            await pilot.pause()
            question = modal.query_one(".needs-user-question", Static)
            assert "Apply the proposed roadmap?" in str(question.render())

    asyncio.run(body())


def test_open_needs_user_respects_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(NeedsUserModal(_QUESTION))
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            open_needs_user(app, _QUESTION)
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())
