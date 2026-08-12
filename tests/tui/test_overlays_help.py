"""Tests for the C06 ``HelpScreen`` overlay.

Pure keymap-row helpers (global / pane-nav / scope) plus Pilot-driven
behaviour: ``?`` opens the overlay, the D31 single-instance guard makes a
second ``?`` a no-op, the rendered overlay shows full key names + palette
verbs, and ``Esc`` closes it (clearing the guard).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.binding import Binding

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY
from eawf.surfaces.tui.screens.help import (
    HelpScreen,
    backlog_key_rows,
    config_overlay_rows,
    global_key_rows,
    mode_action_key_rows,
    pane_nav_rows,
    reference_nav_rows,
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


def test_backlog_key_rows_document_clear_filter() -> None:
    # W26: the backlog filter is *set* via /filter and *cleared* in-pane
    # with ``x``; the help must surface that key (and the closed toggle).
    rows = dict(backlog_key_rows())
    assert "x" in rows
    assert "clear" in rows["x"].lower()
    assert "c" in rows
    assert "closed" in rows["c"].lower()


def test_global_esc_row_no_longer_claims_clear_filter() -> None:
    # W26: clearing the filter moved to the dedicated ``x`` key, so the
    # global Esc row must not claim an (unbacked) Esc-clears-filter path.
    rows = dict(global_key_rows())
    assert "clear filter" not in rows["Esc"]


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
# Mode action-key derivation + reference nav
# --------------------------------------------------------------------------


def _resolve_mode_screen_classes() -> dict[str, type]:
    """Resolve ``{mode_name: screen_class}`` via the live registry factories.

    Drives the resolution off :data:`MODE_REGISTRY` itself (not the help
    module's private map) so the coverage assertions stay independent of the
    code under test: each mode's factory is invoked against a real app to
    learn which screen class that mode actually boots. Home returns its
    scope-screen *name* (a ``str``), not a screen, so it is skipped -- it
    owns no mode-specific BINDINGS.

    Returns:
        A ``{mode_name: screen_class}`` map for every non-Home mode.
    """
    app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
    classes: dict[str, type] = {}
    for spec in MODE_REGISTRY:
        built = spec.factory(app)
        if isinstance(built, str):
            continue
        classes[spec.name] = type(built)
    return classes


def test_mode_action_key_rows_skip_home() -> None:
    # Home reuses the scope screen and owns no mode-specific BINDINGS, so it
    # contributes no action-key subsection; every other registry mode does.
    titles = [title for title, _ in mode_action_key_rows()]
    assert "Home" not in titles
    non_home_titles = {spec.title for spec in MODE_REGISTRY if spec.name != "home"}
    assert set(titles) == non_home_titles


def test_mode_action_key_rows_cover_every_own_binding() -> None:
    # The criterion's coverage guarantee: for every non-Home mode, every key
    # the mode screen declares in its OWN BINDINGS must appear as a help row
    # produced by mode_action_key_rows() -- no binding is silently dropped.
    # Driven off MODE_REGISTRY so a future mode is covered automatically.
    help_sections = dict(mode_action_key_rows())
    classes = _resolve_mode_screen_classes()
    title_for = {spec.name: spec.title for spec in MODE_REGISTRY}

    for name, cls in classes.items():
        title = title_for[name]
        assert title in help_sections, f"mode {name!r} missing its help subsection"
        help_keys = {key for key, _ in help_sections[title]}
        own_bindings = cls.__dict__.get("BINDINGS", ())
        inherited_pairs = {
            (binding.key, binding.action)
            for base in cls.__mro__[1:]
            for binding in base.__dict__.get("BINDINGS", ())
            if isinstance(binding, Binding)
        }
        for binding in own_bindings:
            if (
                isinstance(binding, Binding)
                and (binding.key, binding.action) not in inherited_pairs
            ):
                assert binding.key in help_keys, (
                    f"binding {binding.key!r} of mode {name!r} has no help row"
                )


def test_mode_action_key_rows_use_binding_description() -> None:
    # Each row carries the binding's own description verbatim, so the help
    # reads the live action label (e.g. autopilot 'd' -> 'dispatch').
    sections = dict(mode_action_key_rows())
    autopilot = dict(sections["Autopilot"])
    assert autopilot["d"] == "dispatch"
    assert autopilot["K"] == "kill"


def test_mode_action_key_rows_omit_inherited_chrome() -> None:
    # A mode that declares no own BINDINGS (Feed) carries an empty row tuple
    # rather than re-listing the inherited ScopeScreen chrome (palette / help
    # / quit), which already lives under the global section.
    sections = dict(mode_action_key_rows())
    assert sections["Feed"] == ()
    assert dict(sections["Doctor"]) == {"f": "fix"}
    # The inherited chrome keys never leak into any mode subsection.
    all_keys = {key for rows in sections.values() for key, _ in rows}
    assert "slash" not in all_keys
    assert "question_mark" not in all_keys


def test_reference_nav_rows_surface_alt_arrows() -> None:
    # The alt-left / alt-right history nav is derived from EaApp.BINDINGS and
    # surfaced as help rows so the reference-stack nav is discoverable.
    rows = dict(reference_nav_rows())
    assert "alt+left" in rows
    assert "alt+right" in rows
    assert "back" in rows["alt+left"]
    assert "forward" in rows["alt+right"]


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


def test_help_renders_mode_action_and_reference_sections() -> None:
    async def body() -> None:
        from textual.containers import VerticalScroll

        from eawf.surfaces.tui.snapshot.pilot_harness import capture_screen_text

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await app.workers.wait_for_complete()
            # The new sections sit below the fold; scroll the card so they
            # render, then assert the section headings + a representative row
            # against the plain-text capture (the SVG screenshot fragments
            # multi-word strings across <text> runs and is unreliable here).
            container = app.screen.query_one("#help-container", VerticalScroll)
            container.scroll_to(y=18, animate=False)
            await pilot.pause()
            await app.workers.wait_for_complete()
            shot = capture_screen_text(app)
            assert "Mode action keys" in shot
            assert "Autopilot" in shot
            assert "dispatch" in shot
            # The honest "(navigation only)" note appears for a mode that
            # declares no own action keys (Evidence / Feed). Find it by content
            # so adding a legitimate mode action does not make this viewport
            # assertion brittle.
            navigation_only = ""
            for offset in range(40, 73, 4):
                container.scroll_to(y=offset, animate=False)
                await pilot.pause()
                navigation_only = capture_screen_text(app)
                if "(navigation only)" in navigation_only:
                    break
            assert "(navigation only)" in navigation_only
            # Scroll further to the reference-nav section (alt-arrow nav).
            ref = ""
            for offset in range(68, 101, 4):
                container.scroll_to(y=offset, animate=False)
                await pilot.pause()
                ref = capture_screen_text(app)
                if "Reference navigation" in ref:
                    break
            assert "Reference navigation" in ref
            assert "alt+left" in ref

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
