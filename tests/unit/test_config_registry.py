"""Unit tests for :mod:`eawf.kernel.config.registry`.

Covers:

- Each registry entry validates against its declared type (coercing the
  default must round-trip).
- Ordering helpers (:func:`tabs_sorted` / :func:`keys_for_tab`) return
  alphabetical results.
- :func:`registry_lookup` round-trips for every registered key.
- :func:`coerce_and_validate` accepts in-range values and rejects
  out-of-range / unknown-choice inputs with :class:`InvalidInput`.
- :class:`ConfigKey` rejects invalid construction (extra keys, empty
  choices tuple).
- The module-level sort-invariant assertion fires (every entry sorts on
  its key) and the unique-key invariant fires (no duplicates).
"""

from __future__ import annotations

import pytest

from eawf.kernel.config.registry import (
    CONFIG_REGISTRY,
    LEAF_KEY_REGISTRY,
    ConfigKey,
    coerce_and_validate,
    is_known_key,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)
from eawf.surfaces.cli.errors import UserError

#: The adapter-enable + budget keys promoted into the operator-tunable
#: registry (P29-I13-W18). Each must now resolve as a recognized,
#: layer-aware config key so the TUI config modal (W19) can surface it.
_PROMOTED_KEYS: tuple[str, ...] = (
    "runtime.adapter_catalog.claude.enabled",
    "runtime.adapter_catalog.codex.enabled",
    "runtime.adapter_catalog.opencode.enabled",
    "flow.budget.multiplier",
)


def test_registry_non_empty() -> None:
    """A useful menu requires at least one tunable entry."""
    assert len(CONFIG_REGISTRY) > 0


def test_registry_keys_unique() -> None:
    """No two registry entries may share the same dotted key."""
    keys = [entry.key for entry in CONFIG_REGISTRY]
    assert len(set(keys)) == len(keys)


def test_registry_stored_sorted_by_key() -> None:
    """Diff hygiene: the canonical tuple is stored sorted alphabetical-by-key."""
    sorted_keys = sorted(entry.key for entry in CONFIG_REGISTRY)
    actual_keys = [entry.key for entry in CONFIG_REGISTRY]
    assert actual_keys == sorted_keys


def test_each_entry_default_passes_its_own_validation() -> None:
    """Every default must coerce + validate against the entry's declared type."""
    for entry in CONFIG_REGISTRY:
        coerced = coerce_and_validate(entry, entry.default)
        if entry.type == "bool":
            assert isinstance(coerced, bool)
        elif entry.type == "int":
            assert isinstance(coerced, int) and not isinstance(coerced, bool)
        elif entry.type == "float":
            assert isinstance(coerced, float)
        elif entry.type == "str":
            assert isinstance(coerced, str)
        elif entry.type == "choice":
            assert isinstance(coerced, str)
            assert entry.choices is not None
            assert coerced in entry.choices
        elif entry.type == "multichoice":
            assert isinstance(coerced, list)


def test_tabs_sorted_is_alphabetical() -> None:
    """The tab list is returned alphabetically (success criterion #1)."""
    tabs = tabs_sorted()
    assert tabs == tuple(sorted(tabs))


def test_tabs_sorted_covers_every_registry_tab() -> None:
    """No tab in the registry is hidden from the menu."""
    expected = {entry.tab for entry in CONFIG_REGISTRY}
    assert set(tabs_sorted()) == expected


def test_keys_for_tab_is_alphabetical() -> None:
    """Fields within a tab are alphabetical by dotted key (success criterion #2)."""
    for tab in tabs_sorted():
        keys = [entry.key for entry in keys_for_tab(tab)]
        assert keys == sorted(keys)


def test_keys_for_unknown_tab_is_empty() -> None:
    """Unknown tabs return an empty tuple — defensive for the menu renderer."""
    assert keys_for_tab("no-such-tab") == ()


def test_registry_lookup_round_trips_every_entry() -> None:
    """Lookup must hit every registered key."""
    for entry in CONFIG_REGISTRY:
        assert registry_lookup(entry.key) is entry


def test_registry_lookup_unknown_returns_none() -> None:
    """Unknown keys return ``None`` rather than raising."""
    assert registry_lookup("no.such.key.path") is None


# --- coerce_and_validate -----------------------------------------------------


def test_coerce_bool_accepts_string_forms() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.b",
        label="bool key",
        type="bool",
        default=False,
    )
    assert coerce_and_validate(entry, "true") is True
    assert coerce_and_validate(entry, "FALSE") is False
    assert coerce_and_validate(entry, "yes") is True
    assert coerce_and_validate(entry, True) is True


def test_coerce_bool_rejects_garbage() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.b",
        label="bool key",
        type="bool",
        default=False,
    )
    with pytest.raises(UserError, match="bool"):
        coerce_and_validate(entry, "maybe")


def test_coerce_int_within_range() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.n",
        label="int",
        type="int",
        default=4,
        min_value=1,
        max_value=10,
    )
    assert coerce_and_validate(entry, "5") == 5
    assert coerce_and_validate(entry, 7) == 7


def test_coerce_int_below_minimum_rejected() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.n",
        label="int",
        type="int",
        default=4,
        min_value=1,
        max_value=10,
    )
    with pytest.raises(UserError, match="below minimum"):
        coerce_and_validate(entry, "0")


def test_coerce_int_above_maximum_rejected() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.n",
        label="int",
        type="int",
        default=4,
        min_value=1,
        max_value=10,
    )
    with pytest.raises(UserError, match="above maximum"):
        coerce_and_validate(entry, "11")


def test_coerce_int_rejects_non_integer_string() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.n",
        label="int",
        type="int",
        default=1,
    )
    with pytest.raises(UserError, match="int"):
        coerce_and_validate(entry, "abc")


def test_coerce_float_handles_decimal() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.f",
        label="float",
        type="float",
        default=0.5,
        min_value=0.0,
        max_value=1.0,
    )
    assert coerce_and_validate(entry, "0.25") == pytest.approx(0.25)


def test_coerce_float_below_minimum_rejected() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.f",
        label="float",
        type="float",
        default=0.5,
        min_value=0.0,
        max_value=1.0,
    )
    with pytest.raises(UserError, match="below minimum"):
        coerce_and_validate(entry, "-0.1")


def test_coerce_str_passes_through() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.s",
        label="str",
        type="str",
        default="hi",
    )
    assert coerce_and_validate(entry, "anything") == "anything"


def test_coerce_choice_accepts_known_value() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.c",
        label="choice",
        type="choice",
        default="alpha",
        choices=("alpha", "beta"),
    )
    assert coerce_and_validate(entry, "beta") == "beta"


def test_coerce_choice_rejects_unknown_value() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.c",
        label="choice",
        type="choice",
        default="alpha",
        choices=("alpha", "beta"),
    )
    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, "gamma")


def test_coerce_multichoice_accepts_list() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.m",
        label="multichoice",
        type="multichoice",
        default=[],
        choices=("a", "b", "c"),
    )
    assert coerce_and_validate(entry, ["a", "c"]) == ["a", "c"]


def test_coerce_multichoice_accepts_comma_separated_string() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.m",
        label="multichoice",
        type="multichoice",
        default=[],
        choices=("a", "b", "c"),
    )
    assert coerce_and_validate(entry, "a, b") == ["a", "b"]


def test_coerce_multichoice_rejects_unknown_member() -> None:
    entry = ConfigKey(
        tab="t",
        key="t.m",
        label="multichoice",
        type="multichoice",
        default=[],
        choices=("a", "b"),
    )
    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, "a,zz")


# --- ConfigKey model contracts ------------------------------------------------


def test_config_key_rejects_extra_field() -> None:
    """``extra="forbid"`` per project-wide Pydantic rule 2."""
    with pytest.raises(Exception):  # noqa: B017  pydantic v2 raises ValidationError
        ConfigKey(  # type: ignore[call-arg]
            tab="t",
            key="t.k",
            label="x",
            type="bool",
            default=False,
            unknown_field="oops",  # type: ignore[arg-type]
        )


def test_config_key_rejects_empty_choices() -> None:
    """A choice key with empty choices tuple is unreachable — must be rejected."""
    with pytest.raises(Exception):  # noqa: B017  pydantic v2 raises ValidationError
        ConfigKey(
            tab="t",
            key="t.c",
            label="choice",
            type="choice",
            default="x",
            choices=(),
        )


def test_config_key_frozen() -> None:
    """Frozen model — mutating an instance after construction must fail."""
    entry = ConfigKey(
        tab="t",
        key="t.k",
        label="x",
        type="bool",
        default=False,
    )
    with pytest.raises(Exception):  # noqa: B017  pydantic v2 raises ValidationError
        entry.label = "mutated"  # type: ignore[misc]


# --- is_known_key -------------------------------------------------------------


def test_is_known_key_for_registry_entry() -> None:
    """Any registry entry is reported as known regardless of merged content."""
    entry = CONFIG_REGISTRY[0]
    assert is_known_key({}, entry.key)


def test_is_known_key_for_merged_only() -> None:
    """A key that exists in merged but not the registry is still known."""
    merged = {"foo": {"bar": 1}}
    assert is_known_key(merged, "foo.bar")


def test_is_known_key_unknown_returns_false() -> None:
    assert not is_known_key({"foo": {"bar": 1}}, "no.such.key")
    assert not is_known_key(None, "no.such.key")


# --- W18: promoted adapter-enable + budget keys ------------------------------


@pytest.mark.parametrize("key", _PROMOTED_KEYS)
def test_promoted_key_is_registered(key: str) -> None:
    # Each promoted key resolves to a CONFIG_REGISTRY entry, so the operator-
    # tunable menu + the TUI config modal both surface it.
    entry = registry_lookup(key)
    assert entry is not None, f"promoted key {key!r} is not in CONFIG_REGISTRY"
    assert entry.key == key


@pytest.mark.parametrize("key", _PROMOTED_KEYS)
def test_promoted_key_is_recognized_as_known(key: str) -> None:
    # boundary: a promoted registry key is reported known regardless of merged
    # content, so a write to it is no longer rejected as an unknown key.
    assert is_known_key({}, key)


@pytest.mark.parametrize("key", _PROMOTED_KEYS)
def test_promoted_key_is_layer_aware(key: str) -> None:
    # The promoted keys are layer-aware: each has a leaf-catalog row declaring
    # its writable layers, which the W19 config-modal lock reads to decide
    # whether the key edits or renders read-only on the active save layer. A
    # promoted key with no leaf row would render editable on every layer (the
    # is_editable_key missing-row fallback), defeating the lock.
    leaf = LEAF_KEY_REGISTRY.get(key)
    assert leaf is not None, f"promoted key {key!r} has no leaf-catalog row"
    assert leaf.writable_layers, f"promoted key {key!r} declares no writable layer"


def test_adapter_enable_keys_are_repo_locked() -> None:
    # The adapter-enable keys are writable only on the repo layer, so they
    # render read-only on every other layer the modal can target -- the exact
    # lock W19's snapshot pins.
    for key in (
        "runtime.adapter_catalog.claude.enabled",
        "runtime.adapter_catalog.codex.enabled",
        "runtime.adapter_catalog.opencode.enabled",
    ):
        leaf = LEAF_KEY_REGISTRY.get(key)
        assert leaf is not None
        assert leaf.writable_layers == ("repo",)


def test_promoted_key_defaults_match_leaf_catalog() -> None:
    # The registry default and the leaf-catalog default must agree so the
    # modal's resolved value (merged -> registry default) matches the daemon's
    # built-in layer; a divergent default would flag a phantom dirty edit.
    for key in _PROMOTED_KEYS:
        entry = registry_lookup(key)
        leaf = LEAF_KEY_REGISTRY.get(key)
        assert entry is not None and leaf is not None
        assert entry.default == leaf.default, f"default drift for {key!r}"


def test_budget_multiplier_rejects_below_minimum() -> None:
    # error-path: the budget multiplier is bounded at >= 1.0 (a cap below the
    # base budget is nonsensical), so an under-range value is rejected.
    entry = registry_lookup("flow.budget.multiplier")
    assert entry is not None
    with pytest.raises(UserError, match="below minimum"):
        coerce_and_validate(entry, "0.5")


# --- W26: ui.tour_completed onboarding-state leaf ----------------------------


def test_tour_completed_key_is_registered() -> None:
    # The first-run tour-completed flag resolves to a CONFIG_REGISTRY entry so
    # the operator-tunable menu + TUI config modal both surface it.
    entry = registry_lookup("ui.tour_completed")
    assert entry is not None
    assert entry.key == "ui.tour_completed"
    assert entry.type == "bool"
    assert entry.tab == "ui"


def test_tour_completed_default_is_false() -> None:
    # boundary: a fresh install has not seen the tour, so the default is False
    # (the tour opens on first run and flips this to True on dismissal).
    entry = registry_lookup("ui.tour_completed")
    assert entry is not None
    assert entry.default is False
    assert coerce_and_validate(entry, entry.default) is False


def test_tour_completed_is_recognized_as_known() -> None:
    # boundary: the leaf is reported known regardless of merged content, so a
    # write to it from the tour-dismiss path is not rejected as an unknown key.
    assert is_known_key({}, "ui.tour_completed")


def test_tour_completed_is_layer_aware() -> None:
    # The leaf is layer-aware: a leaf-catalog row declares its writable layers
    # so the config-modal lock and the tour-dismiss writer both resolve it.
    leaf = LEAF_KEY_REGISTRY.get("ui.tour_completed")
    assert leaf is not None
    assert leaf.type == "bool"
    assert leaf.writable_layers


def test_tour_completed_registry_and_leaf_defaults_agree() -> None:
    # The registry default and the leaf-catalog default must agree so the
    # modal's resolved value matches the daemon's built-in layer.
    entry = registry_lookup("ui.tour_completed")
    leaf = LEAF_KEY_REGISTRY.get("ui.tour_completed")
    assert entry is not None and leaf is not None
    assert entry.default == leaf.default


def test_tour_completed_rejects_garbage_bool() -> None:
    # error-path: a non-boolean value is rejected with the bool coercion error.
    entry = registry_lookup("ui.tour_completed")
    assert entry is not None
    with pytest.raises(UserError, match="bool"):
        coerce_and_validate(entry, "maybe")
