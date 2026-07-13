"""Concrete ``1.17`` -> ``1.18`` migration step.

The v1.18 schema delta is additive: it adds
:attr:`~eawf.kernel.state.models.RuntimeBaseline.shared_wave_count` (inherited by
:class:`~eawf.kernel.state.models.RuntimeLatest`), recording how many waves were
active when a snapshot was captured.

One ``runtime.capture`` writes the same session snapshot to every active wave,
and each wave then differences the whole session -- so N concurrent waves each
record the SAME runtime and the session is counted N times over. The count is the
divisor the close-time delta applies, which turns one session's runtime into a
split among its sharers rather than a copy handed to each.

Snapshots written before this bump carry no count, so the step backfills ``None``
-- which the delta reads as a divisor of one, the value it effectively used
before the field existed.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV117(BaseModel):
    """Lean from-version invariant model -- the v1.17 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.17"]


class StateV118(BaseModel):
    """Lean to-version invariant model -- the v1.18 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.18"]


def _backfill_shared_wave_count(snapshot: Any) -> None:
    """Write an explicit ``shared_wave_count: None`` onto a runtime snapshot row."""
    if isinstance(snapshot, dict) and "shared_wave_count" not in snapshot:
        snapshot["shared_wave_count"] = None


class MigrationV117ToV118:
    """Migrate a ``state.json`` dict from schema ``1.17`` to ``1.18``."""

    from_version = "1.17"
    to_version = "1.18"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill ``shared_wave_count`` on every snapshot.

        Args:
            state_dict: Raw v1.17 state dict.

        Returns:
            A deep copy at schema ``1.18`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.18"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                _backfill_shared_wave_count(wave.get("runtime_baseline"))
                _backfill_shared_wave_count(wave.get("runtime_latest"))
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.17 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.17 payload.
        """
        StateV117.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.18 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.18 payload.
        """
        StateV118.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV117ToV118())


__all__ = ["STEP", "MigrationV117ToV118"]
