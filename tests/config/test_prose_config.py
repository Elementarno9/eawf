"""P29-I07-W01: typed ``prose`` config section + the doc-clarity authority guard.

Covers the doc-clarity prose-lint config surface added by this wave:

- :class:`~eawf.kernel.config.schema.ProseConfig` — the strict section
  model. Valid levels are accepted, an unknown level raises
  :class:`pydantic.ValidationError`, omitted fields fall back to the
  documented defaults, and an unknown ``prose.*`` key is rejected by
  ``extra="forbid"``.
- :func:`~eawf.kernel.config.schema.assert_prose_not_weaker_than` — the
  authority guard. A local layer that tightens or matches the baseline is
  accepted; a local layer that loosens below the baseline raises
  :class:`ValueError`.
- The built-in defaults expose a ``prose`` block that round-trips through
  the strict model, and a legacy body lacking ``prose`` defaults cleanly.
- The leaf catalog (:data:`LEAF_KEY_REGISTRY`) resolves the three rows and
  the operator-facing menu registry carries ``prose.level``.
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
    ProseConfig,
    ProseLevel,
    assert_prose_not_weaker_than,
    prose_level_rank,
)
from eawf.surfaces.cli.errors import UserError

_PROSE_KEYS = (
    "prose.level",
    "prose.clarity_judge",
    "prose.block_on_lint",
)


# --- ProseConfig: defaults --------------------------------------------------


def test_prose_config_defaults_applied_when_omitted() -> None:
    """Omitting every field falls back to the documented defaults."""
    cfg = ProseConfig()
    assert cfg.level is ProseLevel.STANDARD
    assert cfg.clarity_judge is None
    assert cfg.block_on_lint is None


def test_prose_config_defaults_from_empty_mapping() -> None:
    """An empty mapping materialises the same defaults (old-shape round-trip)."""
    cfg = ProseConfig.model_validate({})
    assert cfg.level is ProseLevel.STANDARD


# --- ProseConfig: valid values ----------------------------------------------


def test_prose_config_accepts_strict_level() -> None:
    cfg = ProseConfig.model_validate({"level": "strict"})
    assert cfg.level is ProseLevel.STRICT


def test_prose_config_accepts_loose_level() -> None:
    cfg = ProseConfig.model_validate({"level": "loose"})
    assert cfg.level is ProseLevel.LOOSE


def test_prose_config_accepts_explicit_gate_overrides() -> None:
    cfg = ProseConfig.model_validate(
        {"level": "standard", "clarity_judge": True, "block_on_lint": False}
    )
    assert cfg.clarity_judge is True
    assert cfg.block_on_lint is False


# --- ProseConfig: error paths -----------------------------------------------


def test_prose_config_rejects_unknown_level() -> None:
    """An out-of-enum level value fails at the model boundary."""
    with pytest.raises(ValidationError, match="level"):
        ProseConfig.model_validate({"level": "ferocious"})


def test_prose_config_rejects_unknown_key() -> None:
    """An unknown ``prose.*`` key is rejected by ``extra="forbid"``."""
    with pytest.raises(ValidationError, match="bogus_prose"):
        ProseConfig.model_validate({"bogus_prose": "x"})


# --- level ranking ----------------------------------------------------------


def test_prose_level_rank_is_strictly_increasing() -> None:
    """The rank ladder runs loose < standard < strict."""
    assert prose_level_rank(ProseLevel.LOOSE) < prose_level_rank(ProseLevel.STANDARD)
    assert prose_level_rank(ProseLevel.STANDARD) < prose_level_rank(ProseLevel.STRICT)


def test_prose_level_rank_covers_every_member() -> None:
    """Every enum member has a rank (a new member without a rank trips KeyError)."""
    for level in ProseLevel:
        assert isinstance(prose_level_rank(level), int)


# --- authority guard: tightening / matching is accepted ---------------------


def test_authority_guard_accepts_equal_level() -> None:
    """Matching the baseline is allowed (no tightening, no loosening)."""
    baseline = ProseConfig(level=ProseLevel.STANDARD)
    candidate = ProseConfig(level=ProseLevel.STANDARD)
    assert assert_prose_not_weaker_than(baseline, candidate) is candidate


def test_authority_guard_accepts_tightening() -> None:
    """A local layer raising the floor toward strict is allowed."""
    baseline = ProseConfig(level=ProseLevel.LOOSE)
    candidate = ProseConfig(level=ProseLevel.STRICT)
    assert assert_prose_not_weaker_than(baseline, candidate) is candidate


def test_authority_guard_accepts_one_step_tighten() -> None:
    baseline = ProseConfig(level=ProseLevel.STANDARD)
    candidate = ProseConfig(level=ProseLevel.STRICT)
    assert assert_prose_not_weaker_than(baseline, candidate).level is ProseLevel.STRICT


def test_tightens_or_equals_matches_guard() -> None:
    """The boolean helper agrees with the raising guard on the accept cases."""
    strict = ProseConfig(level=ProseLevel.STRICT)
    loose = ProseConfig(level=ProseLevel.LOOSE)
    assert strict.tightens_or_equals(loose) is True
    assert loose.tightens_or_equals(strict) is False


# --- authority guard: loosening is rejected ---------------------------------


def test_authority_guard_rejects_loosening_below_strict_baseline() -> None:
    """A local layer dropping a strict baseline to standard is rejected."""
    baseline = ProseConfig(level=ProseLevel.STRICT)
    candidate = ProseConfig(level=ProseLevel.STANDARD)
    with pytest.raises(ValueError, match="loosens below the baseline"):
        assert_prose_not_weaker_than(baseline, candidate)


def test_authority_guard_rejects_loosening_to_loose() -> None:
    """Dropping a standard baseline all the way to loose is rejected."""
    baseline = ProseConfig(level=ProseLevel.STANDARD)
    candidate = ProseConfig(level=ProseLevel.LOOSE)
    with pytest.raises(ValueError, match="local config may only tighten"):
        assert_prose_not_weaker_than(baseline, candidate)


def test_authority_guard_message_names_both_levels() -> None:
    """The rejection message names the candidate and baseline levels."""
    baseline = ProseConfig(level=ProseLevel.STRICT)
    candidate = ProseConfig(level=ProseLevel.LOOSE)
    with pytest.raises(ValueError) as excinfo:
        assert_prose_not_weaker_than(baseline, candidate)
    msg = str(excinfo.value)
    assert "loose" in msg
    assert "strict" in msg


# --- defaults + legacy round-trip -------------------------------------------


def test_built_in_defaults_carry_prose_block() -> None:
    """The built-in defaults expose a ``prose`` block matching the model."""
    block = built_in_defaults()["prose"]
    assert block == {
        "level": "standard",
        "clarity_judge": None,
        "block_on_lint": None,
    }
    cfg = ProseConfig.model_validate(block)
    assert cfg.level is ProseLevel.STANDARD


def test_legacy_body_without_prose_normalizes_runtime_id() -> None:
    """A canonical 1.0 body still normalizes the legacy runtime identifier."""
    payload = {
        "schema_version": "1.0",
        "runtime": {"adapters": ["claude"], "preference": ["claude"]},
    }
    upgraded, changed = migrate_config_payload(payload)
    assert changed is True
    assert "prose" not in upgraded
    assert upgraded["runtime"] == {
        "adapters": ["claude-code"],
        "preference": ["claude-code"],
    }
    # The section model still defaults cleanly for such a body.
    assert ProseConfig().level is ProseLevel.STANDARD


# --- leaf catalog -----------------------------------------------------------


def test_leaf_catalog_resolves_every_prose_key() -> None:
    """Each new key resolves via the strict catalog lookup accessor."""
    for key in _PROSE_KEYS:
        entry = leaf_key_lookup(key)
        assert entry.key == key
        assert entry.domain == "prose"
        assert is_known_leaf_key(key) is True


def test_leaf_keys_by_domain_groups_prose() -> None:
    """The domain filter returns exactly the three prose rows."""
    rows = leaf_keys_by_domain("prose")
    assert {row.key for row in rows} == set(_PROSE_KEYS)


def test_leaf_catalog_prose_level_default_is_a_declared_choice() -> None:
    """``prose.level``'s default is one of its own declared choices."""
    entry = LEAF_KEY_REGISTRY["prose.level"]
    assert entry.choices is not None
    assert entry.default in entry.choices


def test_leaf_catalog_prose_level_choices_match_enum() -> None:
    """``prose.level`` choices mirror the canonical ProseLevel value set."""
    entry = leaf_key_lookup("prose.level")
    assert entry.choices == tuple(level.value for level in ProseLevel)


def test_leaf_catalog_prose_level_writable_by_local_layer() -> None:
    """A durable local layer may write the floor; the guard enforces direction."""
    entry = leaf_key_lookup("prose.level")
    assert "local" in entry.writable_layers
    assert "repo" in entry.writable_layers


# --- menu registry (eawf config surface) ------------------------------------


def test_menu_registry_carries_prose_level() -> None:
    """The operator-facing menu registry exposes the ``prose.level`` row."""
    entry = registry_lookup("prose.level")
    assert entry is not None
    assert entry.tab == "prose"
    assert entry.type == "choice"


def test_menu_registry_accepts_valid_prose_level() -> None:
    entry = registry_lookup("prose.level")
    assert entry is not None
    assert coerce_and_validate(entry, "strict") == "strict"


def test_menu_registry_rejects_invalid_prose_level() -> None:
    """A bogus level value is rejected with the canonical InvalidInput error."""
    entry = registry_lookup("prose.level")
    assert entry is not None
    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, "ferocious")
