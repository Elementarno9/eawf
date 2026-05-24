"""Pilot tests for the needs_user auto-open + pick path (P26-I02-W07).

Covers the TUI side of wiring ``NeedsUserModal`` to the pause store:

- A pending pause for the active scope auto-opens the modal off the
  :class:`~eawf.surfaces.tui.state_binding.StateBinding` refresh.
- A pick routes through the shared resume library function (the pause is
  resolved with the chosen label).
- A resume failure surfaces an ``error``-severity toast.
- A pause for a different scope does not auto-open.

The pause is seeded into the event store before the app launches so the
initial state load drives the auto-open check.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.needs_user import NeedsUserModal
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import list_open_pauses, record_pause

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SCOPE = "urn:eawf:v1:state:QR"  # matches the fixture's State.urn
_OTHER_SCOPE = "urn:eawf:v1:state:ZZ"
_SESSION = "urn:eawf:v1:session:cli/SES-tui"
_QUESTION = UserQuestion(
    question="Apply the proposed roadmap?",
    options=[
        UserQuestionOption(label="apply", description="apply as-is"),
        UserQuestionOption(label="revise"),
        UserQuestionOption(label="cancel"),
    ],
)


def _temp_state(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    shutil.copyfile(_PHASE_ITER_WAVE, path)
    return path


def test_pending_pause_auto_opens_modal(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert any(isinstance(screen, NeedsUserModal) for screen in app.screen_stack), (
                "a pending pause for the active scope must auto-open NeedsUserModal"
            )

    asyncio.run(body())


def test_pick_resolves_pause_with_chosen_label(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down")  # highlight "revise"
            await pilot.press("enter")
            await pilot.pause()
        # The pick resumed the pause with the chosen label.
        assert list_open_pauses(state_path, scope_id=_SCOPE) == []

    asyncio.run(body())


def test_defer_keeps_pause_open(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("escape")  # defer
            await pilot.pause()
        # Esc defers — the pause is untouched, still open.
        assert len(list_open_pauses(state_path, scope_id=_SCOPE)) == 1

    asyncio.run(body())


def test_resume_failure_shows_error_toast(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        pause_urn = record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            # Resolve the pause out-of-band so the in-app pick fails.
            from eawf.workflow.skills.needs_user import resolve_pause

            resolve_pause(state_path, pause_urn=pause_urn, choice="apply")
            await pilot.press("enter")
            await pilot.pause()
        assert notices, "a resume failure must toast"
        message, severity = notices[-1]
        assert severity == "error"
        assert "resume failed" in message

    asyncio.run(body())


def test_other_scope_pause_does_not_auto_open(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(state_path, scope_id=_OTHER_SCOPE, session=_SESSION, question=_QUESTION)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not any(isinstance(screen, NeedsUserModal) for screen in app.screen_stack), (
                "a pause for a different scope must not auto-open"
            )

    asyncio.run(body())
