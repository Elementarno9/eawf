"""Concrete ``1.8`` -> ``1.9`` migration step.

The v1.9 schema delta adds
:attr:`~eawf.kernel.state.models.Wave.runtime_baseline`, the optional
claim-time snapshot of cumulative runtime counters. The field defaults to
``None`` on the model, and this transform materialises the key on every wave
row so persisted state carries the explicit nullable field after migration.

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the additive v1.7 -> v1.8 step.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV18(BaseModel):
    """Lean from-version invariant model -- the v1.8 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.8"]


class StateV19(BaseModel):
    """Lean to-version invariant model -- the v1.9 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.9"]


class MigrationV18ToV19:
    """Migrate a ``state.json`` dict from schema ``1.8`` to ``1.9``."""

    from_version = "1.8"
    to_version = "1.9"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill ``Wave.runtime_baseline``.

        Walks ``waves`` and writes an explicit ``runtime_baseline: None`` on
        each wave that does not already carry the key. A wave whose
        ``runtime_baseline`` is already present is passed through untouched,
        so the step is idempotent and replay-safe.

        Args:
            state_dict: Raw v1.8 state dict.

        Returns:
            A deep copy at schema ``1.9`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.9"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                if "runtime_baseline" not in wave:
                    wave["runtime_baseline"] = None
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.8 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.8
                payload.
        """
        StateV18.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.9 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.9
                payload.
        """
        StateV19.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV18ToV19())


__all__ = ["STEP", "MigrationV18ToV19"]
