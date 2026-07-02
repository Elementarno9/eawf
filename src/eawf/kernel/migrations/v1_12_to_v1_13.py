"""Concrete ``1.12`` -> ``1.13`` migration step.

The v1.13 schema delta adds the optional
:class:`~eawf.kernel.state.models.CriteriaFloorWaiver` record on
:class:`~eawf.kernel.state.models.Wave` (``criteria_floor_waiver``) -- the
typed, visible bypass of the plan-time typed-criteria floor.

Adding an optional field that defaults to ``None`` is purely additive: a
state written before the bump carries no ``criteria_floor_waiver`` key and
re-validates unchanged (a MISSING key is not an UNKNOWN key under
``extra="forbid"``), so there is no historical fact to recover and no row
to rewrite. The transform therefore *only* bumps the ``schema_version``
marker ``1.12`` -> ``1.13`` -- matching the marker-only ``v1_11_to_v1_12``
precedent -- and the step is a lossless, replay-safe round-trip.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV112(BaseModel):
    """Lean from-version invariant model -- the v1.12 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.12"]


class StateV113(BaseModel):
    """Lean to-version invariant model -- the v1.13 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.13"]


class MigrationV112ToV113:
    """Migrate a ``state.json`` dict from schema ``1.12`` to ``1.13``."""

    from_version = "1.12"
    to_version = "1.13"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` ``1.12`` -> ``1.13`` (purely additive).

        The v1.13 edge only adds the optional ``Wave.criteria_floor_waiver``
        field, which defaults to ``None`` and which no pre-existing row
        carries, so the transform rewrites only the version marker and
        leaves every row untouched.

        Args:
            state_dict: Raw v1.12 state dict.

        Returns:
            A deep copy at schema ``1.13`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.13"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.12 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.12
                payload.
        """
        StateV112.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.13 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.13
                payload.
        """
        StateV113.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV112ToV113())


__all__ = ["STEP", "MigrationV112ToV113"]
