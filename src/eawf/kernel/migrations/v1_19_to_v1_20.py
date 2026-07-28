"""Pure additive ``1.19`` -> ``1.20`` state migration.

The bridge stores integration, close, and dependency facts in sparse top-level
maps. Migration creates only those empty indexes. It never examines Git,
provider transcripts, Wave rows, or sibling stores, so it cannot fabricate
historical integration generations, close attempts, dependency bindings, or
usage provenance.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)

_SPARSE_MAPS: tuple[str, ...] = (
    "wave_integrations",
    "close_attempts",
    "wave_dependency_barriers",
    "wave_dependency_bindings",
)


class StateV119(BaseModel):
    """Lean from-version invariant model."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.19"]


class StateV120(BaseModel):
    """Lean to-version invariant model."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.20"]


class MigrationV119ToV120:
    """Add empty operational indexes and advance the schema marker."""

    from_version = "1.19"
    to_version = "1.20"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Return a deep-copied v1.20 payload with sparse indexes present."""
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = self.to_version
        for field_name in _SPARSE_MAPS:
            migrated.setdefault(field_name, {})
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate that input carries the v1.19 marker."""
        StateV119.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate that output carries the v1.20 marker."""
        StateV120.model_validate(state_dict)


STEP = _register(MigrationV119ToV120())


__all__ = ["STEP", "MigrationV119ToV120"]
