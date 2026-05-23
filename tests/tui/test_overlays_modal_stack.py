"""Tests for the C06 modal-stack depth cap (P26-W19).

The App enforces a single modal-stack cap (``MAX_MODAL_DEPTH == 3`` per
C06 §5.7 / failure mode F6): every overlay-opening path routes through
:meth:`EaApp.push_modal`, which rejects the fourth push and toasts rather
than mutating the stack. These tests drive the cap directly and through
the palette + detail-drill paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.tui.app import EaApp
from eawf.tui.screens.overlays.confirm import ConfirmModal
from eawf.tui.screens.overlays.detail import DetailCard, DetailModal

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_CARD = DetailCard(title="t", rows=(("a", "b"),))


def test_max_modal_depth_is_three() -> None:
    assert EaApp.MAX_MODAL_DEPTH == 3


def test_push_three_modals_succeeds() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            results = []
            for _ in range(3):
                results.append(app.push_modal(DetailModal(_CARD)))
                await pilot.pause()
            assert results == [True, True, True]
            assert app.modal_depth() == 3

    asyncio.run(body())


def test_fourth_modal_rejected() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(3):
                app.push_modal(DetailModal(_CARD))
                await pilot.pause()
            accepted = app.push_modal(DetailModal(_CARD))
            await pilot.pause()
            assert accepted is False
            assert app.modal_depth() == 3

    asyncio.run(body())


def test_cap_frees_after_pop() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(3):
                app.push_modal(DetailModal(_CARD))
                await pilot.pause()
            assert app.push_modal(DetailModal(_CARD)) is False
            await pilot.pause()
            # Pop one (Esc on the top DetailModal), then a push fits again.
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 2
            assert app.push_modal(ConfirmModal("ok?")) is True
            await pilot.pause()
            assert app.modal_depth() == 3

    asyncio.run(body())


def test_modal_depth_zero_on_scope_screen() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # The base scope screen is a plain Screen, not a ModalScreen.
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_palette_then_two_more_then_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Open the palette via the keypress (one modal), then stack
            # two more programmatically and confirm the 4th is rejected.
            await pilot.press("slash")
            await pilot.pause()
            assert app.modal_depth() == 1
            assert app.push_modal(DetailModal(_CARD)) is True
            await pilot.pause()
            assert app.push_modal(ConfirmModal("q?")) is True
            await pilot.pause()
            assert app.modal_depth() == 3
            assert app.push_modal(DetailModal(_CARD)) is False
            await pilot.pause()
            assert app.modal_depth() == 3

    asyncio.run(body())
