"""LeafKey model + layer-writability shorthands for the C08 leaf catalog.

This module hosts the :class:`LeafKey` Pydantic record and its narrow
:data:`LeafKeyType` value-shape Literal, plus the ``_WRITABLE_*`` layer
shorthand tuples consumed by every row in
:mod:`eawf.kernel.config.registry.leaf_catalog`. The catalog data table and its
accessors live in that sibling module so the model and the ~1300-row data
block stay in separately-sized files.

Public API:

- :class:`LeafKeyType` — narrow Literal of supported leaf-key value shapes.
- :class:`LeafKey` — frozen Pydantic record (name, declared type, default,
  writable layers, optional description / domain).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

#: Narrow Literal of supported leaf-key value shapes. Mirrors the YAML
#: scalars the loader writes; ``mapping`` covers dict-typed leaves
#: (e.g. ``workspace.repos``, ``profiles.trusted``); ``any`` is the
#: escape hatch for irregular shapes (e.g. ``project.success_metrics``
#: whose value type can vary).
LeafKeyType = Literal[
    "bool",
    "int",
    "float",
    "str",
    "list_str",
    "list_any",
    "mapping",
    "any",
    "literal",
]


class LeafKey(BaseModel):
    """One leaf-key row in the C08 layered-config catalog.

    Attributes:
        key: Dotted config key path (canonical form). Identical to the
            address used by :func:`eawf.kernel.config.layered.get_dotted` and
            the ``key_path`` parameter of the daemon's
            ``config.set_layer_value`` RPC.
        domain: High-level grouping (``daemon``, ``render``, ``runtime``,
            ``scope``, ``config``, ``profiles``, etc.). Mirrors brief
            §5.2's section titles so audits can ``group_by(domain)``.
        type: Value shape — see :data:`LeafKeyType`.
        default: Default value the built-in layer ships with.
        writable_layers: Tuple of layer labels that may write this
            leaf. ``()`` marks a code-only / locked leaf (e.g.
            ``schema_version``, ``language.runtime``).
        description: One-line human-readable summary.
        choices: Allowed values for ``literal``-typed leaves; ``None``
            for other shapes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    domain: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    type: LeafKeyType
    default: Any = None
    writable_layers: tuple[str, ...] = ()
    description: str = ""
    choices: tuple[str, ...] | None = None

    @field_validator("writable_layers")
    @classmethod
    def _validate_layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject layer names outside the canonical taxonomy."""
        allowed = {
            "global",
            "workspace",
            "repo",
            "branch",
            "local",
            "wave",
            "env",
            "cli",
        }
        unknown = [layer for layer in value if layer not in allowed]
        if unknown:
            raise ValueError(f"unknown layer label(s): {unknown!r}")
        return value

    @field_validator("choices")
    @classmethod
    def _validate_choices(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Reject an empty choices tuple — a literal with no options is unreachable."""
        if value is not None and not value:
            raise ValueError("choices must be non-empty when set")
        return value


# Layer shorthands (read by every row below; reduces line-noise).
_WRITABLE_ALL_DURABLE: tuple[str, ...] = (
    "global",
    "workspace",
    "repo",
    "branch",
    "local",
)
_WRITABLE_GWR: tuple[str, ...] = ("global", "workspace", "repo")
_WRITABLE_REPO_ONLY: tuple[str, ...] = ("repo",)
_WRITABLE_PROJECT_GOALS: tuple[str, ...] = ("repo", "branch", "local")
_WRITABLE_RUNTIME_PREFERENCE: tuple[str, ...] = (
    "global",
    "workspace",
    "repo",
    "branch",
    "local",
    "env",
    "cli",
    "wave",
)
_WRITABLE_NONE: tuple[str, ...] = ()  # locked / code-only
