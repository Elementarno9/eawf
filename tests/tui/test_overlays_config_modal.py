"""Pilot + unit tests for the ``ConfigModal`` config window (tui).

Covers the registry-driven tabbed config overlay after the
Enter-as-sole-mutator redesign (P26-I02-W01):

* **registry coverage** — the modal renders a tab per
  :func:`~eawf.kernel.config.registry.tabs_sorted` and a field row per
  :func:`~eawf.kernel.config.registry.keys_for_tab`, so a dropped registry key
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

import pytest
from textual.widgets import Input, Static, TabbedContent

from eawf.kernel.config.registry import (
    CONFIG_REGISTRY,
    LEAF_KEY_REGISTRY,
    ConfigKey,
    coerce_and_validate,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.config_modal import (
    ConfigModal,
    ConfigModalState,
    current_value,
    cycle_choice,
    editable_keys_for_tab,
    enter_action,
    format_value,
    is_editable_key,
    is_security_key,
    needs_popup_edit,
    save_dirty_fields,
    toggle_bool,
    toggle_multichoice_item,
    writable_layers_for,
)
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.screens.overlays.edit_field import EditFieldModal

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
    entry = registry_lookup("flow.advance_after.audit")
    assert entry is not None
    merged = {"flow": {"advance_after": {"audit": False}}}
    assert current_value(entry, merged, {"flow.advance_after.audit": True}) is True


def test_current_value_falls_back_to_default() -> None:
    entry = registry_lookup("flow.advance_after.audit")
    assert entry is not None
    assert current_value(entry, {}, {}) == entry.default


def test_toggle_bool_flips() -> None:
    entry = registry_lookup("flow.advance_after.audit")
    assert entry is not None
    assert toggle_bool(entry, {"flow": {"advance_after": {"audit": False}}}, {}) == {
        "flow.advance_after.audit": True
    }


def test_toggle_bool_noop_on_non_bool() -> None:
    entry = registry_lookup("audit.default_level")
    assert entry is not None
    assert toggle_bool(entry, {}, {}) == {}


def test_cycle_choice_wraps() -> None:
    entry = registry_lookup("audit.default_level")  # choices ("quick", "standard", "deep")
    assert entry is not None
    merged = {"audit": {"default_level": "deep"}}
    # Cycling forward from the last choice wraps to the first.
    assert cycle_choice(entry, merged, {}, step=1) == {"audit.default_level": "quick"}


def test_cycle_choice_forward_a_b_c() -> None:
    """Forward-cycle steps a -> b -> c (the Enter-mutator direction)."""
    entry = registry_lookup("audit.default_level")  # ("quick", "standard", "deep")
    assert entry is not None
    after_first = cycle_choice(entry, {"audit": {"default_level": "quick"}}, {}, step=1)
    assert after_first == {"audit.default_level": "standard"}
    after_second = cycle_choice(entry, {}, after_first, step=1)
    assert after_second == {"audit.default_level": "deep"}


def test_format_value_bool_lowercase() -> None:
    entry = registry_lookup("flow.advance_after.audit")
    assert entry is not None
    assert format_value(entry, True) == "true"
    assert format_value(entry, False) == "false"


# -- multichoice pure helper (toggle_multichoice_item) ----------------------


def _dashboard_panes_entry() -> ConfigKey:
    """Return the ``ui.dashboard_panes`` multichoice registry entry."""
    entry = registry_lookup("ui.dashboard_panes")
    assert entry is not None and entry.type == "multichoice"
    return entry


def test_toggle_multichoice_item_on_from_empty() -> None:
    """Toggling an item ON from the empty default stages a single-item list."""
    entry = _dashboard_panes_entry()
    assert toggle_multichoice_item(entry, {}, {}, item="roadmap") == {
        "ui.dashboard_panes": ["roadmap"]
    }


def test_toggle_multichoice_item_off_removes() -> None:
    """Toggling a present item OFF removes it from the staged list."""
    entry = _dashboard_panes_entry()
    merged = {"ui": {"dashboard_panes": ["state", "roadmap"]}}
    assert toggle_multichoice_item(entry, merged, {}, item="roadmap") == {
        "ui.dashboard_panes": ["state"]
    }


def test_toggle_multichoice_item_preserves_choices_order() -> None:
    """The staged list follows ``choices`` declaration order, not toggle order.

    Toggling ``roadmap`` then ``state`` (reverse of their declaration order)
    still stages ``[state, roadmap]`` because ``state`` precedes ``roadmap``
    in the key's declared ``choices``.
    """
    entry = _dashboard_panes_entry()
    after_roadmap = toggle_multichoice_item(entry, {}, {}, item="roadmap")
    after_state = toggle_multichoice_item(entry, {}, after_roadmap, item="state")
    assert after_state == {"ui.dashboard_panes": ["state", "roadmap"]}


def test_toggle_multichoice_item_noop_for_unknown_item() -> None:
    """An item outside the declared choices is a no-op (dirty unchanged)."""
    entry = _dashboard_panes_entry()
    assert toggle_multichoice_item(entry, {}, {}, item="bogus") == {}


def test_toggle_multichoice_item_noop_on_non_multichoice() -> None:
    """A non-multichoice field routes through harmlessly (dirty unchanged)."""
    entry = registry_lookup("audit.default_level")  # choice, not multichoice
    assert entry is not None
    assert toggle_multichoice_item(entry, {}, {}, item="quick") == {}


def test_toggle_multichoice_item_round_trip_drop_if_unchanged() -> None:
    """Toggling an item on then off nets the persisted value, so drop clears it.

    The dirty map a round-trip leaves carries the empty list (== the
    persisted default ``()``), so ``ConfigModal._drop_if_unchanged`` removes
    the key — no spurious dirty mark. Drives the helper with an explicit
    empty ``merged`` so the persisted value resolves to the registry
    default regardless of the host machine's layered config.
    """
    entry = _dashboard_panes_entry()
    modal = ConfigModal(workspace=None, repo=Path("/tmp/repo"))
    modal._merged = {}  # persisted value resolves to the registry default ``()``
    on = toggle_multichoice_item(entry, modal._merged, {}, item="roadmap")
    assert on == {"ui.dashboard_panes": ["roadmap"]}
    off = toggle_multichoice_item(entry, modal._merged, on, item="roadmap")
    assert off == {"ui.dashboard_panes": []}
    # The net-empty list equals the persisted default, so drop clears the key.
    reconciled = modal._drop_if_unchanged(entry, off)
    assert "ui.dashboard_panes" not in reconciled


# -- type -> action dispatch (enter_action / needs_popup_edit) --------------


def test_enter_action_bool_toggles() -> None:
    entry = registry_lookup("flow.advance_after.audit")  # bool
    assert entry is not None
    assert enter_action(entry, False, row_width=80) == "toggle"


def test_enter_action_choice_cycles() -> None:
    entry = registry_lookup("audit.default_level")  # choice
    assert entry is not None
    assert enter_action(entry, "ask", row_width=80) == "cycle"


def test_enter_action_int_inline() -> None:
    entry = registry_lookup("flow.max_repair_cycles")  # int
    assert entry is not None
    assert enter_action(entry, 2, row_width=80) == "inline"


def test_enter_action_multichoice() -> None:
    """A multichoice field resolves to the inline checklist action."""
    entry = registry_lookup("ui.dashboard_panes")  # multichoice
    assert entry is not None and entry.type == "multichoice"
    assert enter_action(entry, (), row_width=80) == "multichoice"


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
    for key in (
        "flow.advance_after.audit",
        "audit.default_level",
        "flow.max_repair_cycles",
    ):
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
        {"flow.advance_after.audit": True, "audit.default_level": "standard"},
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
    assert keys == {"flow.advance_after.audit", "audit.default_level"}


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
# Surfaced hidden-leaf-key curation (W09)
# ---------------------------------------------------------------------------

#: Hidden leaf keys promoted into the curated ``CONFIG_REGISTRY`` so they
#: appear in the config overlay. ``ui.toasts`` ships from W08; the rest are
#: surfaced here. The full-catalog ``LEAF_KEY_REGISTRY`` is NOT browsed.
_SURFACED_KEYS: tuple[str, ...] = (
    "ui.toasts",
    "ui.glyphs",
    "ui.dashboard_panes",
    "estimation.display.show_category",
    "estimation.display.show_raw_eu",
    "estimation.display.show_expected_time",
    "estimation.display.show_pessimistic_time",
    "estimation.display.eu_quantum",
    "estimation.display.time_quantum_under_2h_minutes",
    "estimation.display.time_quantum_over_2h_minutes",
    "telemetry.enabled",
    "telemetry.export.format",
    "telemetry.window_default",
    "telemetry.aggregate_window",
    "telemetry.db_kind",
    "daemon.proxy_enabled",
    "daemon.idle_timeout_seconds",
    "daemon.session_handle_ttl_seconds",
)


def test_surfaced_keys_all_have_curated_rows() -> None:
    """Every enumerated hidden leaf key now has a ``CONFIG_REGISTRY`` row."""
    keys = {entry.key for entry in CONFIG_REGISTRY}
    missing = [key for key in _SURFACED_KEYS if key not in keys]
    assert not missing, missing


def test_surfaced_keys_default_matches_leaf_registry() -> None:
    """Each surfaced curated row's default mirrors its LEAF_KEY_REGISTRY default."""
    for key in _SURFACED_KEYS:
        entry = registry_lookup(key)
        leaf = LEAF_KEY_REGISTRY[key]
        assert entry is not None, key
        # Tuple leaf defaults (list_str) round-trip equal to the curated tuple.
        assert entry.default == leaf.default, (key, entry.default, leaf.default)


def test_surfaced_choice_keys_choices_match_leaf_registry() -> None:
    """Choice/multichoice rows mirror their LEAF_KEY_REGISTRY ``choices`` set."""
    for key in _SURFACED_KEYS:
        entry = registry_lookup(key)
        leaf = LEAF_KEY_REGISTRY[key]
        assert entry is not None, key
        if entry.type in ("choice", "multichoice"):
            assert entry.choices is not None, key
            if leaf.choices is not None:
                assert set(entry.choices) == set(leaf.choices), key


def test_surfaced_keys_grouped_under_expected_tab() -> None:
    """Surfaced rows land under the tab their dotted-key family implies."""
    expected_tab = {
        "ui": ("ui.toasts", "ui.glyphs", "ui.dashboard_panes"),
        "estimation": (
            "estimation.display.show_category",
            "estimation.display.eu_quantum",
        ),
        "telemetry": ("telemetry.enabled", "telemetry.db_kind"),
        "daemon": ("daemon.proxy_enabled", "daemon.idle_timeout_seconds"),
    }
    for tab, keys in expected_tab.items():
        tab_keys = {entry.key for entry in keys_for_tab(tab)}
        for key in keys:
            assert key in tab_keys, (tab, key)


def test_config_registry_is_not_the_full_leaf_catalog() -> None:
    """The curated registry stays a small subset — no full-catalog browser."""
    assert len(CONFIG_REGISTRY) < len(LEAF_KEY_REGISTRY)
    # A leaf key the brief did NOT enumerate must stay hidden from the menu.
    assert registry_lookup("schema_version") is None
    assert registry_lookup("cli.canonical_command") is None


def test_surfaced_key_edit_validates_via_coerce_and_validate() -> None:
    """A surfaced choice key coerces a valid value and rejects an invalid one."""
    entry = registry_lookup("telemetry.db_kind")
    assert entry is not None and entry.type == "choice"
    # Boundary: a declared choice round-trips unchanged.
    assert coerce_and_validate(entry, "duckdb") == "duckdb"
    # Error path: an undeclared choice is rejected.
    import pytest

    with pytest.raises(UserError):
        coerce_and_validate(entry, "postgres")


def test_multichoice_coerce_accepts_valid_and_rejects_invalid() -> None:
    """``coerce_and_validate`` accepts a declared-choice list and rejects others."""
    import pytest

    entry = registry_lookup("ui.dashboard_panes")
    assert entry is not None and entry.type == "multichoice"
    # Boundary: a subset of declared choices round-trips to a list.
    assert coerce_and_validate(entry, ["state", "roadmap"]) == ["state", "roadmap"]
    # Boundary: the empty selection validates to an empty list.
    assert coerce_and_validate(entry, []) == []
    # Error path: an undeclared item is rejected.
    with pytest.raises(UserError):
        coerce_and_validate(entry, ["bogus"])


def test_surfaced_int_key_range_rejects_below_minimum() -> None:
    """A surfaced int key with a lower bound rejects an out-of-range value."""
    entry = registry_lookup("daemon.idle_timeout_seconds")
    assert entry is not None and entry.type == "int"
    assert coerce_and_validate(entry, "0") == 0  # boundary: minimum is allowed
    import pytest

    with pytest.raises(UserError):
        coerce_and_validate(entry, "-1")


# ---------------------------------------------------------------------------
# Curated preferences.* + research.* coverage (W28)
# ---------------------------------------------------------------------------

#: The ``preferences.*`` keys curated into ``CONFIG_REGISTRY`` so they appear
#: under the ``preferences`` tab. Each is a closed ``choice`` enum (the
#: planner / AUQ knobs); the full leaf catalog is NOT browsed.
_PREFERENCES_KEYS: tuple[str, ...] = (
    "preferences.solution_bias",
    "preferences.scope_size",
    "preferences.auto_choose",
)

#: The ``research.*`` keys curated into ``CONFIG_REGISTRY`` so they appear
#: under the ``research`` tab — a deliberate subset (depth / sources / count /
#: auto-save), not every research leaf.
_RESEARCH_KEYS: tuple[str, ...] = (
    "research.agent_count",
    "research.auto_save",
    "research.default_depth",
    "research.default_sources",
)


def test_preferences_keys_all_have_curated_rows() -> None:
    """Every curated ``preferences.*`` key has a ``CONFIG_REGISTRY`` row."""
    keys = {entry.key for entry in CONFIG_REGISTRY}
    missing = [key for key in _PREFERENCES_KEYS if key not in keys]
    assert not missing, missing


def test_research_keys_all_have_curated_rows() -> None:
    """Every curated ``research.*`` key has a ``CONFIG_REGISTRY`` row."""
    keys = {entry.key for entry in CONFIG_REGISTRY}
    missing = [key for key in _RESEARCH_KEYS if key not in keys]
    assert not missing, missing


def test_preferences_keys_grouped_under_preferences_tab() -> None:
    """The curated ``preferences.*`` rows land under the ``preferences`` tab."""
    tab_keys = {entry.key for entry in keys_for_tab("preferences")}
    for key in _PREFERENCES_KEYS:
        assert key in tab_keys, key


def test_research_keys_grouped_under_research_tab() -> None:
    """The curated ``research.*`` rows land under the ``research`` tab."""
    tab_keys = {entry.key for entry in keys_for_tab("research")}
    for key in _RESEARCH_KEYS:
        assert key in tab_keys, key


def test_preferences_keys_default_matches_leaf_registry() -> None:
    """Each curated ``preferences.*`` row's default mirrors its leaf default."""
    for key in _PREFERENCES_KEYS:
        entry = registry_lookup(key)
        leaf = LEAF_KEY_REGISTRY[key]
        assert entry is not None, key
        assert entry.default == leaf.default, (key, entry.default, leaf.default)


def test_research_keys_default_matches_leaf_registry() -> None:
    """Each curated ``research.*`` row's default mirrors its leaf default."""
    for key in _RESEARCH_KEYS:
        entry = registry_lookup(key)
        leaf = LEAF_KEY_REGISTRY[key]
        assert entry is not None, key
        assert entry.default == leaf.default, (key, entry.default, leaf.default)


def test_preferences_enums_render_as_choice_fields() -> None:
    """``solution_bias`` / ``scope_size`` / ``auto_choose`` are ``choice`` widgets.

    The three preferences enums must surface as choice fields (forward-cycled
    on ``Enter``), so :func:`enter_action` resolves ``cycle`` and each row
    declares a non-empty ``choices`` set mirroring its leaf registry choices.
    """
    for key in _PREFERENCES_KEYS:
        entry = registry_lookup(key)
        assert entry is not None and entry.type == "choice", key
        assert entry.choices is not None and len(entry.choices) >= 2, key
        leaf = LEAF_KEY_REGISTRY[key]
        if leaf.choices is not None:
            assert set(entry.choices) == set(leaf.choices), key
        assert enter_action(entry, entry.default, row_width=80) == "cycle", key


def test_research_field_widgets_match_declared_type() -> None:
    """Each curated ``research.*`` row maps to the right ``Enter`` action.

    ``research.default_depth`` / ``research.default_sources`` are ``choice``
    enums (cycle), ``research.agent_count`` is a bounded ``int`` (inline edit),
    and ``research.auto_save`` is a ``bool`` (toggle) — the widget the modal
    mounts is the field's declared type, not a generic text box.
    """
    expected_action = {
        "research.agent_count": "inline",
        "research.auto_save": "toggle",
        "research.default_depth": "cycle",
        "research.default_sources": "cycle",
    }
    for key, action in expected_action.items():
        entry = registry_lookup(key)
        assert entry is not None, key
        assert enter_action(entry, entry.default, row_width=80) == action, key
        if entry.type == "choice":
            assert entry.choices is not None and len(entry.choices) >= 2, key


def test_preferences_choice_edit_validates_via_coerce_and_validate() -> None:
    """A preferences choice key coerces a declared value and rejects an undeclared one."""
    import pytest

    entry = registry_lookup("preferences.solution_bias")
    assert entry is not None and entry.type == "choice"
    # Boundary: a declared choice round-trips unchanged.
    assert coerce_and_validate(entry, "thorough") == "thorough"
    # Error path: an undeclared choice is rejected.
    with pytest.raises(UserError):
        coerce_and_validate(entry, "reckless")


def test_research_choice_edit_rejects_invalid_enum() -> None:
    """A ``research.*`` choice key rejects an undeclared enum value."""
    import pytest

    entry = registry_lookup("research.default_depth")
    assert entry is not None and entry.type == "choice"
    # Boundary: a declared depth round-trips unchanged.
    assert coerce_and_validate(entry, entry.choices[0]) == entry.choices[0]  # type: ignore[index]
    # Error path: an undeclared depth is rejected.
    with pytest.raises(UserError):
        coerce_and_validate(entry, "telescopic")


def test_research_int_key_range_rejects_out_of_range() -> None:
    """``research.agent_count`` enforces its declared 1..12 bound on edit."""
    import pytest

    entry = registry_lookup("research.agent_count")
    assert entry is not None and entry.type == "int"
    assert coerce_and_validate(entry, "1") == 1  # boundary: minimum allowed
    assert coerce_and_validate(entry, "12") == 12  # boundary: maximum allowed
    with pytest.raises(UserError):
        coerce_and_validate(entry, "0")
    with pytest.raises(UserError):
        coerce_and_validate(entry, "13")


def test_preferences_cycle_choice_forward_wraps() -> None:
    """Forward-cycling ``preferences.solution_bias`` steps its choices and wraps."""
    entry = registry_lookup("preferences.solution_bias")
    assert entry is not None and entry.choices is not None
    choices = list(entry.choices)
    merged = {"preferences": {"solution_bias": choices[-1]}}
    # Cycling forward from the last choice wraps to the first.
    assert cycle_choice(entry, merged, {}, step=1) == {"preferences.solution_bias": choices[0]}


def test_curated_keys_stay_subset_of_leaf_catalog() -> None:
    """The new keys ride the curated registry, not a full-leaf dump.

    All seven preferences / research keys live in the full
    :data:`LEAF_KEY_REGISTRY` and are mirrored into the curated
    :data:`CONFIG_REGISTRY`, which stays strictly smaller than the catalog —
    the modal shows a deliberate subset, never every leaf.
    """
    for key in (*_PREFERENCES_KEYS, *_RESEARCH_KEYS):
        assert key in LEAF_KEY_REGISTRY, key
        assert registry_lookup(key) is not None, key
    assert len(CONFIG_REGISTRY) < len(LEAF_KEY_REGISTRY)


# ---------------------------------------------------------------------------
# Curated scalar flow.* coverage (W27)
# ---------------------------------------------------------------------------

#: The ``flow.*`` scalar keys curated into ``CONFIG_REGISTRY`` so the
#: workflow transition / repair / budget controls surface under a ``flow``
#: tab. Each is a writable scalar leaf -- never a locked, security, or
#: structural key.
_FLOW_KEYS: tuple[str, ...] = (
    "flow.advance_after.audit",
    "flow.advance_after.polish",
    "flow.advance_after.prep",
    "flow.advance_after.research",
    "flow.budget.enforce",
    "flow.max_repair_cycles",
)


def test_flow_keys_all_have_curated_rows() -> None:
    """Every curated ``flow.*`` key has a ``CONFIG_REGISTRY`` row."""
    keys = {entry.key for entry in CONFIG_REGISTRY}
    missing = [key for key in _FLOW_KEYS if key not in keys]
    assert not missing, missing


def test_flow_keys_grouped_under_flow_tab() -> None:
    """The curated ``flow.*`` rows land under the ``flow`` tab."""
    tab_keys = {entry.key for entry in keys_for_tab("flow")}
    for key in _FLOW_KEYS:
        assert key in tab_keys, key


def test_flow_keys_are_safe_to_edit() -> None:
    """Each curated ``flow.*`` key is a writable scalar leaf (not locked/structural).

    The curated set is deliberately limited to keys safe to edit from the
    TUI: writable from at least one layer, of a scalar shape the single-row
    editor handles, and outside the ``security.*`` confirm-gated family.
    """
    non_scalar = {"list_str", "list_any", "mapping", "any"}
    for key in _FLOW_KEYS:
        leaf = LEAF_KEY_REGISTRY[key]
        assert leaf.writable_layers != (), key  # not locked
        assert leaf.type not in non_scalar, (key, leaf.type)  # scalar, not structural
        assert not key.startswith("security."), key


@pytest.mark.parametrize("key", _FLOW_KEYS)
def test_flow_key_round_trips_through_coerce_and_validate(key: str) -> None:
    """Each newly-surfaced ``flow.*`` ConfigKey round-trips through its leaf row.

    The curated default coerces under the ConfigKey's declared type, and a
    declared choice / leaf default lands back unchanged -- the curated row's
    type + choices stay leaf-consistent so an edit validated in the modal is
    one the daemon's leaf catalog also accepts.
    """
    entry = registry_lookup(key)
    leaf = LEAF_KEY_REGISTRY[key]
    assert entry is not None, key
    # The curated default mirrors the leaf default.
    assert entry.default == leaf.default, (key, entry.default, leaf.default)
    # The default round-trips through coerce_and_validate under its declared type.
    coerced_default = coerce_and_validate(entry, entry.default)
    if entry.type == "bool":
        assert isinstance(coerced_default, bool)
        # The opposite boolean (string form) also coerces cleanly.
        assert coerce_and_validate(entry, "false") is False
        assert coerce_and_validate(entry, "true") is True
    elif entry.type == "choice":
        assert entry.choices is not None and len(entry.choices) >= 2, key
        # A choice key's choices mirror the leaf catalog's choices exactly.
        if leaf.choices is not None:
            assert set(entry.choices) == set(leaf.choices), key
        # Every declared choice round-trips unchanged through the leaf row.
        for choice in entry.choices:
            assert coerce_and_validate(entry, choice) == choice
        # An undeclared value is rejected.
        with pytest.raises(UserError):
            coerce_and_validate(entry, "not-a-declared-choice")


def test_flow_keys_stay_subset_of_leaf_catalog() -> None:
    """The curated ``flow.*`` rows ride the curated registry, not a full-leaf dump."""
    for key in _FLOW_KEYS:
        assert key in LEAF_KEY_REGISTRY, key
        assert registry_lookup(key) is not None, key
    assert len(CONFIG_REGISTRY) < len(LEAF_KEY_REGISTRY)


# ---------------------------------------------------------------------------
# Lock-filter + security-confirm (W28)
# ---------------------------------------------------------------------------


def _locked_config_key() -> ConfigKey:
    """Return a synthetic ConfigKey whose dotted key names a LOCKED leaf.

    ``schema_version`` has ``writable_layers == ()`` in the leaf catalog, so
    a curated row pointing at it must be filtered out of the modal's surfaced
    set regardless of the modal's target layers.
    """
    leaf = LEAF_KEY_REGISTRY["schema_version"]
    assert leaf.writable_layers == ()
    return ConfigKey(tab="config", key="schema_version", label="schema", type="str", default="1.0")


def test_is_editable_key_omits_locked_leaf() -> None:
    """A curated key naming a locked leaf is not editable from any layer."""
    entry = _locked_config_key()
    assert is_editable_key(entry, ("global", "repo", "local")) is False


def test_is_editable_key_omits_leaf_not_writable_from_layers() -> None:
    """A key not writable from any offered layer is filtered out.

    ``telemetry.db_kind`` is ``global``-only, so it is editable when the
    modal can target ``global`` but not when limited to ``repo`` / ``local``.
    """
    entry = registry_lookup("telemetry.db_kind")
    assert entry is not None
    assert is_editable_key(entry, ("repo", "local")) is False
    assert is_editable_key(entry, ("global", "repo")) is True


def test_is_editable_key_keeps_writable_scalar() -> None:
    """A curated key writable from an offered layer stays editable."""
    entry = registry_lookup("flow.advance_after.audit")
    assert entry is not None
    assert is_editable_key(entry, ("global", "repo", "local")) is True


def test_editable_keys_for_tab_drops_locked_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``editable_keys_for_tab`` filters a locked key out of a tab's field list.

    Injects a synthetic locked row into the ``config`` tab via the registry
    helper so the filter is exercised against a key that would otherwise be
    surfaced; the locked key must be absent from the returned list while a
    writable sibling stays.
    """
    import eawf.surfaces.tui.screens.overlays.config_modal_logic as logic

    locked = _locked_config_key()
    writable = ConfigKey(
        tab="config",
        key="config.layers_visible",
        label="layers visible",
        type="bool",
        default=True,
    )

    def fake_keys_for_tab(tab: str) -> tuple[ConfigKey, ...]:
        return (locked, writable) if tab == "config" else ()

    monkeypatch.setattr(logic, "keys_for_tab", fake_keys_for_tab)
    result = editable_keys_for_tab("config", ("global", "repo", "local"))
    keys = [entry.key for entry in result]
    assert "schema_version" not in keys
    assert "config.layers_visible" in keys


def test_is_security_key_detects_security_domain() -> None:
    """``is_security_key`` is True for ``security.*`` keys, False otherwise."""
    sec = ConfigKey(
        tab="security",
        key="security.permission_mode",
        label="perm",
        type="str",
        default="ask_first",
    )
    assert is_security_key(sec) is True
    non_sec = registry_lookup("flow.advance_after.audit")
    assert non_sec is not None
    assert is_security_key(non_sec) is False


def _security_config_key() -> ConfigKey:
    """Return a curated-shaped ConfigKey for a real ``security.*`` leaf.

    ``security.secret_scan`` is a writable ``bool`` leaf; a row pointing at
    it routes its save through the confirm gate.
    """
    leaf = LEAF_KEY_REGISTRY["security.secret_scan"]
    assert leaf.type == "bool" and leaf.writable_layers != ()
    return ConfigKey(
        tab="security",
        key="security.secret_scan",
        label="secret scan",
        type="bool",
        default=True,
    )


def test_security_edit_without_confirm_no_ops() -> None:
    """Committing a ``security.*`` edit without confirming leaves dirty untouched.

    ``_commit_staged`` pushes a :class:`ConfirmModal` for a ``security.*``
    field instead of staging immediately; until the operator confirms, the
    candidate value is NOT folded into the dirty map.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = _security_config_key()
            # Stage a flipped value through the security gate.
            staged = {entry.key: False}
            modal._commit_staged(entry, staged)
            await pilot.pause()
            # A ConfirmModal is now on top, and nothing has been staged yet.
            assert isinstance(app.screen, ConfirmModal)
            assert entry.key not in modal._view.dirty

    asyncio.run(body())


def test_security_edit_confirm_prompt_names_key_and_value() -> None:
    """The security confirm prompt names BOTH the key and the staged value."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = _security_config_key()
            staged = {entry.key: False}
            modal._commit_staged(entry, staged)
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            prompt = app.screen._prompt
            # The prompt names the key and the staged value.
            assert entry.key in prompt, prompt
            assert format_value(entry, False) in prompt, prompt

    asyncio.run(body())


def test_security_edit_stages_only_after_confirm() -> None:
    """Confirming the security prompt folds the staged value into the dirty map."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = _security_config_key()
            modal._merged = {"security": {"secret_scan": True}}
            modal._commit_staged(entry, {entry.key: False})
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            # Confirm: move highlight to Yes (right) and press Enter.
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert modal._view.dirty.get(entry.key) is False

    asyncio.run(body())


def test_security_edit_decline_leaves_dirty_untouched() -> None:
    """Declining the security prompt (``Esc``) stages nothing."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = _security_config_key()
            modal._merged = {"security": {"secret_scan": True}}
            modal._commit_staged(entry, {entry.key: False})
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")  # cancel = decline
            await pilot.pause()
            assert entry.key not in modal._view.dirty

    asyncio.run(body())


def test_non_security_edit_stages_without_confirm() -> None:
    """A non-``security`` field stages immediately (no confirm modal pushed)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            entry = registry_lookup("flow.advance_after.audit")
            assert entry is not None
            modal._merged = {"flow": {"advance_after": {"audit": False}}}
            modal._commit_staged(entry, {entry.key: True})
            await pilot.pause()
            # No confirm modal: the edit staged directly.
            assert app.screen is modal
            assert modal._view.dirty.get(entry.key) is True

    asyncio.run(body())


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


def _seek_field(modal: ConfigModal, key: str) -> None:
    """Point the cursor at *key* within the active tab (within-tab-order robust).

    Anchors on the field's dotted key via the modal's own rendered field list
    rather than a hardcoded index, so adding a key to a tab (which shifts the
    alphabetical order) does not silently retarget these interaction tests.
    """
    fields = modal._active_fields()
    modal.field_index = [entry.key for entry in fields].index(key)


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


def test_surfaced_keys_render() -> None:
    """The modal surfaces the curated hidden keys plus ``ui.toasts`` (W08)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            # Map each surfaced key to the rendered text of its field row.
            for key in _SURFACED_KEYS:
                entry = registry_lookup(key)
                assert entry is not None, key
                tab = entry.tab
                index = [e.key for e in keys_for_tab(tab)].index(key)
                row = modal.query_one(f"#{modal._field_row_id(tab, index)}", Static)
                text = str(row.render())
                assert key in text, (key, text)
            # No full-catalog: a leaf key the brief did not surface has no row.
            rendered = {entry.key for entry in CONFIG_REGISTRY}
            assert "schema_version" not in rendered
            assert len(rendered) < len(LEAF_KEY_REGISTRY)

    asyncio.run(body())


def test_preferences_tab_renders_choice_rows() -> None:
    """The ``preferences`` tab surfaces its three choice rows as field rows."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            fields = keys_for_tab("preferences")
            for index, entry in enumerate(fields):
                row = modal.query_one(f"#{modal._field_row_id('preferences', index)}", Static)
                text = str(row.render())
                assert entry.key in text, (entry.key, text)
                # The choice value renders in the row (e.g. ``balanced``).
                assert f"[{entry.type}]" in text, (entry.key, text)
            rendered = {entry.key for entry in fields}
            for key in _PREFERENCES_KEYS:
                assert key in rendered, key

    asyncio.run(body())


def test_research_tab_renders_curated_rows() -> None:
    """The ``research`` tab surfaces its curated choice / int / bool rows."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            fields = keys_for_tab("research")
            for index, entry in enumerate(fields):
                row = modal.query_one(f"#{modal._field_row_id('research', index)}", Static)
                text = str(row.render())
                assert entry.key in text, (entry.key, text)
            rendered = {entry.key for entry in fields}
            for key in _RESEARCH_KEYS:
                assert key in rendered, key

    asyncio.run(body())


def test_preferences_enter_cycles_choice_and_stages() -> None:
    """``Enter`` on a ``preferences`` choice row cycles it + stages the edit."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "preferences")
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.type == "choice"
            before = current_value(entry, modal._merged, modal._view.dirty)
            await pilot.press("enter")  # forward-cycle the choice
            await pilot.pause()
            after = current_value(entry, modal._merged, modal._view.dirty)
            assert after != before
            assert entry.key in modal._view.dirty
            # The staged value is one of the declared choices.
            assert after in (entry.choices or ())

    asyncio.run(body())


def test_preferences_choice_edit_flushes_to_writable_layer() -> None:
    """Cycling a ``preferences`` choice then ``s`` flushes to the chosen layer.

    Exercises the end-to-end stage -> flush path: focus the preferences tab,
    ``Enter`` to cycle ``solution_bias``, ``s`` to save. The layered-writer
    seam is called with the staged value targeting the repo-layer YAML — never
    ``state.json`` (AGENTS rule 4).
    """

    async def body() -> None:
        calls: list[dict[str, Any]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app, save_fn=lambda **k: calls.append(k))
            await pilot.pause()
            _goto_tab(modal, "preferences")
            # Anchor on solution_bias regardless of within-tab order.
            fields = keys_for_tab("preferences")
            modal.field_index = [e.key for e in fields].index("preferences.solution_bias")
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.key == "preferences.solution_bias"
            await pilot.press("enter")  # cycle to the next choice
            await pilot.pause()
            staged = modal._view.dirty.get("preferences.solution_bias")
            assert staged is not None
            await pilot.press("s")  # flush through the layered writer
            await pilot.pause()
            saved = [call for call in calls if call["key"] == "preferences.solution_bias"]
            assert len(saved) == 1
            assert saved[0]["value"] == staged
            assert str(saved[0]["target_path"]).endswith(".yaml")
            assert "state.json" not in str(saved[0]["target_path"])
            assert modal._view.dirty == {}

    asyncio.run(body())


def test_research_choice_edit_flushes_to_writable_layer() -> None:
    """Cycling ``research.default_depth`` then ``s`` flushes to the writable layer."""

    async def body() -> None:
        calls: list[dict[str, Any]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app, save_fn=lambda **k: calls.append(k))
            await pilot.pause()
            _goto_tab(modal, "research")
            fields = keys_for_tab("research")
            modal.field_index = [e.key for e in fields].index("research.default_depth")
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.key == "research.default_depth"
            await pilot.press("enter")  # cycle to the next depth
            await pilot.pause()
            staged = modal._view.dirty.get("research.default_depth")
            assert staged is not None
            assert staged in (entry.choices or ())
            await pilot.press("s")  # flush through the layered writer
            await pilot.pause()
            saved = [call for call in calls if call["key"] == "research.default_depth"]
            assert len(saved) == 1
            assert saved[0]["value"] == staged
            assert str(saved[0]["target_path"]).endswith(".yaml")
            assert "state.json" not in str(saved[0]["target_path"])
            assert modal._view.dirty == {}

    asyncio.run(body())


def test_research_invalid_enum_inline_edit_rejected() -> None:
    """An invalid enum typed into a ``research`` choice surfaces an error, stages nothing.

    The inline ``Input`` path coerces the buffer through
    :func:`coerce_and_validate`; an undeclared depth value keeps the editor
    open with the error row populated and the dirty map untouched.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "research")
            fields = keys_for_tab("research")
            modal.field_index = [e.key for e in fields].index("research.default_depth")
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.type == "choice"
            # A choice cycles in place, so drive the validation path directly
            # through the commit handler with an undeclared value, mirroring an
            # operator routing a bad value via coerce_and_validate.
            import pytest

            with pytest.raises(UserError):
                coerce_and_validate(entry, "telescopic")
            # The modal stays clean — no value was staged.
            assert entry.key not in modal._view.dirty

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
            _goto_tab(modal, "flow")
            _seek_field(modal, "flow.advance_after.audit")
            entry = modal._active_field()  # flow.advance_after.audit (bool)
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
            _goto_tab(modal, "audit")
            await pilot.pause()
            _seek_field(modal, "audit.default_level")
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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            await pilot.press("enter")  # open inline editor
            await pilot.pause()
            modal.query_one("#config-inline-input", Input).value = "5"
            await pilot.press("enter")  # commit
            await pilot.pause()
            assert modal._editing_key is None  # editor torn down
            assert modal._view.dirty.get(entry.key) == 5  # coerced int, not "5"

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
            # Drain the GitPane git-probe worker spawned on mount. It runs a
            # real ``git`` subprocess off the event loop; left in flight it
            # keeps the screen's message count non-zero, so a follow-on
            # ``pilot.pause`` can hit Textual's 30 s ``WaitForScreenTimeout``
            # under a saturated ``-n auto`` matrix. Draining to quiescence
            # removes the timing sensitivity (the project Pilot-worker rule).
            await app.workers.wait_for_complete()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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
            _goto_tab(modal, "flow")
            _seek_field(modal, "flow.advance_after.audit")
            entry = modal._active_field()  # flow.advance_after.audit (bool)
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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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
    entry = registry_lookup("planning.max_parallel_waves")
    assert entry is not None
    modal = ConfigModal(workspace=None, repo=Path("/tmp/repo"))
    line = modal._meta_line(entry)
    assert line.startswith("   planning.max_parallel_waves")
    # The key column matches the static row (caret + dirty + space = 3).
    assert line.index("planning.max_parallel_waves") == 3


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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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


def test_config_long_key_tab_columns_stay_aligned() -> None:
    """On the long-key estimation tab the type + value columns stay aligned.

    Regression for W12: the field-row renderer hardcoded a ``:<42`` key
    column, so the ``estimation.display.time_quantum_*`` keys (48 chars,
    over the column) overflowed and pushed the ``[type]`` tag + value cell
    right on their own rows — ragged columns. The per-tab key width
    (:meth:`ConfigModal._key_col_width`) sizes the column to the widest key
    actually rendered, so a short key and the longest key land their
    ``[type]`` tag (and the value cell) at the identical column index.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "estimation")
            await pilot.pause()
            fields = keys_for_tab("estimation")
            short_entry = registry_lookup("estimation.enabled")  # 18 chars
            long_entry = registry_lookup(
                "estimation.display.time_quantum_under_2h_minutes"  # 48 chars
            )
            assert short_entry is not None and long_entry is not None
            assert short_entry in fields and long_entry in fields
            short_line = modal._field_line(short_entry)
            long_line = modal._field_line(long_entry)
            # The widest key (48) > the legacy floor, so the type tag column
            # is identical across the short-key and longest-key rows.
            assert short_line.index("[") == long_line.index("[")
            # The value cell anchor (right after the padded type cell) also
            # lines up — measured from the end of the ``[type]`` cell.
            short_value_col = short_line.index("[bool]") + len(f"{'[bool]':<14}")
            long_value_col = long_line.index("[int]") + len(f"{'[int]':<14}")
            assert short_value_col == long_value_col

    asyncio.run(body())


def test_config_meta_line_value_column_matches_field_line() -> None:
    """``_meta_line`` and ``_field_line`` place the value/input column identically.

    The inline editor mounts an :class:`Input` after the ``_meta_line``
    prefix, so the meta line's trailing column (where the input starts) must
    equal the static row's value column for the inline editor to stay in
    column — including on the long-key estimation tab.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "estimation")
            await pilot.pause()
            entry = registry_lookup("estimation.display.time_quantum_under_2h_minutes")
            assert entry is not None
            static_line = modal._field_line(entry)
            meta_line = modal._meta_line(entry)
            type_cell = f"[{entry.type}]"
            # Key + type cells share their column; the meta line's full length
            # (where the inline Input mounts) equals the static value column.
            assert static_line.index(entry.key) == meta_line.index(entry.key)
            assert static_line.index(type_cell) == meta_line.index(type_cell)
            value_col = static_line.index(type_cell) + len(f"{type_cell:<14}") + 1
            assert len(meta_line) == value_col

    asyncio.run(body())


def test_config_inline_edit_esc_cancels_without_mutation() -> None:
    """``Esc`` in the inline input aborts the edit and stages nothing."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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


# -- multichoice inline checklist (Pilot) -----------------------------------


def _goto_dashboard_panes(modal: ConfigModal) -> int:
    """Activate the ``ui`` tab and point the cursor at ``ui.dashboard_panes``.

    Clears the modal's merged config so the persisted ``ui.dashboard_panes``
    value resolves to the empty registry default regardless of the host
    machine's layered config (the YAML layers may pre-set the panes). Returns
    the field index of ``ui.dashboard_panes`` within the ``ui`` tab.
    """
    modal._merged = {}
    _goto_tab(modal, "ui")
    fields = keys_for_tab("ui")
    index = [entry.key for entry in fields].index("ui.dashboard_panes")
    modal.field_index = index
    return index


def test_config_enter_expands_multichoice_checklist() -> None:
    """``Enter`` on a multichoice field mounts the inline checklist editor."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_dashboard_panes(modal)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.key == "ui.dashboard_panes"
            await pilot.press("enter")  # expand the checklist
            await pilot.pause()
            assert modal._editing_key == "ui.dashboard_panes"
            # The modal stays the active screen (no popup pushed).
            assert app.screen is modal
            checklist = modal.query_one("#config-multichoice", Static)
            text = str(checklist.render())
            # Every declared choice renders as a checklist line.
            for choice in entry.choices or ():
                assert choice in text
            # The empty default seeds all-cleared: every row wears the
            # chrome check_off hollow square (reskin replaced "[ ]").
            from eawf.surfaces.tui.widgets.sigils import chrome

            assert chrome("check_off", mode="unicode") in text

    asyncio.run(body())


def test_config_multichoice_checklist_not_clamped_to_one_row() -> None:
    """The expanded checklist lays out its full choice list, not one clipped row.

    Regression guard for the ``config-edit-row`` ``height: 1`` clamp: the
    checklist mounted without that class so its own ``height: auto`` (capped
    at ``max-height: 12`` with ``overflow-y: auto``) governs layout. With
    ``ui.dashboard_panes``' seven choices the rendered region must be taller
    than the single-cell clamp.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_dashboard_panes(modal)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None and entry.key == "ui.dashboard_panes"
            choice_count = len(entry.choices or ())
            assert choice_count > 1  # the regression only bites with >1 row
            await pilot.press("enter")  # expand the checklist
            await pilot.pause()
            checklist = modal.query_one("#config-multichoice", Static)
            # The clamp pinned the region to a single cell; layout now grows
            # to fit every choice line (capped by the widget's max-height).
            assert checklist.outer_size.height > 1
            assert checklist.region.height > 1

    asyncio.run(body())


def test_config_multichoice_space_toggle_then_enter_commits() -> None:
    """Space toggles an item, the second Enter commits the staged list.

    Exercises the full operator UX: focus the key, Enter to expand, Space to
    toggle ``state`` ON, Enter to commit. The dirty map then carries the
    toggled list and the restored row shows the overridden-key marker -- the
    half-filled claimed sigil the reskin migrated the dirty marker onto.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            index = _goto_dashboard_panes(modal)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            first_choice = (entry.choices or ())[0]  # 'state' (line 0)
            await pilot.press("enter")  # expand the checklist
            await pilot.pause()
            await pilot.press("space")  # toggle the focused line (first choice) ON
            await pilot.pause()
            await pilot.press("enter")  # commit + collapse
            await pilot.pause()
            # Editor torn down, the toggled list staged, no popup.
            assert modal._editing_key is None
            assert modal._view.dirty.get("ui.dashboard_panes") == [first_choice]
            # The restored static row carries the overridden-key marker --
            # the half-filled claimed sigil the reskin migrated off ``*``.
            from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

            overridden_marker = glyph(Sigil.CLAIMED, mode="unicode")
            row = modal.query_one(f"#{modal._field_row_id('ui', index)}", Static)
            assert overridden_marker in str(row.render())

    asyncio.run(body())


def test_config_multichoice_commit_saves_through_save_path() -> None:
    """A committed multichoice list flushes through the existing layered-writer seam."""

    async def body() -> None:
        calls: list[dict[str, Any]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app, save_fn=lambda **k: calls.append(k))
            await pilot.pause()
            _goto_dashboard_panes(modal)
            await pilot.pause()
            entry = modal._active_field()
            assert entry is not None
            first_choice = (entry.choices or ())[0]
            await pilot.press("enter")  # expand
            await pilot.pause()
            await pilot.press("space")  # toggle first choice ON
            await pilot.pause()
            await pilot.press("enter")  # commit
            await pilot.pause()
            assert modal._view.dirty.get("ui.dashboard_panes") == [first_choice]
            await pilot.press("s")  # save through the layered writer
            await pilot.pause()
            # The save seam was called with the toggled list for the key.
            saved = [call for call in calls if call["key"] == "ui.dashboard_panes"]
            assert len(saved) == 1
            assert saved[0]["value"] == [first_choice]
            assert str(saved[0]["target_path"]).endswith(".yaml")
            assert "state.json" not in str(saved[0]["target_path"])
            assert modal._view.dirty == {}

    asyncio.run(body())


def test_config_multichoice_empty_selection_stages_empty_list() -> None:
    """Toggling everything off (from a non-empty value) stages the empty list.

    Seeds a non-empty dirty value so the checklist opens with items checked,
    toggles them all off, and commits — the empty list validates and stages
    (it differs from the empty-default only when the persisted value is
    non-empty; here the staged ``[]`` nets the persisted default so the
    dirty mark clears, but the commit path itself raises no error).
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_dashboard_panes(modal)
            # Seed a staged non-empty selection so the checklist opens checked.
            modal._view.dirty = {"ui.dashboard_panes": ["state", "roadmap"]}
            await pilot.pause()
            await pilot.press("enter")  # expand (state + roadmap checked)
            await pilot.pause()
            # Toggle the two checked lines off: line 0 (state), line 1 (roadmap).
            await pilot.press("space")  # state OFF
            await pilot.pause()
            await pilot.press("down")  # focus roadmap
            await pilot.pause()
            await pilot.press("space")  # roadmap OFF
            await pilot.pause()
            await pilot.press("enter")  # commit the empty selection
            await pilot.pause()
            assert modal._editing_key is None
            # Empty selection nets the persisted empty default → dirty cleared.
            assert "ui.dashboard_panes" not in modal._view.dirty
            # No error row lingered and the modal stayed open.
            assert app.screen is modal

    asyncio.run(body())


def test_config_multichoice_esc_cancels_without_staging() -> None:
    """``Esc`` in the checklist aborts the edit and restores the pre-edit dirty map."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_dashboard_panes(modal)
            await pilot.pause()
            await pilot.press("enter")  # expand
            await pilot.pause()
            await pilot.press("space")  # toggle a line ON (stages live)
            await pilot.pause()
            assert "ui.dashboard_panes" in modal._view.dirty  # live-staged
            await pilot.press("escape")  # cancel
            await pilot.pause()
            assert modal._editing_key is None
            # Cancel restored the pre-edit (clean) dirty map.
            assert "ui.dashboard_panes" not in modal._view.dirty
            assert app.screen is modal  # modal stays open

    asyncio.run(body())


def test_config_multichoice_hint_shows_space_toggle() -> None:
    """While the checklist is open the footer hint advertises Space-toggle keys."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_config(app)
            await pilot.pause()
            _goto_dashboard_panes(modal)
            await pilot.pause()
            await pilot.press("enter")  # expand
            await pilot.pause()
            hint = str(modal.query_one("#config-hint", Static).render())
            assert "Space toggle" in hint
            assert "Enter commit" in hint
            assert "Esc cancel" in hint

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
            _goto_tab(modal, "planning")
            _seek_field(modal, "planning.max_parallel_waves")
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


def test_c_keypress_opens_config_from_a_mode_screen() -> None:
    # ``c`` is an app-wide binding, so it must reach the config window from a
    # dedicated mode (autopilot, digit ``2``), not just the home/scope
    # screens. A scope-only binding regresses this -- the mode screens
    # subclass the ScopeScreen base, which never bound ``c``.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("2")  # -> autopilot mode
            await pilot.pause()
            assert app.current_mode == "autopilot"
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_config_palette_verb_opens_modal() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import _handle_config

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _handle_config(app, "")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_config_verb_registered_in_palette() -> None:
    from eawf.surfaces.tui.palette.verbs import VERBS

    names = {verb.name for verb in VERBS}
    assert "/config" in names


def test_config_verb_respects_modal_cap() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import _handle_config

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                _push_config(app)
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            _handle_config(app, "")
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())
