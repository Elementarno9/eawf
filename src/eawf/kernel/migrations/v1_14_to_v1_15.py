"""Concrete ``1.14`` -> ``1.15`` migration step.

The v1.15 schema delta is additive and makes runtime capture session-aware:

- :attr:`~eawf.kernel.state.models.RuntimeBaseline.session_id` (inherited by
  :class:`~eawf.kernel.state.models.RuntimeLatest`) records which session's
  cumulative counters a snapshot was read from; and
- :attr:`~eawf.kernel.state.models.Wave.runtime_carry` accumulates the runtime
  a wave already spent in sessions that have since ended.

Together they let a wave claimed in one session and closed in another sum its
per-session runtimes rather than differencing counters taken against two
different origins. Neither field can be recovered for a wave captured before the
bump, so the step backfills both explicitly (``session_id: null``,
``runtime_carry: null``) -- the honest "unknown", not a fabricated origin.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV114(BaseModel):
    """Lean from-version invariant model -- the v1.14 pre-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.14"]


class StateV115(BaseModel):
    """Lean to-version invariant model -- the v1.15 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.15"]


def _backfill_session_id(snapshot: Any) -> None:
    """Write an explicit ``session_id: None`` onto a runtime snapshot row."""
    if isinstance(snapshot, dict) and "session_id" not in snapshot:
        snapshot["session_id"] = None


class MigrationV114ToV115:
    """Migrate a ``state.json`` dict from schema ``1.14`` to ``1.15``."""

    from_version = "1.14"
    to_version = "1.15"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill the session-aware runtime fields.

        Walks every wave, writing an explicit ``session_id: None`` on its
        ``runtime_baseline`` / ``runtime_latest`` snapshots and an explicit
        ``runtime_carry: None`` on the wave itself. A row that already carries
        the field is passed through untouched, so the step is idempotent.

        Args:
            state_dict: Raw v1.14 state dict.

        Returns:
            A deep copy at schema ``1.15`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.15"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                _backfill_session_id(wave.get("runtime_baseline"))
                _backfill_session_id(wave.get("runtime_latest"))
                if "runtime_carry" not in wave:
                    wave["runtime_carry"] = None
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.14 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.14 payload.
        """
        StateV114.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.15 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.15 payload.
        """
        StateV115.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV114ToV115())


__all__ = ["STEP", "MigrationV114ToV115"]
