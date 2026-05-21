"""Pilot + unit tests for the C06 ``ConfigModal`` config window (P26-W34).

Covers the registry-driven tabbed config overlay:

* **registry coverage** — the modal renders a tab per
  :func:`~eawf.config.registry.tabs_sorted` and a field row per
  :func:`~eawf.config.registry.keys_for_tab`, so a dropped registry key
  fails the coverage test (the modal cannot hardcode a subset).
* **per-type interaction** — ``Space`` toggles a bool, ``←`` / ``→``
  cycle an enum, ``Enter`` opens the scalar :class:`EditFieldModal`.
* **save path** — ``s`` flushes staged edits through the layered-config
  writer seam and NEVER writes ``state.json``.
* **layer cycle** — ``L`` rotates the writable layer.
* **dirty guard** — ``Esc`` on a dirty modal prompts before discarding;
  on a clean modal it closes immediately.
* **wiring** — the App ``action_open_config`` and the ``/config`` palette
  verb both open the window.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Static, TabbedContent

from eawf.config.registry import CONFIG_REGISTRY, keys_for_tab, registry_lookup, tabs_sorted
from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays.config_modal import (
    ConfigModal,
    ConfigModalState,
    current_value,
    cycle_choice,
    format_value,
    save_dirty_fields,
    toggle_bool,
    writable_layers_for,
)
from eawf.tui_v2.screens.overlays.confirm import ConfirmModal
from eawf.tui_v2.screens.overlays.edit_field import EditFieldModal

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no Textual mount)
# ---------------------------------------------------------------------------


def test_config_modal_state_is_strict() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConfigModalState(layer="repo", bogus=1)  # type: ignore[call-arg]


def test_writable_layers_repo_anchor() -> None:
    layers = writable_layers_for(None, Path("/tmp/repo"))
    assert layers == ("global", "repo", "local")


def test_writable_layers_global_only_without_anchors() -> None:
    assert writable_layers_for(None, None) == ("global",)


def test_current_value_dirty_wins() -> None:
    entry = registry_lookup("audit.fix_safe")
    assert entry is not None
    merged = {"audit": {"fix_safe": False}}
    assert current_value(entry, merged, {"audit.fix_safe": True}) is True


def test_current_value_falls_back_to_default() -> None:
    entry = registry_lookup("audit.fix_safe")
    assert entry is not None
    assert current_value(entry, {}, {}) == entry.default


def test_toggle_bool_flips() -> None:
    entry = registry_lookup("audit.fix_safe")
    assert entry is not None
    assert toggle_bool(entry, {"audit": {"fix_safe": False}}, {}) == {"audit.fix_safe": True}


def test_toggle_bool_noop_on_non_bool() -> None:
    entry = registry_lookup("planning.approval")
    assert entry is not None
    assert toggle_bool(entry, {}, {}) == {}


def test_cycle_choice_wraps() -> None:
    entry = registry_lookup("planning.approval")  # choices ("ask", "auto", "never")
    assert entry is not None
    merged = {"planning": {"approval": "never"}}
    # Cycling forward from the last choice wraps to the first.
    assert cycle_choice(entry, merged, {}, step=1) == {"planning.approval": "ask"}


def test_cycle_choice_backward() -> None:
    entry = registry_lookup("planning.approval")
    assert entry is not None
    merged = {"planning": {"approval": "ask"}}
    assert cycle_choice(entry, merged, {}, step=-1) == {"planning.approval": "never"}


def test_format_value_bool_lowercase() -> None:
    entry = registry_lookup("audit.fix_safe")
    assert entry is not None
    assert format_value(entry, True) == "true"
    assert format_value(entry, False) == "false"


def test_save_dirty_fields_routes_through_save_fn_not_state_json() -> None:
    """``s`` save calls the layered writer seam; it never touches state.json."""
    calls: list[dict[str, Any]] = []

    def fake_save(**kwargs: Any) -> None:
        calls.append(kwargs)

    repo = Path("/tmp/repo")
    saved = save_dirty_fields(
        {"audit.fix_safe": True, "planning.approval": "auto"},
        layer="repo",
        workspace=None,
        repo=repo,
        save_fn=fake_save,
    )
    assert saved == 2
    # Each call targets the repo layer's YAML — never a state.json path.
    targets = [str(call["target_path"]) for call in calls]
    assert all(target.endswith(".yaml") for target in targets), targets
    assert not any("state.json" in target for target in targets), targets
    keys = {call["key"] for call in calls}
    assert keys == {"audit.fix_safe", "planning.approval"}


def test_save_dirty_fields_empty_is_noop() -> None:
    calls: list[dict[str, Any]] = []
    saved = save_dirty_fields(
        {},
        layer="repo",
        workspace=None,
        repo=Path("/tmp/repo"),
        save_fn=lambda **k: calls.append(k),
    )
    assert saved == 0
    assert calls == []


# ---------------------------------------------------------------------------
# Pilot tests (Textual mount)
# ---------------------------------------------------------------------------


def _push_config(app: EaApp, save_fn: Any = None) -> ConfigModal:
    modal = ConfigModal(workspace=None, repo=Path("/tmp/repo"), save_fn=save_fn)
    app.push_screen(modal)
    return modal


def test_config_renders_every_registry_tab() -> None:
    """A registry-driven modal shows a tab per ``tabs_sorted()`` — none missing."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            tabs = modal.query_one("#config-tabs", TabbedContent)
            pane_ids = {pane.id for pane in tabs.query("TabPane")}
            expected = {modal._tab_pane_id(tab) for tab in tabs_sorted()}
            assert pane_ids == expected
            # All 9 tabs are present.
            assert len(expected) == len(tabs_sorted()) == 9

    asyncio.run(body())


def test_config_renders_every_registry_key() -> None:
    """Every ``CONFIG_REGISTRY`` key has a field row — a dropped key fails here."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            rendered: set[str] = set()
            for tab in tabs_sorted():
                for index, entry in enumerate(keys_for_tab(tab)):
                    row = modal.query_one(f"#{modal._field_row_id(tab, index)}", Static)
                    text = str(row.render())
                    assert entry.key in text, (tab, entry.key, text)
                    rendered.add(entry.key)
            # The set of rendered keys equals the full registry — exact cover.
            assert rendered == {entry.key for entry in CONFIG_REGISTRY}

    asyncio.run(body())


def test_config_space_toggles_bool() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            # Active tab is the first ("audit"); first field is audit.fix_safe (bool).
            entry = modal._active_field()
            assert entry is not None and entry.type == "bool"
            before = current_value(entry, modal._merged, modal._view.dirty)
            await pilot.press("space")
            await pilot.pause()
            after = current_value(entry, modal._merged, modal._view.dirty)
            assert after != before
            assert entry.key in modal._view.dirty

    asyncio.run(body())


def test_config_arrows_cycle_enum() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            # Navigate to the "planning" tab which has an enum (approval).
            # Setting ``active`` directly re-focuses the tab bar, so clear
            # focus the way the modal's own tab switch does (keeps the
            # screen's arrow bindings winning).
            tabs = modal.query_one("#config-tabs", TabbedContent)
            tabs.active = modal._tab_pane_id("planning")
            modal.set_focus(None)
            modal.field_index = 0
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.type == "choice", entry
            await pilot.press("right")
            await pilot.pause()
            assert entry.key in modal._view.dirty
            staged = modal._view.dirty[entry.key]
            assert staged in (entry.choices or ())

    asyncio.run(body())


def test_config_enter_opens_edit_field_for_scalar() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            # audit.flaky_retry_count (int) is the 2nd field on the audit tab.
            modal.field_index = 1
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.type == "int"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditFieldModal)

    asyncio.run(body())


def test_config_save_calls_layered_writer_seam() -> None:
    """``s`` flushes staged edits through the injected layered-writer seam."""

    async def body() -> None:
        calls: list[dict[str, Any]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app, save_fn=lambda **k: calls.append(k))
            await pilot.pause()
            # Toggle the first bool to stage one edit, then save.
            await pilot.press("space")
            await pilot.pause()
            assert modal._view.dirty
            await pilot.press("s")
            await pilot.pause()
            # Save flushed through the seam and cleared the dirty map.
            assert len(calls) == 1
            assert str(calls[0]["target_path"]).endswith(".yaml")
            assert "state.json" not in str(calls[0]["target_path"])
            assert modal._view.dirty == {}

    asyncio.run(body())


def test_config_reset_drops_staged_edits() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            assert modal._view.dirty
            await pilot.press("r")
            await pilot.pause()
            assert modal._view.dirty == {}

    asyncio.run(body())


def test_config_l_cycles_writable_layer() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            assert modal._view.layer == "repo"  # default with a repo anchor
            await pilot.press("L")
            await pilot.pause()
            # Cycles to the next layer in writable_layers_for order.
            assert modal._view.layer != "repo"
            assert modal._view.layer in modal._layers

    asyncio.run(body())


def test_config_esc_clean_closes_immediately() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_config(app)
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            # Clean modal closes with no confirm prompt.
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_config_esc_dirty_prompts_confirm() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            await pilot.press("space")  # stage an edit
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            # Dirty Esc pushes a ConfirmModal rather than dismissing.
            assert isinstance(app.screen, ConfirmModal)
            assert modal in app.screen_stack

    asyncio.run(body())


def test_config_esc_dirty_discard_confirmed_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_config(app)
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            # Confirm "Yes" (right then enter) discards and closes both modals.
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Wiring tests — c keypress + /config palette verb
# ---------------------------------------------------------------------------


def test_action_open_config_pushes_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_open_config()
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_c_keypress_opens_config_on_repo_scope() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_config_palette_verb_opens_modal() -> None:
    async def body() -> None:
        from eawf.tui_v2.palette.verbs import _handle_config

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_config(app, "")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_config_verb_registered_in_palette() -> None:
    from eawf.tui_v2.palette.verbs import VERBS

    names = {verb.name for verb in VERBS}
    assert "/config" in names


def test_config_verb_respects_modal_cap() -> None:
    async def body() -> None:
        from eawf.tui_v2.palette.verbs import _handle_config

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(3):
                _push_config(app)
                await pilot.pause()
            assert app.modal_depth() == 3
            _handle_config(app, "")
            await pilot.pause()
            # Cap holds — the verb's push is rejected, not stacked.
            assert app.modal_depth() == 3

    asyncio.run(body())
