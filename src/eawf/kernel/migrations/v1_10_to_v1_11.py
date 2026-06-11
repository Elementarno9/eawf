"""Concrete ``1.10`` -> ``1.11`` migration step.

The v1.11 schema delta is purely additive: it adds the optional ``harness`` and
``model`` attribution fields to :class:`~eawf.kernel.state.models.ActualSummary`
and :class:`~eawf.kernel.state.models.RuntimeBaseline` (inherited by
:class:`~eawf.kernel.state.models.RuntimeLatest`), so captured EU actuals and
runtime counters become calibratable by harness+model. Both fields default to
``None`` on the model, and this transform materialises the keys with NULL
attribution on every ``actuals`` row and on every wave's ``runtime_baseline`` /
``runtime_latest`` snapshot so persisted state carries the explicit nullable
fields after migration.

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the additive v1.8 -> v1.9 step.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV110(BaseModel):
    """Lean from-version invariant model -- the v1.10 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.10"]


class StateV111(BaseModel):
    """Lean to-version invariant model -- the v1.11 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.11"]


def _backfill_attribution(row: object) -> None:
    """Write explicit ``harness: None`` / ``model: None`` on a dict *row*.

    A non-dict *row* (e.g. a missing snapshot) is left untouched, and a row
    that already carries a key keeps its value, so the transform is idempotent
    and replay-safe.
    """
    if not isinstance(row, dict):
        return
    if "harness" not in row:
        row["harness"] = None
    if "model" not in row:
        row["model"] = None


class MigrationV110ToV111:
    """Migrate a ``state.json`` dict from schema ``1.10`` to ``1.11``."""

    from_version = "1.10"
    to_version = "1.11"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill NULL harness+model attribution.

        Walks every ``actuals`` row and every wave's ``runtime_baseline`` /
        ``runtime_latest`` snapshot, writing an explicit ``harness: None`` and
        ``model: None`` on each row that does not already carry the key. A row
        whose attribution is already present is passed through untouched, so
        the step is idempotent and replay-safe.

        Args:
            state_dict: Raw v1.10 state dict.

        Returns:
            A deep copy at schema ``1.11`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.11"

        actuals = migrated.get("actuals")
        if isinstance(actuals, dict):
            for actual in actuals.values():
                _backfill_attribution(actual)

        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                _backfill_attribution(wave.get("runtime_baseline"))
                _backfill_attribution(wave.get("runtime_latest"))

        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.10 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.10
                payload.
        """
        StateV110.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.11 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.11
                payload.
        """
        StateV111.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV110ToV111())


__all__ = ["STEP", "MigrationV110ToV111"]
