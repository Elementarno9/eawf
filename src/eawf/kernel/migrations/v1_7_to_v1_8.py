"""Concrete ``1.7`` -> ``1.8`` migration step.

The v1.8 schema delta adds the typed :attr:`~eawf.kernel.state.models.Wave.gates`
list -- the per-wave :class:`~eawf.kernel.spec.common.GateSpec` rows the close
gate scores against each wave's success criteria. The field is additive with a
model default of ``[]``, so no existing wave carries a gate list and the
:class:`~eawf.kernel.state.models.Wave` model supplies the empty default on load.

Unlike the additive top-level-flag edges (``v1_5_to_v1_6``), this transform
*does* backfill: it walks every wave and writes an explicit ``gates: []`` when
the key is absent, so the on-disk row materialises the new field rather than
leaning on the load-time default alone. A wave that already carries a ``gates``
list (re-run safety) is passed through untouched, so the step is idempotent and
replay-safe -- an already-1.8 state is a lossless round-trip.

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the ``v1_5_to_v1_6`` and
``v1_6_to_v1_7`` steps.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV17(BaseModel):
    """Lean from-version invariant model -- the v1.7 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.7"]


class StateV18(BaseModel):
    """Lean to-version invariant model -- the v1.8 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.8"]


class MigrationV17ToV18:
    """Migrate a ``state.json`` dict from schema ``1.7`` to ``1.8``."""

    from_version = "1.7"
    to_version = "1.8"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill every wave's ``gates`` list.

        Walks ``waves`` (a flat ``{wave_id: wave_dict}`` map on the State
        model) and writes an explicit ``gates: []`` on each wave that does
        not already carry the key. A wave whose ``gates`` is already present
        (re-run safety, including an already-1.8 state) is left untouched,
        so the step is idempotent and replay-safe.

        Args:
            state_dict: Raw v1.7 state dict.

        Returns:
            A deep copy at schema ``1.8`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.8"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                if "gates" not in wave:
                    wave["gates"] = []
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.7 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.7
                payload.
        """
        StateV17.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.8 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.8
                payload.
        """
        StateV18.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV17ToV18())


__all__ = ["STEP", "MigrationV17ToV18"]
