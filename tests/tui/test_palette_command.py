"""Pilot tests for the C06 ``CommandPalette`` overlay (P26-W19).

Drives the palette through the real :class:`~eawf.surfaces.tui.app.EaApp` via
Textual's Pilot harness: ``/`` opens it pre-filled, typing fuzzy-filters
the option list, ``Tab`` autocompletes, ``Enter`` runs a verb + dismisses,
and ``Esc`` closes without executing. The pure registry/ranker tests live
in ``test_palette_verbs.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.palette.command_palette import (
    PALETTE_PREFIX,
    CommandPalette,
    _option_label,
)
from eawf.surfaces.tui.palette.verbs import VERBS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


# --------------------------------------------------------------------------
# Open + seed
# --------------------------------------------------------------------------


def test_palette_opens_on_slash_prefilled() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert isinstance(app.screen, CommandPalette)
            assert app.modal_depth() == 1
            palette_input = app.screen.query_one("#palette-input", Input)
            assert palette_input.value == PALETTE_PREFIX

    asyncio.run(body())


def test_palette_seeds_full_verb_list_for_scope() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            option_list = app.screen.query_one(OptionList)
            # All repo-visible verbs are listed before any typing (the
            # leading "/" matches every verb name).
            assert option_list.option_count > 10

    asyncio.run(body())


# --------------------------------------------------------------------------
# Fuzzy filter
# --------------------------------------------------------------------------


def test_palette_filters_as_operator_types() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("t", "h", "e", "m", "e")
            await pilot.pause()
            option_list = app.screen.query_one(OptionList)
            # The sole match renders under its section header (a disabled
            # row), so the runnable verb is the only selectable option.
            ids = [option_list.get_option_at_index(i).id for i in range(option_list.option_count)]
            assert "/theme" in ids
            selectable = [
                option_list.get_option_at_index(i).id
                for i in range(option_list.option_count)
                if not option_list.get_option_at_index(i).disabled
            ]
            assert selectable == ["/theme"]

    asyncio.run(body())


def test_palette_no_match_shows_frozen_prompt() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.palette.command_palette import NO_MATCH_PROMPT

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("z", "z", "z", "z")
            await pilot.pause()
            option_list = app.screen.query_one(OptionList)
            # No verb matches: a single disabled row pins the frozen prompt.
            assert option_list.option_count == 1
            prompt = option_list.get_option_at_index(0)
            assert str(prompt.prompt) == NO_MATCH_PROMPT
            assert prompt.disabled is True

    asyncio.run(body())


# --------------------------------------------------------------------------
# Tab autocomplete
# --------------------------------------------------------------------------


def test_palette_tab_autocompletes_to_highlighted() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("t", "h", "e")
            await pilot.press("tab")
            await pilot.pause()
            palette_input = app.screen.query_one("#palette-input", Input)
            assert palette_input.value == "/theme "

    asyncio.run(body())


# --------------------------------------------------------------------------
# Enter runs + dismisses
# --------------------------------------------------------------------------


def test_palette_enter_runs_quit_verb() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("q", "u", "i", "t")
            await pilot.press("enter")
            await pilot.pause()
            # /quit handler exits the app.
            assert app._exit is True

    asyncio.run(body())


def test_palette_enter_help_opens_help_and_closes_palette() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("h", "e", "l", "p")
            await pilot.press("enter")
            await pilot.pause()
            from eawf.surfaces.tui.screens.help import HelpScreen

            # Palette dismissed, help pushed in its place (depth stays 1).
            assert isinstance(app.screen, HelpScreen)
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_palette_unknown_verb_keeps_palette_open() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            # Type a non-verb then submit; palette stays open.
            for char in "zztop":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, CommandPalette)

    asyncio.run(body())


def test_palette_empty_enter_dismisses() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            # Palette opens seeded with just "/"; Enter on the bare prefix
            # closes it instead of toasting an "unknown verb".
            assert isinstance(app.screen, CommandPalette)
            await pilot.press("enter")
            await pilot.pause()
            assert app.modal_depth() == 0
            assert app._exit is False

    asyncio.run(body())


def test_palette_cleared_then_enter_dismisses() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            # Clear the seeded "/" to an empty input, then Enter dismisses.
            palette_input = app.screen.query_one("#palette-input", Input)
            palette_input.value = ""
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# Esc closes without executing
# --------------------------------------------------------------------------


def test_palette_esc_closes_without_running() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0
            assert app._exit is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# Scope-aware verb visibility
# --------------------------------------------------------------------------


def test_palette_user_scope_hides_removed_placeholder_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate the user registry to a tmp home so the synthesized portfolio
    # is deterministic and never reads the real ~/.eawf/registry.json.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ea_dir = tmp_path / ".eawf"
    ea_dir.mkdir(parents=True, exist_ok=True)
    (ea_dir / "registry.json").write_text(
        '{"version":"1","repos":{"ABC":{"code":"ABC","path":"/abs/path/abc"}}}',
        encoding="utf-8",
    )

    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("w", "a", "v", "e")
            await pilot.pause()
            option_list = app.screen.query_one(OptionList)
            ids = {option_list.get_option_at_index(i).id for i in range(option_list.option_count)}
            # No placeholder /wave* verb is offered.
            assert not any((i or "").startswith("/wave") for i in ids)

    asyncio.run(body())


# --------------------------------------------------------------------------
# _option_label rendering
# --------------------------------------------------------------------------


def test_option_label_includes_name_and_hint() -> None:
    verb = next(v for v in VERBS if v.name == "/find")
    label = _option_label(verb)
    assert "/find" in label
    assert verb.hint in label
    assert verb.args_grammar in label
