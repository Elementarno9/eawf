"""Layered configuration subsystem for eawf.

Configuration is layered with the following precedence (lowest to
highest, later overrides earlier):

1. built-in defaults (read-only, baked into the package)
2. global ``~/.config/eawf/config.yaml``
3. workspace ``<workspace>/.ea/config.yaml``
4. repo ``<repo>/.ea/config.yaml``
5. local ``<repo>/.ea/local/config.yaml``
6. environment variables (``EAWF_*``)
7. CLI overrides

Required top-level sections of the merged config follow
``docs/architecture/envelope.md`` "Config schema required sections".

Public API:

- :func:`eawf.kernel.config.layered.merge_config` returns ``(merged, source_map)``.
- :func:`eawf.kernel.config.profile.enable_profile` writes a profile to a layer file
  and materialises any required state-keys.
- :data:`eawf.kernel.config.defaults.BUILT_IN_DEFAULTS` is the read-only base layer.
- :func:`eawf.kernel.config.loader.load_yaml_layer` parses a single layer file.
"""

from __future__ import annotations
