"""P29-I02-W03: strict validation for the ``preferences`` config section.

Covers the three operator-tunable preference keys added by this wave —
``preferences.solution_bias`` / ``preferences.scope_size`` /
``preferences.auto_choose`` — across the surfaces that enforce their
closed-enum contract:

- :class:`~eawf.kernel.config.schema.PreferencesConfig` — the strict
  section model. Valid enum values are accepted, an unknown enum value
  raises :class:`pydantic.ValidationError`, omitted fields fall back to
  the documented default, and an unknown ``preferences.*`` key is
  rejected by ``extra="forbid"``.
- An old-shape config body without a ``preferences`` block defaults
  cleanly (the section model materialises from an empty mapping and the
  legacy migration leaves the body untouched).
- The leaf catalog (:data:`LEAF_KEY_REGISTRY`) resolves the three rows
  via the registry accessors.
- The operator-facing menu registry (:data:`CONFIG_REGISTRY`) carries
  the three rows and :func:`coerce_and_validate` rejects an out-of-enum
  value with :class:`InvalidInput`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry import (
    LEAF_KEY_REGISTRY,
    coerce_and_validate,
    is_known_leaf_key,
    leaf_key_lookup,
    leaf_keys_by_domain,
    registry_lookup,
)
from eawf.kernel.config.schema import (
    AutoChoose,
    PreferencesConfig,
    SolutionBias,
)
from eawf.kernel.state.enums import EffortBucket
from eawf.surfaces.cli.errors import UserError

_PREFERENCE_KEYS = (
    "preferences.solution_bias",
    "preferences.scope_size",
    "preferences.auto_choose",
)


# --- PreferencesConfig: defaults --------------------------------------------


def test_preferences_config_defaults_applied_when_omitted() -> None:
    """Omitting every field falls back to the documented enum defaults."""
    cfg = PreferencesConfig()
    assert cfg.solution_bias is SolutionBias.BALANCED
    assert cfg.scope_size is EffortBucket.M
    assert cfg.auto_choose is AutoChoose.OFF


def test_preferences_config_defaults_from_empty_mapping() -> None:
    """An empty mapping materialises the same defaults (old-shape round-trip)."""
    cfg = PreferencesConfig.model_validate({})
    assert cfg.solution_bias is SolutionBias.BALANCED
    assert cfg.scope_size is EffortBucket.M
    assert cfg.auto_choose is AutoChoose.OFF


# --- PreferencesConfig: valid enum values -----------------------------------


def test_preferences_config_accepts_valid_solution_bias() -> None:
    cfg = PreferencesConfig.model_validate({"solution_bias": "thorough"})
    assert cfg.solution_bias is SolutionBias.THOROUGH


def test_preferences_config_accepts_valid_scope_size() -> None:
    cfg = PreferencesConfig.model_validate({"scope_size": "XL"})
    assert cfg.scope_size is EffortBucket.XL


def test_preferences_config_accepts_valid_auto_choose() -> None:
    cfg = PreferencesConfig.model_validate({"auto_choose": "always"})
    assert cfg.auto_choose is AutoChoose.ALWAYS


# --- PreferencesConfig: error paths -----------------------------------------


def test_preferences_config_rejects_unknown_solution_bias() -> None:
    """An out-of-enum solution_bias value fails at the model boundary."""
    with pytest.raises(ValidationError, match="solution_bias"):
        PreferencesConfig.model_validate({"solution_bias": "bogus"})


def test_preferences_config_rejects_unknown_scope_size() -> None:
    with pytest.raises(ValidationError, match="scope_size"):
        PreferencesConfig.model_validate({"scope_size": "HUGE"})


def test_preferences_config_rejects_unknown_auto_choose() -> None:
    with pytest.raises(ValidationError, match="auto_choose"):
        PreferencesConfig.model_validate({"auto_choose": "sometimes"})


def test_preferences_config_rejects_unknown_key() -> None:
    """An unknown ``preferences.*`` key is rejected by ``extra="forbid"``."""
    with pytest.raises(ValidationError, match="bogus_pref"):
        PreferencesConfig.model_validate({"bogus_pref": "x"})


# --- defaults + legacy round-trip -------------------------------------------


def test_built_in_defaults_carry_preferences_block() -> None:
    """The built-in defaults expose a ``preferences`` block matching the model."""
    block = built_in_defaults()["preferences"]
    assert block == {
        "solution_bias": "balanced",
        "scope_size": "M",
        "auto_choose": "off",
    }
    # The block round-trips through the strict model unchanged.
    cfg = PreferencesConfig.model_validate(block)
    assert cfg.solution_bias is SolutionBias.BALANCED


def test_legacy_body_without_preferences_migrates_unchanged() -> None:
    """A canonical 1.0 body lacking ``preferences`` is left untouched by migration."""
    payload = {
        "schema_version": "1.0",
        "runtime": {"adapters": ["claude"], "preference": ["claude"]},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is False
    assert "preferences" not in upgraded
    # The section model still defaults cleanly for such a body.
    assert PreferencesConfig().auto_choose is AutoChoose.OFF


# --- leaf catalog -----------------------------------------------------------


def test_leaf_catalog_resolves_every_preference_key() -> None:
    """Each new key resolves via the strict catalog lookup accessor."""
    for key in _PREFERENCE_KEYS:
        entry = leaf_key_lookup(key)
        assert entry.key == key
        assert entry.domain == "preferences"
        assert entry.type == "literal"
        assert entry.choices is not None
        assert is_known_leaf_key(key) is True


def test_leaf_keys_by_domain_groups_preferences() -> None:
    """The domain filter returns exactly the three preference rows."""
    rows = leaf_keys_by_domain("preferences")
    assert {row.key for row in rows} == set(_PREFERENCE_KEYS)


def test_leaf_catalog_preference_defaults_are_declared_choices() -> None:
    """Each row's default is one of its own declared choices."""
    for key in _PREFERENCE_KEYS:
        entry = LEAF_KEY_REGISTRY[key]
        assert entry.choices is not None
        assert entry.default in entry.choices


def test_leaf_catalog_scope_size_choices_match_effort_bucket() -> None:
    """``scope_size`` reuses the canonical effort-bucket value set."""
    entry = leaf_key_lookup("preferences.scope_size")
    assert entry.choices == tuple(b.value for b in EffortBucket)


# --- menu registry (eawf config surface) ------------------------------------


def test_menu_registry_carries_every_preference_key() -> None:
    """The operator-facing menu registry exposes the three preference rows."""
    for key in _PREFERENCE_KEYS:
        entry = registry_lookup(key)
        assert entry is not None
        assert entry.tab == "preferences"
        assert entry.type == "choice"


def test_menu_registry_accepts_valid_preference_value() -> None:
    entry = registry_lookup("preferences.solution_bias")
    assert entry is not None
    assert coerce_and_validate(entry, "simple") == "simple"


def test_menu_registry_rejects_invalid_preference_value() -> None:
    """A bogus enum value is rejected with the canonical InvalidInput error."""
    entry = registry_lookup("preferences.solution_bias")
    assert entry is not None
    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, "bogus")
