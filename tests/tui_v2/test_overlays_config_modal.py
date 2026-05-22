"""Pilot + unit tests for the ``ConfigModal`` config window (tui_v2).

Covers the registry-driven tabbed config overlay after the
Enter-as-sole-mutator redesign (P26-I02-W01):

* **registry coverage** — the modal renders a tab per
  :func:`~eawf.config.registry.tabs_sorted` and a field row per
  :func:`~eawf.config.registry.keys_for_tab`, so a dropped registry key
  fails the coverage test (the modal cannot hardcode a subset).
* **keymap** — ``↑`` / ``↓`` move the field cursor (clamped to the first /
  last field of the active tab); ``←`` / ``→`` switch tabs; ``Enter`` is
  the sole mutator (toggle bool / forward-cycle choice / inline-edit
  scalar / popup for a multi-line ``str``).
* **type→action dispatch** — :func:`enter_action` / :func:`needs_popup_edit`
  map a field's type + value onto the action without mounting Textual.
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

from textual.widgets import Input, Static, TabbedContent

from eawf.config.registry import (
    CONFIG_REGISTRY,
    ConfigKey,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)
from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays.config_modal import (
    ConfigModal,
    ConfigModalState,
    current_value,
    cycle_choice,
    enter_action,
    format_value,
    needs_popup_edit,
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


def test_cycle_choice_forward_a_b_c() -> None:
    """Forward-cycle steps a -> b -> c (the Enter-mutator direction)."""
    entry = registry_lookup("planning.approval")  # ("ask", "auto", "never")
    assert entry is not None
    after_first = cycle_choice(entry, {"planning": {"approval": "ask"}}, {}, step=1)
    assert after_first == {"planning.approval": "auto"}
    after_second = cycle_choice(entry, {}, after_first, step=1)
    assert after_second == {"planning.approval": "never"}


def test_format_value_bool_lowercase() -> None:
    entry = registry_lookup("audit.fix_safe")
    assert entry is not None
    assert format_value(entry, True) == "true"
    assert format_value(entry, False) == "false"


# -- type -> action dispatch (enter_action / needs_popup_edit) --------------


def test_enter_action_bool_toggles() -> None:
    entry = registry_lookup("audit.fix_safe")  # bool
    assert entry is not None
    assert enter_action(entry, False, row_width=80) == "toggle"


def test_enter_action_choice_cycles() -> None:
    entry = registry_lookup("planning.approval")  # choice
    assert entry is not None
    assert enter_action(entry, "ask", row_width=80) == "cycle"


def test_enter_action_int_inline() -> None:
    entry = registry_lookup("audit.flaky_retry_count")  # int
    assert entry is not None
    assert enter_action(entry, 2, row_width=80) == "inline"


def _str_key() -> ConfigKey:
    """Return a ``str`` registry entry, or a synthetic one when none exists."""
    for entry in CONFIG_REGISTRY:
        if entry.type == "str":
            return entry
    return ConfigKey(tab="t", key="t.scalar", label="scalar str", type="str", default="")


def _multiline_str_key() -> ConfigKey:
    """Return a synthetic multi-line ``str`` entry (no registry row sets it)."""
    return ConfigKey(tab="t", key="t.note", label="note", type="str", default="", multiline=True)


def test_enter_action_short_str_inline() -> None:
    """A short single-line ``str`` value edits inline."""
    entry = _str_key()
    assert enter_action(entry, "short", row_width=80) == "inline"


def test_enter_action_multiline_str_popup() -> None:
    """A ``multiline``-flagged ``str`` field routes to the popup editor."""
    entry = _multiline_str_key()
    assert enter_action(entry, "anything", row_width=80) == "popup"


def test_enter_action_str_with_newline_popup() -> None:
    """A value containing a newline routes to the popup editor."""
    entry = _str_key()
    assert enter_action(entry, "line1\nline2", row_width=80) == "popup"


def test_enter_action_str_over_row_width_popup() -> None:
    """A value wider than the row routes to the popup editor."""
    entry = _str_key()
    assert enter_action(entry, "x" * 30, row_width=10) == "popup"


def test_needs_popup_edit_false_for_non_str() -> None:
    """Non-``str`` types never use the popup (toggle / cycle / inline only)."""
    for key in ("audit.fix_safe", "planning.approval", "audit.flaky_retry_count"):
        entry = registry_lookup(key)
        assert entry is not None
        assert needs_popup_edit(entry, entry.default, row_width=1) is False


def test_needs_popup_edit_zero_width_disables_width_check() -> None:
    """A non-positive width disables the over-wide check (newline still routes)."""
    entry = _str_key()
    assert needs_popup_edit(entry, "x" * 200, row_width=0) is False
    assert needs_popup_edit(entry, "a\nb", row_width=0) is True


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


def _goto_tab(modal: ConfigModal, tab: str) -> None:
    """Activate *tab* and clear focus the way the modal's own switch does."""
    modal.query_one("#config-tabs", TabbedContent).active = modal._tab_pane_id(tab)
    modal.set_focus(None)
    modal.field_index = 0


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
            assert len(expected) == len(tabs_sorted())

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
            assert rendered == {entry.key for entry in CONFIG_REGISTRY}

    asyncio.run(body())


def test_config_opens_focused_on_first_field() -> None:
    """The modal opens with the cursor on field 0 (immediately actionable)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            assert modal.field_index == 0
            row = modal.query_one(f"#{modal._field_row_id(modal._active_tab(), 0)}", Static)
            assert str(row.render()).lstrip().startswith(">")

    asyncio.run(body())


def test_config_cursor_down_then_up_moves_field() -> None:
    """``↓`` then ``↑`` step the field cursor within the active tab."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            assert len(keys_for_tab(modal._active_tab())) >= 2
            await pilot.press("down")
            await pilot.pause()
            assert modal.field_index == 1
            await pilot.press("up")
            await pilot.pause()
            assert modal.field_index == 0

    asyncio.run(body())


def test_config_cursor_up_clamps_at_first_field() -> None:
    """``↑`` on the first field is a no-op (clamped, no wrap)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            assert modal.field_index == 0
            await pilot.press("up")
            await pilot.pause()
            assert modal.field_index == 0

    asyncio.run(body())


def test_config_cursor_down_clamps_at_last_field() -> None:
    """``↓`` on the last field is a no-op (clamped, no wrap)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            last = len(keys_for_tab(modal._active_tab())) - 1
            for _ in range(last + 3):  # over-press past the bottom
                await pilot.press("down")
                await pilot.pause()
            assert modal.field_index == last

    asyncio.run(body())


def test_config_left_right_switch_tabs() -> None:
    """``←`` / ``→`` switch the active tab and reset the field cursor."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            assert modal._active_tab() == tabs_sorted()[0]
            await pilot.press("right")
            await pilot.pause()
            assert modal._active_tab() == tabs_sorted()[1]
            assert modal.field_index == 0
            await pilot.press("left")
            await pilot.pause()
            assert modal._active_tab() == tabs_sorted()[0]

    asyncio.run(body())


def test_config_left_wraps_to_last_tab() -> None:
    """``←`` from the first tab wraps to the last."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            assert modal._active_tab() == tabs_sorted()[0]
            await pilot.press("left")
            await pilot.pause()
            assert modal._active_tab() == tabs_sorted()[-1]

    asyncio.run(body())


def test_config_enter_toggles_bool() -> None:
    """``Enter`` on a bool field toggles it (the sole mutator)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = modal._active_field()  # audit.fix_safe (bool)
            assert entry is not None and entry.type == "bool"
            before = current_value(entry, modal._merged, modal._view.dirty)
            await pilot.press("enter")
            await pilot.pause()
            after = current_value(entry, modal._merged, modal._view.dirty)
            assert after != before
            assert entry.key in modal._view.dirty

    asyncio.run(body())


def test_config_enter_cycles_choice_a_b_c_a() -> None:
    """``Enter`` forward-cycles a -> b -> c -> a; the wrap back to the persisted
    value clears the dirty mark (no spurious unsaved edit)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "planning")
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.type == "choice"
            choices = list(entry.choices or ())
            assert len(choices) >= 3
            start = current_value(entry, modal._merged, modal._view.dirty)
            start_index = choices.index(str(start))
            # Press Enter len(choices) times: each step advances the displayed
            # value, and the final wrap back to the persisted start clears dirty.
            seen: list[str] = []
            for i in range(len(choices)):
                await pilot.press("enter")
                await pilot.pause()
                value = current_value(entry, modal._merged, modal._view.dirty)
                seen.append(value)
                expected_value = choices[(start_index + i + 1) % len(choices)]
                assert value == expected_value
                if expected_value == str(start):
                    assert entry.key not in modal._view.dirty  # wrapped → no dirty mark
                else:
                    assert modal._view.dirty[entry.key] == expected_value
            expected = [choices[(start_index + i + 1) % len(choices)] for i in range(len(choices))]
            assert seen == expected
            assert seen[-1] == str(start)  # wrapped back to start

    asyncio.run(body())


def test_config_enter_opens_inline_edit_for_int() -> None:
    """``Enter`` on an int field mounts the inline ``Input`` editor in the row."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # audit.flaky_retry_count (int)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.type == "int"
            await pilot.press("enter")
            await pilot.pause()
            assert modal._editing_key == entry.key
            # The modal stays the active screen (no popup pushed).
            assert app.screen is modal
            assert modal.query_one("#config-inline-input", Input) is not None

    asyncio.run(body())


def test_config_inline_edit_commit_stages_value() -> None:
    """``Enter`` in the inline input commits the coerced value to dirty."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # audit.flaky_retry_count (int, 0..5)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            await pilot.press("enter")  # open inline editor
            await pilot.pause()
            modal.query_one("#config-inline-input", Input).value = "4"
            await pilot.press("enter")  # commit
            await pilot.pause()
            assert modal._editing_key is None  # editor torn down
            assert modal._view.dirty.get(entry.key) == 4  # coerced int, not "4"

    asyncio.run(body())


def test_config_inline_edit_same_value_leaves_no_dirty() -> None:
    """Committing the field's current value stages no edit (no ``*`` / no tint).

    Review fix: re-entering the existing value (e.g. typing ``4`` into
    ``planning.max_parallel_waves`` whose default is ``4``) must not mark the
    field dirty — nothing actually changed.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "planning")
            modal.field_index = 2  # planning.max_parallel_waves (int, default 4)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.key == "planning.max_parallel_waves"
            persisted = modal._persisted_value(entry)
            await pilot.press("enter")  # open inline editor
            await pilot.pause()
            modal.query_one("#config-inline-input", Input).value = str(persisted)
            await pilot.press("enter")  # commit the unchanged value
            await pilot.pause()
            assert modal._editing_key is None
            assert entry.key not in modal._view.dirty  # no spurious dirty mark

    asyncio.run(body())


def test_config_toggle_twice_clears_dirty() -> None:
    """Toggling a bool back to its persisted value clears the dirty mark."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = modal._active_field()  # audit.fix_safe (bool)
            assert entry is not None and entry.type == "bool"
            await pilot.press("enter")  # toggle once → dirty
            await pilot.pause()
            assert entry.key in modal._view.dirty
            await pilot.press("enter")  # toggle back → matches persisted, no dirty
            await pilot.pause()
            assert entry.key not in modal._view.dirty

    asyncio.run(body())


def test_config_inline_edit_input_renders_wider_than_one_cell() -> None:
    """The inline ``Input`` keeps a usable width so the seeded value is visible.

    Regression for W14 issue 5a: the meta-line label had no CSS width and
    grew to the full row, starving the ``1fr`` input down to a single cell
    (the value was present but clipped to invisibility). With the label
    constrained to ``width: auto`` the input must claim the remaining row.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # audit.flaky_retry_count (int)
            await pilot.pause()
            await pilot.press("enter")  # open inline editor
            await pilot.pause()
            input_width = modal.query_one("#config-inline-input", Input).size.width
            assert input_width > 1

    asyncio.run(body())


def test_config_inline_edit_label_aligns_with_static_row() -> None:
    """The inline meta-line key column matches the static field row's key column.

    Regression for W14 issue 5b: the static row reserves caret + dirty
    marker + space (three cells) ahead of the key, but the inline meta line
    started the key at column 0, so opening the editor jumped the key three
    columns left. The 3-space prefix realigns them.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # audit.flaky_retry_count (int)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            static_line = modal._field_line(entry, selected=True)
            meta_line = modal._meta_line(entry)
            # Both lines place the key at the same column.
            assert static_line.index(entry.key) == meta_line.index(entry.key)

    asyncio.run(body())


def test_meta_line_prefixes_three_spaces_for_alignment() -> None:
    """``_meta_line`` leads with three spaces (caret + dirty + separator)."""
    entry = registry_lookup("audit.flaky_retry_count")
    assert entry is not None
    line = ConfigModal._meta_line(entry)
    assert line.startswith("   audit.flaky_retry_count")
    # The key column matches the static row (caret + dirty + space = 3).
    assert line.index("audit.flaky_retry_count") == 3


def test_config_inline_edit_type_cell_aligns_with_static_row() -> None:
    """The inline meta line's ``[type]`` cell shares the static row's column.

    Review fix for the "row squished after Enter": the old meta line packed
    key + type + range with single separators, so the type cell (and the
    trailing input) bunched left of the static value column. Reusing the
    static column widths realigns them, landing the input in the value column.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # audit.flaky_retry_count (int)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            static_line = modal._field_line(entry, selected=True)
            meta_line = modal._meta_line(entry)
            type_cell = f"[{entry.type}]"
            # Both the key and the type cell share a column with the static row,
            # so the trailing input lands in the static value column.
            assert static_line.index(entry.key) == meta_line.index(entry.key)
            assert static_line.index(type_cell) == meta_line.index(type_cell)

    asyncio.run(body())


def test_config_inline_edit_esc_cancels_without_mutation() -> None:
    """``Esc`` in the inline input aborts the edit and stages nothing."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            await pilot.press("enter")  # open inline editor
            await pilot.pause()
            modal.query_one("#config-inline-input", Input).value = "4"
            await pilot.press("escape")  # cancel
            await pilot.pause()
            assert modal._editing_key is None
            assert entry.key not in modal._view.dirty
            # The modal stays open (Esc cancelled only the inline edit).
            assert app.screen is modal

    asyncio.run(body())


def test_config_inline_edit_out_of_range_int_shows_error_no_mutation() -> None:
    """An out-of-range int reports inline and stages nothing; editor stays open."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # audit.flaky_retry_count (max 5)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            await pilot.press("enter")
            await pilot.pause()
            modal.query_one("#config-inline-input", Input).value = "99"
            await pilot.press("enter")  # commit attempt — should fail validation
            await pilot.pause()
            # Editor still open, error surfaced, nothing staged.
            assert modal._editing_key == entry.key
            error = modal.query_one("#config-edit-error", Static)
            assert "maximum" in str(error.render())
            assert entry.key not in modal._view.dirty

    asyncio.run(body())


def test_config_enter_str_with_newline_routes_to_popup() -> None:
    """A ``str`` whose current value has a newline opens the popup editor.

    The operator-tunable ``CONFIG_REGISTRY`` carries no ``str`` field
    today (only ``bool`` / ``choice`` / ``int``), so the multi-line ``str``
    routing is driven through the real ``action_edit`` dispatch with a
    synthetic ``str`` field substituted for the focused field and a
    newline value staged in the dirty map.
    """

    async def body() -> None:
        synthetic = ConfigKey(tab="audit", key="audit.note", label="note", type="str", default="")
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal._active_field = lambda: synthetic  # type: ignore[method-assign]
            modal._view.dirty = {synthetic.key: "line1\nline2"}
            modal.action_edit()
            await pilot.pause()
            # Routed to the larger popup editor, not an inline input.
            assert isinstance(app.screen, EditFieldModal)
            assert modal._editing_key is None

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
            await pilot.press("enter")  # toggle the first bool — stages one edit
            await pilot.pause()
            assert modal._view.dirty
            await pilot.press("s")
            await pilot.pause()
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
            await pilot.press("enter")  # toggle bool
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
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_config_esc_dirty_prompts_confirm() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            await pilot.press("enter")  # stage an edit (toggle bool)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
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
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_config_hint_describes_arrows_and_enter() -> None:
    """The footer hint advertises arrow nav + Enter-as-mutator, not Space."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            hint = str(modal.query_one("#config-hint", Static).render())
            assert "↑/↓ field" in hint
            assert "←/→ tab" in hint
            assert "Enter edit" in hint
            assert "Space" not in hint

    asyncio.run(body())


def test_config_hint_flips_during_inline_edit() -> None:
    """While inline-editing, the hint shows the commit / cancel keys."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            modal.field_index = 1  # int field
            await pilot.pause()
            await pilot.press("enter")  # open inline editor
            await pilot.pause()
            hint = str(modal.query_one("#config-hint", Static).render())
            assert "Enter commit" in hint
            assert "Esc cancel" in hint

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
            assert app.modal_depth() == 3

    asyncio.run(body())
