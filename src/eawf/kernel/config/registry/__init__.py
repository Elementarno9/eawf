"""Typed metadata registry for ``eawf config`` plus the C08 leaf-key catalog.

This package was split out of the former single-file
``eawf/config/registry.py`` (P27-I05-W08) and re-exports the full public
API so every ``from eawf.kernel.config.registry import X`` import keeps working
unchanged. The split is purely structural — responsibility-scoped
submodules behind a stable facade:

- :mod:`~eawf.kernel.config.registry.config_keys` — :class:`ConfigKey`,
  :data:`CONFIG_REGISTRY`, and the menu-surface accessors.
- :mod:`~eawf.kernel.config.registry.coercion` — raw-answer coercion +
  validation (:func:`coerce_and_validate`, :func:`is_known_key`).
- :mod:`~eawf.kernel.config.registry.leaf_keys` — the :class:`LeafKey` model +
  layer-writability shorthands.
- :mod:`~eawf.kernel.config.registry.leaf_catalog` — the ~150-row leaf-key data
  table (:data:`LEAF_KEY_REGISTRY`) and its lookup accessors.

The two registries are related but distinct:

1. :data:`CONFIG_REGISTRY` — the operator-tunable subset surfaced by the
   interactive ``eawf config`` menu (P20-W10) and the TUI config hotkey
   (P20-W11). One :class:`ConfigKey` row per menu entry.
2. :data:`LEAF_KEY_REGISTRY` — the full ~150-key catalog (P25-W14 / C08
   §5.2) covering every leaf in the layered config. The catalog is what
   the daemon uses to reject ``unknown config key: <key!r>`` writes; the
   menu never iterates the full set.
"""

from __future__ import annotations

from eawf.kernel.config.registry.coercion import coerce_and_validate, is_known_key
from eawf.kernel.config.registry.config_keys import (
    CONFIG_REGISTRY,
    ConfigKey,
    ConfigKeyType,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)
from eawf.kernel.config.registry.leaf_catalog import (
    LEAF_KEY_REGISTRY,
    is_known_leaf_key,
    leaf_key_lookup,
    leaf_keys_by_domain,
)
from eawf.kernel.config.registry.leaf_keys import LeafKey, LeafKeyType

__all__ = [
    "CONFIG_REGISTRY",
    "LEAF_KEY_REGISTRY",
    "ConfigKey",
    "ConfigKeyType",
    "LeafKey",
    "LeafKeyType",
    "coerce_and_validate",
    "is_known_key",
    "is_known_leaf_key",
    "keys_for_tab",
    "leaf_key_lookup",
    "leaf_keys_by_domain",
    "registry_lookup",
    "tabs_sorted",
]
