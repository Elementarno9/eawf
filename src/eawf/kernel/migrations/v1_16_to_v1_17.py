"""Concrete ``1.16`` -> ``1.17`` migration step.

The v1.17 schema delta is additive: it adds
:attr:`~eawf.kernel.state.models.RuntimeBaseline.measure_version` (inherited by
:class:`~eawf.kernel.state.models.RuntimeLatest`), recording which definition of
the counters produced a snapshot.

Cumulative counters are comparable only against a baseline taken under the same
definition. When the definition changes, the difference between two snapshots is
not work -- it is the change. That cannot be inferred from the direction the
number moved: a redefinition that lowers the figure looks like a counter reset
and is caught, but one that RAISES it looks exactly like a productive week and is
banked as runtime.

Snapshots written before this bump carry no version, so the step backfills
``None`` -- which the comparison treats as "unknown", falling back to the
direction check rather than assuming a match.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV116(BaseModel):
    """Lean from-version invariant model -- the v1.16 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.16"]


class StateV117(BaseModel):
    """Lean to-version invariant model -- the v1.17 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.17"]


def _backfill_measure_version(snapshot: Any) -> None:
    """Write an explicit ``measure_version: None`` onto a runtime snapshot row."""
    if isinstance(snapshot, dict) and "measure_version" not in snapshot:
        snapshot["measure_version"] = None


class MigrationV116ToV117:
    """Migrate a ``state.json`` dict from schema ``1.16`` to ``1.17``."""

    from_version = "1.16"
    to_version = "1.17"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill ``measure_version`` on every snapshot.

        Args:
            state_dict: Raw v1.16 state dict.

        Returns:
            A deep copy at schema ``1.17`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.17"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                _backfill_measure_version(wave.get("runtime_baseline"))
                _backfill_measure_version(wave.get("runtime_latest"))
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.16 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.16 payload.
        """
        StateV116.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.17 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.17 payload.
        """
        StateV117.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV116ToV117())


__all__ = ["STEP", "MigrationV116ToV117"]
