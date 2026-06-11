"""Concrete ``1.9`` -> ``1.10`` migration step.

The v1.10 schema delta is a **rename**, not an additive field: the
``Subproject`` entity is renamed to :class:`~eawf.kernel.state.models.Track`,
the top-level ``subprojects`` key to
:attr:`~eawf.kernel.state.models.State.tracks`, the cursor field
``current.subproject_id`` to
:attr:`~eawf.kernel.state.models.CurrentPointers.track_id`, and each phase's
``subproject_id`` link to :attr:`~eawf.kernel.state.models.Phase.track_id`.
Because the live model forbids unknown keys (``extra="forbid"``), an
un-migrated state carrying the old key names rejects on read -- this transform
rewrites every name so a pre-existing state re-validates after migration.

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


class StateV19(BaseModel):
    """Lean from-version invariant model -- the v1.9 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.9"]


class StateV110(BaseModel):
    """Lean to-version invariant model -- the v1.10 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.10"]


def _rename_key(row: object) -> None:
    """Rename ``subproject_id`` -> ``track_id`` in place on a dict *row*.

    A non-dict *row* (e.g. a missing ``current`` block) or a row that
    already carries ``track_id`` is left untouched, keeping the transform
    idempotent and replay-safe.
    """
    if isinstance(row, dict) and "subproject_id" in row and "track_id" not in row:
        row["track_id"] = row.pop("subproject_id")


class MigrationV19ToV110:
    """Migrate a ``state.json`` dict from schema ``1.9`` to ``1.10``."""

    from_version = "1.9"
    to_version = "1.10"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and rename the subproject keys to track.

        Renames the top-level ``subprojects`` key to ``tracks``, the cursor
        field ``current.subproject_id`` to ``current.track_id``, and each
        phase's ``subproject_id`` link to ``track_id``. A key that already
        carries the new name (e.g. a partial replay) is passed through
        untouched, so the step is idempotent and replay-safe.

        Args:
            state_dict: Raw v1.9 state dict.

        Returns:
            A deep copy at schema ``1.10`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.10"

        if "subprojects" in migrated and "tracks" not in migrated:
            migrated["tracks"] = migrated.pop("subprojects")

        _rename_key(migrated.get("current"))

        phases = migrated.get("phases")
        if isinstance(phases, dict):
            for phase in phases.values():
                _rename_key(phase)

        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.9 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.9
                payload.
        """
        StateV19.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.10 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.10
                payload.
        """
        StateV110.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV19ToV110())


__all__ = ["STEP", "MigrationV19ToV110"]
