"""Unit tests for the statusline glyph-mode / color-mode / rows config keys.

The B018 statusline surface (P29-I13) adds three operator-tunable knobs that
the render layer resolves: ``statusline.glyph_mode`` and
``statusline.color_mode`` (literal/choice keys) plus ``statusline.rows`` (a
bounded int). These tests pin that each key is registered in both the
operator-tunable ``CONFIG_REGISTRY`` and the layer-aware leaf catalog, that
defaults agree across the two registries, and that the choice/bounds
validation rejects out-of-range or unknown-enum values.
"""

from __future__ import annotations

import pytest

from eawf.kernel.config.registry import (
    LEAF_KEY_REGISTRY,
    coerce_and_validate,
    is_known_key,
    registry_lookup,
)
from eawf.surfaces.cli.errors import UserError

#: The three statusline config knobs added by the B018 surface.
_STATUSLINE_KEYS: tuple[str, ...] = (
    "statusline.glyph_mode",
    "statusline.color_mode",
    "statusline.rows",
)


@pytest.mark.parametrize("key", _STATUSLINE_KEYS)
def test_statusline_key_is_registered(key: str) -> None:
    # Each key resolves to a CONFIG_REGISTRY entry, so the operator-tunable
    # menu + the TUI config modal both surface it.
    entry = registry_lookup(key)
    assert entry is not None, f"statusline key {key!r} is not in CONFIG_REGISTRY"
    assert entry.key == key
    assert entry.tab == "statusline"


@pytest.mark.parametrize("key", _STATUSLINE_KEYS)
def test_statusline_key_is_recognized_as_known(key: str) -> None:
    # boundary: a registered key is reported known regardless of merged
    # content, so a write to it is not rejected as an unknown key.
    assert is_known_key({}, key)


@pytest.mark.parametrize("key", _STATUSLINE_KEYS)
def test_statusline_key_is_layer_aware(key: str) -> None:
    # Each key is layer-aware: a leaf-catalog row declares its writable
    # layers so the config-modal lock and the render resolver both resolve it.
    leaf = LEAF_KEY_REGISTRY.get(key)
    assert leaf is not None, f"statusline key {key!r} has no leaf-catalog row"
    assert leaf.writable_layers, f"statusline key {key!r} declares no writable layer"


@pytest.mark.parametrize("key", _STATUSLINE_KEYS)
def test_statusline_registry_and_leaf_defaults_agree(key: str) -> None:
    # The registry default and the leaf-catalog default must agree so the
    # modal's resolved value matches the daemon's built-in layer; a divergent
    # default would flag a phantom dirty edit.
    entry = registry_lookup(key)
    leaf = LEAF_KEY_REGISTRY.get(key)
    assert entry is not None and leaf is not None
    assert entry.default == leaf.default, f"default drift for {key!r}"


def test_glyph_mode_default_is_auto() -> None:
    # boundary: a fresh install defers glyph selection to the terminal probe.
    entry = registry_lookup("statusline.glyph_mode")
    assert entry is not None
    assert entry.default == "auto"
    assert coerce_and_validate(entry, entry.default) == "auto"


def test_glyph_mode_accepts_each_choice() -> None:
    entry = registry_lookup("statusline.glyph_mode")
    assert entry is not None
    assert entry.choices == ("auto", "ascii", "unicode")
    for choice in entry.choices:
        assert coerce_and_validate(entry, choice) == choice


def test_glyph_mode_rejects_unknown_choice() -> None:
    # error-path: an enum value outside the declared choices is rejected.
    entry = registry_lookup("statusline.glyph_mode")
    assert entry is not None
    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, "braille")


def test_color_mode_accepts_each_choice() -> None:
    entry = registry_lookup("statusline.color_mode")
    assert entry is not None
    assert entry.choices == ("auto", "always", "never")
    for choice in entry.choices:
        assert coerce_and_validate(entry, choice) == choice


def test_color_mode_rejects_unknown_choice() -> None:
    # error-path: an enum value outside the declared choices is rejected.
    entry = registry_lookup("statusline.color_mode")
    assert entry is not None
    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, "rainbow")


def test_rows_default_is_one() -> None:
    # boundary: a single-row statusline is the conservative default.
    entry = registry_lookup("statusline.rows")
    assert entry is not None
    assert entry.default == 1
    assert coerce_and_validate(entry, entry.default) == 1


def test_rows_accepts_in_range_values() -> None:
    entry = registry_lookup("statusline.rows")
    assert entry is not None
    assert entry.min_value == 1
    assert entry.max_value == 3
    for value in (1, 2, 3):
        assert coerce_and_validate(entry, value) == value


def test_rows_rejects_below_minimum() -> None:
    # error-path: zero rows would render nothing, so it is rejected.
    entry = registry_lookup("statusline.rows")
    assert entry is not None
    with pytest.raises(UserError, match="below minimum"):
        coerce_and_validate(entry, "0")


def test_rows_rejects_above_maximum() -> None:
    # error-path: a four-row statusline exceeds the supported layout.
    entry = registry_lookup("statusline.rows")
    assert entry is not None
    with pytest.raises(UserError, match="above maximum"):
        coerce_and_validate(entry, "4")


def test_rows_rejects_non_integer() -> None:
    # error-path: a non-numeric row count is rejected at coercion.
    entry = registry_lookup("statusline.rows")
    assert entry is not None
    with pytest.raises(UserError, match="int"):
        coerce_and_validate(entry, "two")
