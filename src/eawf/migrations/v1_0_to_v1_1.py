"""Concrete ``1.0`` -> ``1.1`` migration step.

The v1.1 schema delta (C10 §5.5 [17:52-58]) is small + additive:

* bump ``schema_version`` ``"1.0"`` -> ``"1.1"``;
* add a ``principal_id`` root field defaulting to ``"operator:local"``
  (the canonical-naming convention is ``scope_id`` / ``principal_id`` —
  the bare-``scope`` outlier is renamed, never shimmed);
* rename any embedded ``scope`` key to ``scope_id`` so the wire form
  matches the canonical naming convention.

The live :class:`eawf.state.models.State` model is pinned at
``schema_version="1.0"``, so this step cannot validate against it. The
pre/post invariants therefore Pydantic-load against lean fixture models
defined here (:class:`StateV10` / :class:`StateV11`) that mirror the
delta — enough surface to fail fast on a malformed input (MIG-F4) or a
bad transform (MIG-F5) without coupling to the full state schema.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.migrations._base import _register

logger = logging.getLogger(__name__)

#: Default principal id stamped onto a v1.0 state that predates the field.
_DEFAULT_PRINCIPAL_ID = "operator:local"


class StateV10(BaseModel):
    """Lean from-version invariant model — the v1.0 pre-condition.

    Mirrors only the surface the step touches; ``extra="ignore"`` lets
    the rest of the real state payload pass through unread so the
    pre-check stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.0"]


class StateV11(BaseModel):
    """Lean to-version invariant model — the v1.1 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.1"]
    principal_id: str


def _rename_scope_to_scope_id(node: Any) -> Any:
    """Recursively rename bare ``scope`` keys to ``scope_id``.

    Walks dicts + lists; on a dict carrying ``scope`` (but not already
    ``scope_id``) the key is renamed in place. Canonical-naming
    convention: ``scope_id`` is the one true field name; the bare
    ``scope`` outlier is renamed, never adapter-shimmed.
    """
    if isinstance(node, dict):
        renamed = {k: _rename_scope_to_scope_id(v) for k, v in node.items()}
        if "scope" in renamed and "scope_id" not in renamed:
            renamed["scope_id"] = renamed.pop("scope")
        return renamed
    if isinstance(node, list):
        return [_rename_scope_to_scope_id(item) for item in node]
    return node


class MigrationV10ToV11:
    """Migrate a ``state.json`` dict from schema ``1.0`` to ``1.1``."""

    from_version = "1.0"
    to_version = "1.1"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump the version, add ``principal_id``, rename ``scope`` keys.

        Args:
            state_dict: Raw v1.0 state dict.

        Returns:
            A new dict at schema ``1.1`` — the input is not mutated.
        """
        migrated = _rename_scope_to_scope_id(state_dict)
        assert isinstance(migrated, dict)
        migrated["schema_version"] = "1.1"
        migrated.setdefault("principal_id", _DEFAULT_PRINCIPAL_ID)
        logger.info(f"apply from={self.from_version} to={self.to_version} added=principal_id")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input loads against the v1.0 invariant model.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a valid
                v1.0 payload (MIG-F4).
        """
        StateV10.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output loads against the v1.1 invariant model.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a valid
                v1.1 payload (MIG-F5).
        """
        StateV11.model_validate(state_dict)


#: Registered into :data:`eawf.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV10ToV11())


__all__ = ["STEP", "MigrationV10ToV11"]
