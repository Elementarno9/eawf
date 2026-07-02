"""Concrete ``1.13`` -> ``1.14`` migration step.

The v1.14 schema delta registers the additive
:attr:`~eawf.kernel.state.enums.StoreKind.JURY_BALLOT` store kind -- the
persisted per-juror ballot store the jury-calibration substrate reads.

Adding an enum value that no existing persisted ``State`` field references
is purely additive: ballots land in the append-only sibling
``jury_ballot.jsonl`` store, not on a top-level ``State`` field, so there
is no historical fact to recover and no row to rewrite. The transform
therefore *only* bumps the ``schema_version`` marker ``1.13`` -> ``1.14``
-- matching the marker-only ``v1_12_to_v1_13`` precedent -- and the step
is a lossless, replay-safe round-trip.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV113(BaseModel):
    """Lean from-version invariant model -- the v1.13 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.13"]


class StateV114(BaseModel):
    """Lean to-version invariant model -- the v1.14 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.14"]


class MigrationV113ToV114:
    """Migrate a ``state.json`` dict from schema ``1.13`` to ``1.14``."""

    from_version = "1.13"
    to_version = "1.14"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` ``1.13`` -> ``1.14`` (purely additive).

        The v1.14 edge only registers the additive ``StoreKind.JURY_BALLOT``
        enum value, which no persisted ``State`` field references, so the
        transform rewrites only the version marker and leaves every row
        untouched.

        Args:
            state_dict: Raw v1.13 state dict.

        Returns:
            A deep copy at schema ``1.14`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.14"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.13 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.13
                payload.
        """
        StateV113.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.14 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.14
                payload.
        """
        StateV114.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV113ToV114())


__all__ = ["STEP", "MigrationV113ToV114"]
