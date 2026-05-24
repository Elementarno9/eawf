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

from eawf.cli.errors import UserError
from eawf.kernel.config.registry import (
    CONFIG_REGISTRY,
    ConfigKey,
    coerce_and_validate,
    is_known_key,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
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
