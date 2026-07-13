"""Concrete ``1.18`` -> ``1.19`` migration step.

The v1.19 schema delta is additive: it adds
:attr:`~eawf.kernel.state.models.ActualSummary.calibration_excluded`, which marks
an actual that was measured but does not calibrate anything.

Two things disqualify a recorded actual as a reference class. A counter reset
re-originated the wave, so the runtime measured before the reset is gone and the
figure is a floor rather than a measure. Or the session was shared by several
concurrent waves, so each wave's figure is a split of one session's counters and
the split is an approximation whenever the concurrency moved mid-span. Both rows
are honest records of what was captured; neither is a reference class.

Without the flag the exclusion has nowhere to live but a document, and a consumer
reading ``state.json`` cannot tell a disqualified row from a clean one.

Rows written before this bump carry no flag, so the step backfills ``False`` --
"nothing known to disqualify it", which is what an unmarked row has always meant.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV118(BaseModel):
    """Lean from-version invariant model -- the v1.18 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.18"]


class StateV119(BaseModel):
    """Lean to-version invariant model -- the v1.19 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.19"]


class MigrationV118ToV119:
    """Migrate a ``state.json`` dict from schema ``1.18`` to ``1.19``."""

    from_version = "1.18"
    to_version = "1.19"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill ``calibration_excluded`` on every actual.

        Args:
            state_dict: Raw v1.18 state dict.

        Returns:
            A deep copy at schema ``1.19`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.19"
        actuals = migrated.get("actuals")
        if isinstance(actuals, dict):
            for actual in actuals.values():
                if isinstance(actual, dict) and "calibration_excluded" not in actual:
                    actual["calibration_excluded"] = False
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.18 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.18 payload.
        """
        StateV118.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.19 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.19 payload.
        """
        StateV119.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV118ToV119())


__all__ = ["STEP", "MigrationV118ToV119"]
