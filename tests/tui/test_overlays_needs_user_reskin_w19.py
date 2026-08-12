"""Pilot tests for the needs_user inbox + modal cosmic-terminal reskin.

The reskin lands the shared ``attention`` chrome sigil on the inbox cards
and the single-pause prompt modal, the green-accent option chips, and the
literal calm empty-inbox copy. These tests mount each overlay IN
ISOLATION through a Pilot harness and assert:

- two seeded open pauses each render a card carrying the attention sigil
  plus the wave / scope name (urgency tint preserved);
- the empty inbox collapses to the literal :data:`EMPTY_INBOX_TEXT` calm
  note and issues NO fabricated pause row;
- ``affordance_parity`` -- the open (``Enter``), dismiss (``d``), and
  close (``Esc``) keys resolve to live ``Binding`` actions that fire;
- the single-pause modal header wears the attention sigil and the options
  render as green-accent chips, with ``Enter`` / ``Esc`` still wired.

Pauses are seeded via the pre-ranked tuple the host resolves (the same
contract :func:`open_needs_user_inbox` consumes) so the overlay never
reaches into the store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.binding import Binding
from textual.widgets import Static

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.needs_user import NeedsUserModal, open_needs_user
from eawf.surfaces.tui.screens.overlays.needs_user_inbox import (
    EMPTY_INBOX_TEXT,
    NeedsUserInbox,
    open_needs_user_inbox,
    rank_pauses_by_urgency,
)
from eawf.surfaces.tui.widgets.sigils import chrome
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import OpenPause

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_SCOPE = "urn:eawf:v1:state:QR"  # matches the fixture's State.urn
_SESSION_A = "urn:eawf:v1:session:cli/SES-wave-a"
_SESSION_B = "urn:eawf:v1:session:cli/SES-wave-b"

#: Fixed reference instant so a pause row's relative ``time-ago`` is stable.
_FIXED_NOW = datetime(2099, 1, 1, tzinfo=UTC)

#: The unicode attention sigil (the default render mode) the reskin marks
#: every card and the modal header with -- the single-cell triangle.
_ATTENTION_SIGIL = chrome("attention", mode="unicode")


def _question(text: str) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[
            UserQuestionOption(label="apply", description="apply as-is"),
            UserQuestionOption(label="cancel"),
        ],
    )


def _pause(*, urgency: Urgency, question: str, pause_urn: str, session: str) -> OpenPause:
    return OpenPause(
        pause_urn=pause_urn,
        scope_id=_SCOPE,
        session=session,
        question=_question(question),
        urgency=urgency,
    )


def _two_pauses() -> tuple[OpenPause, ...]:
    return rank_pauses_by_urgency(
        (
            _pause(
                urgency=Urgency.URGENT,
                question="urgent question",
                pause_urn="urn:eawf:v1:event:QR/p-a",
                session=_SESSION_A,
            ),
            _pause(
                urgency=Urgency.LOW,
                question="calm question",
                pause_urn="urn:eawf:v1:event:QR/p-b",
                session=_SESSION_B,
            ),
        )
    )


def _binding_for(screen: object, key: str) -> Binding:
    """Return the live ``Binding`` a *key* resolves to on *screen*.

    Args:
        screen: The mounted overlay whose ``BINDINGS`` to scan.
        key: The bound key string (e.g. ``"enter"``).

    Returns:
        The first ``Binding`` declared for *key*.

    Raises:
        AssertionError: When *key* has no live binding on *screen*.
    """
    for binding in screen.BINDINGS:  # type: ignore[attr-defined]
        if isinstance(binding, Binding) and binding.key == key:
            return binding
    raise AssertionError(f"no live binding for key {key!r}")


# --------------------------------------------------------------------------
# Inbox -- a card per pause carries the attention sigil + the wave name
# --------------------------------------------------------------------------


def test_inbox_cards_carry_attention_sigil_and_wave_name() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, _two_pauses(), now=_FIXED_NOW)
            await pilot.pause()
            assert isinstance(app.screen, NeedsUserInbox)
            rows = app.screen.query(".inbox-row")
            # One card per open pause -- no more, no fewer.
            assert len(rows) == 2
            rendered = [str(row.render()) for row in rows]
            for card in rendered:
                # Each card leads with the shared attention sigil.
                assert _ATTENTION_SIGIL in card
            # The wave / session names ride their respective cards (urgent
            # ranks first, so it is card 0).
            assert "SES-wave-a" in rendered[0]
            assert "urgent question" in rendered[0]
            assert "SES-wave-b" in rendered[1]
            assert "calm question" in rendered[1]

    asyncio.run(body())


def test_inbox_empty_renders_literal_calm_copy_with_no_fabricated_row() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, (), now=_FIXED_NOW)
            await pilot.pause()
            inbox = app.screen
            assert isinstance(inbox, NeedsUserInbox)
            # The empty inbox issues NO fabricated pause row...
            assert len(inbox.query(".inbox-row")) == 0
            # ...and collapses to the literal calm copy verbatim.
            empties = inbox.query(".inbox-empty")
            assert empties
            assert str(empties.first(Static).render()) == EMPTY_INBOX_TEXT

    asyncio.run(body())


def test_inbox_affordance_parity_open_dismiss_close_keys_fire() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            open_needs_user_inbox(app, _two_pauses(), now=_FIXED_NOW)
            await pilot.pause()
            inbox = app.screen
            assert isinstance(inbox, NeedsUserInbox)
            # affordance_parity: open / dismiss / close keys resolve to live
            # Bindings naming a callable action_* handler on the screen.
            assert _binding_for(inbox, "enter").action == "open_pause"
            assert _binding_for(inbox, "d").action == "dismiss_row"
            assert _binding_for(inbox, "escape").action == "close"
            for action in ("action_open_pause", "action_dismiss_row", "action_close"):
                assert callable(getattr(inbox, action))

            # Fire the dismiss key: the highlighted card is removed live.
            assert len(inbox.query(".inbox-row")) == 2
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(inbox.query(".inbox-row")) == 1

            # Fire the open key on the survivor: the inbox closes and the
            # pause's NeedsUserModal lands on the stack.
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, NeedsUserModal)

            # Fire the close key: the modal dismisses.
            depth_before = app.modal_depth()
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() < depth_before

    asyncio.run(body())


# --------------------------------------------------------------------------
# Single-pause modal -- attention-sigil header + green-accent option chips
# --------------------------------------------------------------------------


def test_modal_header_carries_attention_sigil() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            open_needs_user(app, _question("Apply the proposed roadmap?"))
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, NeedsUserModal)
            header = modal.query_one(".needs-user-question", Static)
            rendered = str(header.render())
            assert _ATTENTION_SIGIL in rendered
            assert "Apply the proposed roadmap?" in rendered

    asyncio.run(body())


def test_modal_options_render_as_chips_and_keys_fire() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = NeedsUserModal(_question("pick one"))
            app.push_screen(modal, callback=sink.append)
            await pilot.pause()
            # The option chips carry the leading dot marker (the green-accent
            # chip is the .needs-user-option class, styled $accent).
            chips = [str(c.render()) for c in modal.query(".needs-user-option")]
            assert chips
            assert all(chip.startswith("- ") for chip in chips)
            assert any("apply" in chip for chip in chips)

            # The action keys are unchanged: Enter confirms the raw label,
            # Esc defers to None.
            assert _binding_for(modal, "enter").action == "confirm"
            assert _binding_for(modal, "escape").action == "defer"
            await pilot.press("enter")
            await pilot.pause()
        # The dismiss value is the raw label (no chip marker leaked in).
        assert sink == ["apply"]

    asyncio.run(body())


def test_modal_esc_still_defers_to_none() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[str | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = NeedsUserModal(_question("pick one"))
            app.push_screen(modal, callback=sink.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == [None]

    asyncio.run(body())
