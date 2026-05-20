"""Tests for the C06 ``HelpScreen`` overlay (P26-W19).

Pure keymap-row helpers (global / pane-nav / scope) plus Pilot-driven
behaviour: ``?`` opens the overlay, the D31 single-instance guard makes a
second ``?`` a no-op, the rendered overlay shows full key names + palette
verbs, and ``Esc`` closes it (clearing the guard).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.help import (
    HelpScreen,
    global_key_rows,
    pane_nav_rows,
    scope_key_rows,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


# --------------------------------------------------------------------------
# Pure keymap rows
# --------------------------------------------------------------------------


def test_global_key_rows_include_palette_and_help_keys() -> None:
    keys = {key for key, _ in global_key_rows()}
    assert "/" in keys
    assert "?" in keys
    assert "q" in keys


def test_pane_nav_rows_use_full_key_names() -> None:
    keys = {key for key, _, _ in pane_nav_rows()}
    # Full names per D11 — never PgUp / PgDn.
    assert "PageUp" in keys
    assert "PageDown" in keys
    assert "PgUp" not in keys
    assert "PgDn" not in keys


def test_pane_nav_rows_carry_vim_aliases() -> None:
    aliases = {alias for _, _, alias in pane_nav_rows()}
    assert {"h", "j", "k", "l"} <= aliases


def test_scope_key_rows_repo_has_wave_board() -> None:
    rows = scope_key_rows("repo")
    keys = {key for key, _ in rows}
    assert "w" in keys


def test_scope_key_rows_user_is_empty() -> None:
    assert scope_key_rows("user") == ()


# --------------------------------------------------------------------------
# Pilot behaviour
# --------------------------------------------------------------------------


def test_help_opens_on_question_mark() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            assert app._help_open is True

    asyncio.run(body())


def test_help_second_question_mark_is_noop() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            # D31: the guard suppresses the duplicate push.
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_help_renders_keymap_and_verbs() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "PageUp" in rendered
            assert "/find" in rendered

    asyncio.run(body())


def test_help_esc_closes_and_clears_guard() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0
            assert app._help_open is False

    asyncio.run(body())


def test_help_reopens_after_close() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            # Guard cleared on close, so ? opens it again.
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

    asyncio.run(body())
