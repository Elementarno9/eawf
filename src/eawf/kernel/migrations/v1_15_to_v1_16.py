"""Concrete ``1.15`` -> ``1.16`` migration step.

The v1.16 schema delta is additive: it adds
:attr:`~eawf.kernel.state.models.RuntimeCarry.counter_resets`, which counts the
times a wave's counter source reset under it -- a truncated transcript, or a
change to what the duration measures.

A reset drops the runtime measured before it (the old figures cannot be
re-derived under the new source), so the count is the recorded REASON a wave may
close with less runtime than it really spent. Without it, an honest reset is
indistinguishable from a capture path that silently did nothing, and the close
gate has to choose between refusing every reset wave and trusting every zero.

Existing carries predate the counter and cannot have observed a reset, so the
step backfills ``0``.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV115(BaseModel):
    """Lean from-version invariant model -- the v1.15 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.15"]


class StateV116(BaseModel):
    """Lean to-version invariant model -- the v1.16 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.16"]


class MigrationV115ToV116:
    """Migrate a ``state.json`` dict from schema ``1.15`` to ``1.16``."""

    from_version = "1.15"
    to_version = "1.16"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill ``RuntimeCarry.counter_resets``.

        Walks every wave that carries a ``runtime_carry`` and writes an explicit
        ``counter_resets: 0`` onto it. A carry that already carries the field is
        passed through untouched, so the step is idempotent.

        Args:
            state_dict: Raw v1.15 state dict.

        Returns:
            A deep copy at schema ``1.16`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.16"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                carry = wave.get("runtime_carry")
                if isinstance(carry, dict) and "counter_resets" not in carry:
                    carry["counter_resets"] = 0
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.15 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.15 payload.
        """
        StateV115.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.16 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.16 payload.
        """
        StateV116.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV115ToV116())


__all__ = ["STEP", "MigrationV115ToV116"]
