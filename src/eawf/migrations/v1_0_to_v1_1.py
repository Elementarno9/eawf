"""Concrete ``1.0`` -> ``1.1`` migration step.

This first edge is intentionally a pure version bump: it rewrites
``schema_version`` ``"1.0"`` -> ``"1.1"`` and is otherwise an identity
transform. The v1.1 field deltas (title-cap tightening, Decision /
Hypothesis field renames) land in a later wave that EXTENDS this same
step; keeping this edge field-free first lets the model's
``schema_version`` ``Literal`` advance to accept ``"1.1"`` without any
on-disk state losing or gaining a field.

The step operates on the **raw** state dict and never re-validates the
input against the full :class:`eawf.state.models.State` model. Later
waves add breaking field changes here, so the transform must not assume
the input loads against the current model. The pre/post invariants
therefore Pydantic-load against lean fixture models defined here
(:class:`StateV10` / :class:`StateV11`) that read only the
``schema_version`` marker — enough to fail fast on a malformed input
(MIG-F4) or a bad transform (MIG-F5) without coupling to the full state
schema.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV10(BaseModel):
    """Lean from-version invariant model — the v1.0 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.0"]


class StateV11(BaseModel):
    """Lean to-version invariant model — the v1.1 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.1"]


class MigrationV10ToV11:
    """Migrate a ``state.json`` dict from schema ``1.0`` to ``1.1``."""

    from_version = "1.0"
    to_version = "1.1"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` ``1.0`` -> ``1.1`` (identity otherwise).

        Args:
            state_dict: Raw v1.0 state dict.

        Returns:
            A deep copy at schema ``1.1`` — the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.1"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.0 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.0
                payload (MIG-F4).
        """
        StateV10.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.1 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.1
                payload (MIG-F5).
        """
        StateV11.model_validate(state_dict)


#: Registered into :data:`eawf.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV10ToV11())


__all__ = ["STEP", "MigrationV10ToV11"]
