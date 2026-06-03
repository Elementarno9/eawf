"""Concrete ``1.5`` -> ``1.6`` migration step.

The v1.6 schema delta adds the top-level :attr:`~eawf.kernel.state.models.State.dispatch_paused`
flag — the cooperative dispatch-gate marker the ``agent.pause`` / ``agent.resume``
RPCs persist and :func:`eawf.workflow.lifecycle.wave.claim_wave` reads to block a
claim while dispatch is paused.

Adding a field with a model default is purely additive: no existing state row
carries ``dispatch_paused`` (the field is brand new), and the
:class:`~eawf.kernel.state.models.State` model supplies the ``False`` default on
load, so there is no historical fact to recover and no row to rewrite. The
transform therefore *only* bumps the ``schema_version`` marker ``1.5`` -> ``1.6``
— there is no field backfill, the step is a lossless round-trip of the on-disk
shape, and it is replay-safe.

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the ``v1_3_to_v1_4`` and
``v1_4_to_v1_5`` steps.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV15(BaseModel):
    """Lean from-version invariant model -- the v1.5 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.5"]


class StateV16(BaseModel):
    """Lean to-version invariant model -- the v1.6 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.6"]


class MigrationV15ToV16:
    """Migrate a ``state.json`` dict from schema ``1.5`` to ``1.6``."""

    from_version = "1.5"
    to_version = "1.6"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` ``1.5`` -> ``1.6`` (purely additive).

        The v1.6 edge only adds the top-level
        :attr:`~eawf.kernel.state.models.State.dispatch_paused` flag, which
        the model defaults to ``False`` on load, so the transform rewrites
        only the version marker and leaves every row untouched.

        Args:
            state_dict: Raw v1.5 state dict.

        Returns:
            A deep copy at schema ``1.6`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.6"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.5 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.5
                payload.
        """
        StateV15.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.6 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.6
                payload.
        """
        StateV16.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV15ToV16())


__all__ = ["STEP", "MigrationV15ToV16"]
