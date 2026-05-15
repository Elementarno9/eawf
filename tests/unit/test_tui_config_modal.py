"""Unit tests for the config modal pure helpers (P20-I01-W11).

Covers the navigation helpers, the per-type editor seeding, the
key-dispatch state machine, the dirty-map round-trip, and the save
helper's contract with the W10 layered writer.

Integration-level golden snapshots + hotkey wiring tests live in
``tests/integration/test_tui_config_modal.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.cli.errors import InvalidInput
from eawf.config.registry import CONFIG_REGISTRY, registry_lookup
from eawf.tui.config_modal import (
    MODAL_FOOTER,
    MODAL_FOOTER_EDIT,
    ConfigModalState,
    active_field,
    active_fields,
    active_tab,
    apply_key,
    begin_edit,
    build_footer_panel,
    build_form_panel,
    build_header_panel,
    build_input_panel,
    build_modal_frame,
    build_tabs_panel,
    commit_edit,
    render_modal,
    save_dirty,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_modal_footer_lists_primary_keys() -> None:
    """Footer carries the primary keymap: arrows, Tab, Enter, s, q/Esc."""
    assert MODAL_FOOTER.startswith("↑↓")
    assert "Tab" in MODAL_FOOTER
    assert "Enter" in MODAL_FOOTER
    assert "s save" in MODAL_FOOTER
    assert "Esc" in MODAL_FOOTER


def test_modal_footer_edit_mentions_commit_and_cancel() -> None:
    assert "Enter commit" in MODAL_FOOTER_EDIT
    assert "Esc cancel" in MODAL_FOOTER_EDIT


# ---------------------------------------------------------------------------
# ConfigModalState — Pydantic v2 strict
# ---------------------------------------------------------------------------


def test_config_modal_state_defaults() -> None:
    view = ConfigModalState()
    assert view.tab_index == 0
    assert view.field_index == 0
    assert view.editing is False
    assert view.edit_buffer == ""
    assert view.dirty == {}
    assert view.toast == ""


def test_config_modal_state_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ConfigModalState(  # type: ignore[call-arg]
            tab_index=0,
            bogus_field="oops",
        )


def test_config_modal_state_rejects_negative_index() -> None:
    with pytest.raises(ValidationError):
        ConfigModalState(tab_index=-1)
    with pytest.raises(ValidationError):
        ConfigModalState(field_index=-1)


def test_config_modal_state_model_copy_update() -> None:
    view = ConfigModalState(tab_index=0)
    new_view = view.model_copy(update={"tab_index": 2})
    assert view.tab_index == 0
    assert new_view.tab_index == 2


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------


def test_active_tab_returns_first_alphabetical_at_default_index() -> None:
    """tab_index=0 maps to the first alphabetical tab in the registry."""
    view = ConfigModalState()
    tab = active_tab(view)
    # First registry tab alphabetically is "audit".
    assert tab == "audit"


def test_active_tab_wraps_on_oversized_index() -> None:
    """Stale tab_index past the end wraps via modulo."""
    view = ConfigModalState(tab_index=999)
    tab = active_tab(view)
    assert tab  # should be a real tab name


def test_active_fields_returns_alphabetical_keys_for_tab() -> None:
    view = ConfigModalState()  # tab=audit
    fields = active_fields(view)
    keys = [f.key for f in fields]
    assert keys == sorted(keys)
    assert all(f.tab == "audit" for f in fields)


def test_active_field_returns_first_field_at_default_index() -> None:
    view = ConfigModalState()  # tab=audit, field=0
    entry = active_field(view)
    assert entry is not None
    assert entry.tab == "audit"


def test_active_field_clamps_oversized_index() -> None:
    """Stale field_index past the end clamps to the last entry."""
    view = ConfigModalState(field_index=999)
    entry = active_field(view)
    assert entry is not None
    assert entry.tab == "audit"


# ---------------------------------------------------------------------------
# Key dispatch (navigation mode)
# ---------------------------------------------------------------------------


def test_apply_key_tab_advances_tab_index_and_resets_field() -> None:
    view = ConfigModalState(tab_index=0, field_index=3)
    out = apply_key(view, "\t")
    assert out.tab_index == 1
    assert out.field_index == 0


def test_apply_key_shift_tab_retreats_with_wrap() -> None:
    """Shift-Tab from tab_index=0 wraps to the last tab."""
    view = ConfigModalState(tab_index=0)
    out = apply_key(view, "\x1b[Z")
    # Wraps to last tab.
    from eawf.config.registry import tabs_sorted

    assert out.tab_index == len(tabs_sorted()) - 1


def test_apply_key_down_advances_field_cursor() -> None:
    view = ConfigModalState(field_index=0)
    out = apply_key(view, "\x1b[B")
    assert out.field_index == 1


def test_apply_key_up_retreats_field_cursor() -> None:
    view = ConfigModalState(field_index=1)
    out = apply_key(view, "\x1b[A")
    assert out.field_index == 0


def test_apply_key_up_clamps_at_zero() -> None:
    view = ConfigModalState(field_index=0)
    out = apply_key(view, "\x1b[A")
    assert out.field_index == 0


def test_apply_key_down_clamps_at_max() -> None:
    """Cursor cannot escape past the last field in the tab."""
    view = ConfigModalState()
    fields = active_fields(view)
    n = len(fields)
    view = view.model_copy(update={"field_index": n - 1})
    out = apply_key(view, "\x1b[B")
    assert out.field_index == n - 1


def test_apply_key_vim_aliases_act_as_arrow_keys() -> None:
    # Pick a tab with at least 3 fields so j/k can move both directions.
    from eawf.config.registry import tabs_sorted

    # planning has 4 fields (approval, auto_plan, max_parallel_waves,
    # require_research_for_unknowns); use that.
    planning_idx = tabs_sorted().index("planning")
    view = ConfigModalState(tab_index=planning_idx, field_index=1)
    assert apply_key(view, "j").field_index == 2
    assert apply_key(view, "k").field_index == 0


def test_apply_key_enter_opens_editor_with_seeded_buffer() -> None:
    view = ConfigModalState()
    out = apply_key(view, "\r")
    assert out.editing is True
    # First entry is audit.fix_safe (bool, default False).
    assert out.edit_buffer == "false"


def test_apply_key_unknown_key_returns_view_unchanged() -> None:
    view = ConfigModalState(tab_index=1, field_index=2)
    out = apply_key(view, "z")
    assert out.tab_index == 1
    assert out.field_index == 2


# ---------------------------------------------------------------------------
# Key dispatch (editing mode)
# ---------------------------------------------------------------------------


def test_apply_key_editing_appends_printable_char() -> None:
    view = ConfigModalState(editing=True, edit_buffer="hel")
    out = apply_key(view, "l")
    assert out.edit_buffer == "hell"


def test_apply_key_editing_backspace_removes_last_char() -> None:
    view = ConfigModalState(editing=True, edit_buffer="hello")
    out = apply_key(view, "\x7f")
    assert out.edit_buffer == "hell"


def test_apply_key_editing_backspace_on_empty_buffer_is_noop() -> None:
    view = ConfigModalState(editing=True, edit_buffer="")
    out = apply_key(view, "\x7f")
    assert out.edit_buffer == ""


def test_apply_key_editing_esc_cancels_edit() -> None:
    view = ConfigModalState(editing=True, edit_buffer="staged value")
    out = apply_key(view, "\x1b")
    assert out.editing is False
    assert out.edit_buffer == ""


def test_apply_key_editing_csi_arrow_ignored() -> None:
    """Arrow keys (CSI sequences) inside the editor are silently ignored."""
    view = ConfigModalState(editing=True, edit_buffer="foo")
    out = apply_key(view, "\x1b[A")
    assert out.edit_buffer == "foo"


# ---------------------------------------------------------------------------
# Toast is cleared on next keystroke
# ---------------------------------------------------------------------------


def test_apply_key_clears_toast() -> None:
    view = ConfigModalState(toast="saved 3 keys")
    out = apply_key(view, "j")
    assert out.toast == ""


# ---------------------------------------------------------------------------
# begin_edit / commit_edit
# ---------------------------------------------------------------------------


def test_begin_edit_seeds_buffer_from_merged_config() -> None:
    """begin_edit pulls the current value from the merged dict.

    Unlike :func:`apply_key`, which lacks merged-config access and
    falls back to the registry default, :func:`begin_edit` is called by
    the live loop after the merged dict is resolved.
    """
    view = ConfigModalState()  # tab=audit, field=0 → audit.fix_safe (bool)
    merged = {"audit": {"fix_safe": True}}
    out = begin_edit(view, merged)
    assert out.editing is True
    assert out.edit_buffer == "true"


def test_begin_edit_falls_back_to_default_when_merged_lacks_key() -> None:
    view = ConfigModalState()
    out = begin_edit(view, {})
    # audit.fix_safe default is False.
    assert out.edit_buffer == "false"


def test_commit_edit_folds_typed_value_into_dirty_map() -> None:
    """commit_edit coerces the buffer and stages it in ``dirty``."""
    view = ConfigModalState(editing=True, edit_buffer="true")
    out = commit_edit(view)
    assert out.editing is False
    assert out.edit_buffer == ""
    # audit.fix_safe is bool; "true" coerces to True.
    assert out.dirty == {"audit.fix_safe": True}


def test_commit_edit_int_coerces_to_int() -> None:
    """int field's buffer coerces to int, range-checked."""
    # Pick a bounded int entry. audit.flaky_retry_count: min=0, max=5, default=1.
    view = ConfigModalState(
        tab_index=0,
        field_index=1,  # audit.flaky_retry_count
        editing=True,
        edit_buffer="3",
    )
    out = commit_edit(view)
    assert out.dirty == {"audit.flaky_retry_count": 3}
    assert isinstance(out.dirty["audit.flaky_retry_count"], int)


def test_commit_edit_out_of_range_int_raises_invalid_input() -> None:
    view = ConfigModalState(
        tab_index=0,
        field_index=1,  # audit.flaky_retry_count (max=5)
        editing=True,
        edit_buffer="99",
    )
    with pytest.raises(InvalidInput, match="above maximum"):
        commit_edit(view)


def test_commit_edit_choice_value_must_be_in_choices() -> None:
    """A choice field rejects values outside the declared choices."""
    # ui.bare_command (choice) is at tab="ui". Find its index.
    ui_keys = [e for e in CONFIG_REGISTRY if e.tab == "ui"]
    ui_keys_sorted = sorted(ui_keys, key=lambda e: e.key)
    bare_idx = next(i for i, e in enumerate(ui_keys_sorted) if e.key == "ui.bare_command")
    from eawf.config.registry import tabs_sorted

    tab_idx = tabs_sorted().index("ui")
    view = ConfigModalState(
        tab_index=tab_idx,
        field_index=bare_idx,
        editing=True,
        edit_buffer="not-a-real-choice",
    )
    with pytest.raises(InvalidInput, match="not in choices"):
        commit_edit(view)


# ---------------------------------------------------------------------------
# save_dirty contract
# ---------------------------------------------------------------------------


def test_save_dirty_routes_through_w10_writer_seam(tmp_path: Path) -> None:
    """save_dirty calls save_fn once per dirty key with target_path/key/value."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    view = ConfigModalState(
        dirty={
            "audit.fix_safe": True,
            "audit.flaky_retry_count": 2,
        }
    )
    recorded: list[dict[str, Any]] = []

    def fake_save(*, target_path: Path, key: str, value: Any) -> None:
        recorded.append({"target_path": target_path, "key": key, "value": value})

    out = save_dirty(
        view,
        scope="repo",
        workspace=None,
        repo=repo,
        save_fn=fake_save,
    )
    assert out.dirty == {}
    assert out.toast == "saved 2 keys"
    assert len(recorded) == 2
    keys_saved = {r["key"] for r in recorded}
    assert keys_saved == {"audit.fix_safe", "audit.flaky_retry_count"}
    # Every call targets the same layered path.
    target = recorded[0]["target_path"]
    assert target == repo / ".ea" / "config.yaml"


def test_save_dirty_empty_dirty_emits_no_change_toast(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    view = ConfigModalState()  # no dirty
    out = save_dirty(view, scope="repo", workspace=None, repo=repo, save_fn=lambda **_: None)
    assert out.toast == "no changes to save"
    assert out.dirty == {}


def test_save_dirty_singular_toast_for_one_key(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    view = ConfigModalState(dirty={"audit.fix_safe": True})
    out = save_dirty(view, scope="repo", workspace=None, repo=repo, save_fn=lambda **_: None)
    # "saved 1 key" (no plural) — keeps the line tidy.
    assert out.toast == "saved 1 key"


def test_save_dirty_unknown_key_skipped(tmp_path: Path) -> None:
    """A key not in the registry is logged + skipped, not saved."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    view = ConfigModalState(dirty={"made.up.key": "value"})
    recorded: list[dict[str, Any]] = []

    def fake_save(*, target_path: Path, key: str, value: Any) -> None:
        recorded.append({"key": key})

    out = save_dirty(view, scope="repo", workspace=None, repo=repo, save_fn=fake_save)
    assert recorded == []
    # Toast still counts saved=0 keys (plural form for 0).
    assert out.toast == "saved 0 keys"


def test_save_dirty_invalid_scope_raises_value_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    view = ConfigModalState(dirty={"audit.fix_safe": True})
    with pytest.raises(ValueError, match="unknown writable layer"):
        save_dirty(view, scope="moonbase", workspace=None, repo=repo, save_fn=lambda **_: None)


# ---------------------------------------------------------------------------
# Panel builders (structural assertions)
# ---------------------------------------------------------------------------


def _render(renderable: Any) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=120, record=False).print(renderable)
    return buf.getvalue()


def test_build_header_panel_carries_brand_and_modal_marker() -> None:
    state = {"project": {"code": "EAWF"}, "current": {"phase_id": "P20", "iter_id": "P20-I01"}}
    panel = build_header_panel(state)
    rendered = _render(panel)
    assert "Eä" in rendered
    assert "EAWF" in rendered
    assert "P20" in rendered
    # Modal marker so the operator sees the surface change.
    assert "config" in rendered


def test_build_tabs_panel_lists_every_tab_with_active_bracketed() -> None:
    view = ConfigModalState()  # tab=audit
    panel = build_tabs_panel(view)
    rendered = _render(panel)
    assert "audit" in rendered
    # Active tab is bracketed.
    assert "[audit]" in rendered


def test_build_form_panel_renders_field_rows_with_cursor() -> None:
    view = ConfigModalState()  # tab=audit, field=0
    merged: dict[str, Any] = {"audit": {"fix_safe": False, "flaky_retry_count": 1}}
    panel = build_form_panel(view, merged)
    rendered = _render(panel)
    # First field of audit is audit.fix_safe — gets the cursor.
    assert "audit.fix_safe" in rendered
    assert "audit.flaky_retry_count" in rendered
    # The cursor marker is ">".
    cursor_lines = [line for line in rendered.splitlines() if "audit.fix_safe" in line]
    assert any(">" in line for line in cursor_lines)


def test_build_form_panel_marks_dirty_rows() -> None:
    view = ConfigModalState(dirty={"audit.fix_safe": True})
    merged: dict[str, Any] = {"audit": {"fix_safe": False, "flaky_retry_count": 1}}
    panel = build_form_panel(view, merged)
    rendered = _render(panel)
    # Dirty marker is "*" on the row prefix.
    dirty_lines = [line for line in rendered.splitlines() if "audit.fix_safe" in line]
    assert any("*" in line for line in dirty_lines)


def test_build_form_panel_dirty_value_overrides_merged_value() -> None:
    """The dirty buffer wins over the merged config for display."""
    view = ConfigModalState(dirty={"audit.flaky_retry_count": 4})
    merged: dict[str, Any] = {"audit": {"fix_safe": False, "flaky_retry_count": 1}}
    panel = build_form_panel(view, merged)
    rendered = _render(panel)
    # The dirty value (4) should appear on the flaky_retry_count row.
    flaky_lines = [line for line in rendered.splitlines() if "audit.flaky_retry_count" in line]
    assert any("4" in line for line in flaky_lines)


def test_build_input_panel_idle_prompts_enter_to_edit() -> None:
    view = ConfigModalState()
    panel = build_input_panel(view)
    rendered = _render(panel)
    assert "Enter to edit" in rendered


def test_build_input_panel_editing_shows_buffer_and_field_key() -> None:
    view = ConfigModalState(editing=True, edit_buffer="hello")
    panel = build_input_panel(view)
    rendered = _render(panel)
    assert "edit" in rendered
    assert "audit.fix_safe" in rendered
    assert "hello" in rendered


def test_build_footer_panel_idle_shows_modal_footer() -> None:
    view = ConfigModalState()
    panel = build_footer_panel(view)
    rendered = _render(panel)
    assert "s save" in rendered


def test_build_footer_panel_editing_shows_edit_keymap() -> None:
    view = ConfigModalState(editing=True)
    panel = build_footer_panel(view)
    rendered = _render(panel)
    assert "Enter commit" in rendered


def test_build_footer_panel_shows_toast_when_set() -> None:
    view = ConfigModalState(toast="saved 3 keys")
    panel = build_footer_panel(view)
    rendered = _render(panel)
    assert "saved 3 keys" in rendered


# ---------------------------------------------------------------------------
# Frame composition
# ---------------------------------------------------------------------------


def test_build_modal_frame_carries_brand_and_footer() -> None:
    state = {"project": {"code": "EAWF"}, "current": {"phase_id": "P20", "iter_id": "P20-I01"}}
    merged: dict[str, Any] = {"audit": {"fix_safe": False, "flaky_retry_count": 1}}
    view = ConfigModalState()
    rendered = _render(build_modal_frame(view, state=state, merged=merged))
    assert "Eä" in rendered
    assert "EAWF" in rendered
    assert "audit.fix_safe" in rendered
    assert "s save" in rendered


def test_render_modal_returns_string_without_external_console() -> None:
    state = {"project": {"code": "EAWF"}}
    merged: dict[str, Any] = {}
    out = render_modal(ConfigModalState(), state=state, merged=merged)
    assert "Eä" in out


def test_render_modal_writes_to_external_console_and_returns_empty() -> None:
    import io

    from rich.console import Console

    state = {"project": {"code": "EAWF"}}
    merged: dict[str, Any] = {}
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    out = render_modal(ConfigModalState(), state=state, merged=merged, console=console)
    assert out == ""
    assert "Eä" in buf.getvalue()


# ---------------------------------------------------------------------------
# Per-type editor seeding integration
# ---------------------------------------------------------------------------


def test_begin_edit_seeds_choice_value() -> None:
    """Choice fields seed the buffer with the current choice literal."""
    # ui.bare_command (choice, default tui).
    from eawf.config.registry import tabs_sorted

    ui_idx = tabs_sorted().index("ui")
    ui_keys = sorted(
        [e for e in CONFIG_REGISTRY if e.tab == "ui"],
        key=lambda e: e.key,
    )
    bare_field_idx = next(i for i, e in enumerate(ui_keys) if e.key == "ui.bare_command")
    view = ConfigModalState(tab_index=ui_idx, field_index=bare_field_idx)
    merged: dict[str, Any] = {"ui": {"bare_command": "status"}}
    out = begin_edit(view, merged)
    assert out.edit_buffer == "status"


def test_begin_edit_seeds_int_value() -> None:
    view = ConfigModalState(field_index=1)  # audit.flaky_retry_count
    merged: dict[str, Any] = {"audit": {"flaky_retry_count": 4}}
    out = begin_edit(view, merged)
    assert out.edit_buffer == "4"


def test_registry_lookup_resolves_dirty_keys() -> None:
    """Every key the modal touches must be registry-lookup-able.

    Sanity check that the registry and the modal agree on the set of
    keys — protects against future drift where a key gets renamed in
    the registry but the modal still references it.
    """
    view = ConfigModalState(dirty={"audit.fix_safe": True})
    for key in view.dirty:
        assert registry_lookup(key) is not None, f"missing registry entry for {key!r}"
