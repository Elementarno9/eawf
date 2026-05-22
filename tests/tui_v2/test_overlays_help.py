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
    config_overlay_rows,
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


def test_global_key_rows_have_raw_scope_switch() -> None:
    # The W32 keybinding fix: raw w/r/u switch scope (no ctrl). The
    # dead wave-board ``w`` repo-scope binding was removed.
    rows = dict(global_key_rows())
    assert "switch to workspace scope" in rows["w"]
    assert "switch to repo scope" in rows["r"]
    assert "switch to user scope" in rows["u"]


def test_global_key_rows_document_moved_refresh() -> None:
    # Refresh moved off raw ``r`` (now repo scope-switch) onto F5; the
    # affordance must stay documented (daemon-push / heartbeat-ack ref).
    rows = dict(global_key_rows())
    assert "F5" in rows
    assert "refresh" in rows["F5"]


def test_global_key_rows_document_config_any_scope() -> None:
    # W14: ``c`` opens config from every scope, so the help lists it as a
    # global key (not a repo-only per-screen extra) and frames it as
    # scope-agnostic.
    rows = dict(global_key_rows())
    assert "c" in rows
    assert "config" in rows["c"]
    assert "any scope" in rows["c"]


def test_scope_key_rows_repo_is_empty() -> None:
    # W14: config moved to the global table; the repo screen no longer
    # carries a repo-only ``c`` extra (it is not repo-specific anymore).
    rows = scope_key_rows("repo")
    keys = {key for key, _ in rows}
    assert "c" not in keys


def test_scope_key_rows_user_is_empty() -> None:
    assert scope_key_rows("user") == ()


def test_config_overlay_rows_describe_arrows_and_enter() -> None:
    # The config-overlay redesign: arrows navigate, Enter is the sole
    # mutator. Space (the old cycler) must not appear as a primary key.
    rows = config_overlay_rows()
    keys = [key for key, _ in rows]
    actions = " ".join(action for _, action in rows)
    assert "↑ / ↓" in keys
    assert "← / →" in keys
    assert "Enter" in keys
    # Vim keys are aliases only — surfaced in the action text, never as the
    # primary key column.
    assert "j" not in keys
    assert "k" not in keys
    assert "vim: k / j" in actions


def test_config_overlay_rows_enter_is_sole_mutator() -> None:
    # Enter carries the toggle / cycle / edit affordance; the legacy Space
    # cycler is gone from the keymap.
    rows = dict(config_overlay_rows())
    assert "toggle" in rows["Enter"]
    assert "cycle" in rows["Enter"]
    assert "edit" in rows["Enter"]
    assert "Space" not in rows


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
        from textual.containers import VerticalScroll

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            # The pane-nav keys sit at the top; the palette verbs scroll
            # below the fold (the keymap now also lists the config overlay).
            assert "PageUp" in app.export_screenshot()
            app.screen.query_one("#help-container", VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            assert "/find" in app.export_screenshot()

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
