"""Pilot tests for the C06 ``ConfirmModal`` overlay (P26-W19).

Covers the arrow-toggle yes/no contract: the safe ``No`` default, ``←`` /
``→`` selection movement, ``Enter`` confirming the highlighted choice
(returned via the modal dismiss value), and ``Esc`` cancelling to
``False``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _push_confirm(app: EaApp, prompt: str, sink: list[bool | None]) -> ConfirmModal:
    modal = ConfirmModal(prompt)
    app.push_screen(modal, callback=lambda result: sink.append(result))
    return modal


def test_confirm_defaults_to_no() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_confirm(app, "Drop wave?", [])
            await pilot.pause()
            assert modal.selected == 0  # index 0 == "No"

    asyncio.run(body())


def test_confirm_right_then_enter_returns_true() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[bool | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_confirm(app, "Drop wave?", sink)
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert modal.selected == 1  # "Yes"
            await pilot.press("enter")
            await pilot.pause()
        assert sink == [True]

    asyncio.run(body())


def test_confirm_enter_on_default_returns_false() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[bool | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_confirm(app, "Drop wave?", sink)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert sink == [False]

    asyncio.run(body())


def test_confirm_esc_returns_false() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[bool | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_confirm(app, "Drop wave?", sink)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == [False]

    asyncio.run(body())


def test_confirm_left_after_right_returns_to_no() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_confirm(app, "Drop wave?", [])
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("left")
            await pilot.pause()
            assert modal.selected == 0

    asyncio.run(body())


def test_confirm_vim_alias_l_selects_yes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_confirm(app, "Drop wave?", [])
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert modal.selected == 1

    asyncio.run(body())


def test_confirm_renders_prompt() -> None:
    async def body() -> None:
        from textual.widgets import Static

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_confirm(app, "Cherry-pick now?", [])
            await pilot.pause()
            prompt = modal.query_one(".confirm-prompt", Static)
            assert "Cherry-pick now?" in str(prompt.render())

    asyncio.run(body())
