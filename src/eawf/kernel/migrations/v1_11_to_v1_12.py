"""Concrete ``1.11`` -> ``1.12`` migration step.

The v1.12 schema delta registers the
:class:`~eawf.kernel.state.enums.UserDecisionKind` enum and the
:class:`~eawf.kernel.state.models.PauseResolution` typed record -- the shared
operator-decision shape spanning the ``needs_user`` pause surface and the
fleet-fork surface.

Adding an enum + a typed record that no existing persisted ``State`` field
references is purely additive: pause / fork resolutions live in the
append-only event + evidence stores, not on a top-level ``State`` field, so
there is no historical fact to recover and no row to rewrite. The transform
therefore *only* bumps the ``schema_version`` marker ``1.11`` -> ``1.12`` --
there is no field backfill, the step is a lossless round-trip of the on-disk
shape, and it is replay-safe.

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the additive ``v1_4_to_v1_5`` and
``v1_10_to_v1_11`` steps.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV111(BaseModel):
    """Lean from-version invariant model -- the v1.11 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.11"]


class StateV112(BaseModel):
    """Lean to-version invariant model -- the v1.12 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.12"]


class MigrationV111ToV112:
    """Migrate a ``state.json`` dict from schema ``1.11`` to ``1.12``."""

    from_version = "1.11"
    to_version = "1.12"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` ``1.11`` -> ``1.12`` (purely additive).

        The v1.12 edge only registers the additive
        :class:`~eawf.kernel.state.enums.UserDecisionKind` enum and the
        :class:`~eawf.kernel.state.models.PauseResolution` typed record, neither
        of which any existing persisted ``State`` field references, so the
        transform rewrites only the version marker and leaves every row
        untouched.

        Args:
            state_dict: Raw v1.11 state dict.

        Returns:
            A deep copy at schema ``1.12`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.12"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.11 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.11
                payload.
        """
        StateV111.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.12 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.12
                payload.
        """
        StateV112.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV111ToV112())


__all__ = ["STEP", "MigrationV111ToV112"]
