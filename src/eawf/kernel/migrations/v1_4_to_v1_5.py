"""Concrete ``1.4`` -> ``1.5`` migration step.

The v1.5 schema delta registers
:attr:`~eawf.kernel.state.enums.ArtifactKind.MATH_EXPLAINER`, the artifact
kind for the verification-grounded math-explainer doc-type (typed
:class:`~eawf.kernel.spec.math.MathClaim` / :class:`~eawf.kernel.spec.math.MathExplainer`).

Adding an enum *value* is purely additive: no existing state row references
the new kind (``Artifact.kind`` is a free-form string today, and no historical
artifact carries ``"math_explainer"``), so there is no historical fact to
recover and no row to rewrite. The transform therefore *only* bumps the
``schema_version`` marker ``1.4`` -> ``1.5`` — there is no field backfill, the
step is a lossless round-trip of the on-disk shape, and it is replay-safe.

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the ``v1_2_to_v1_3`` and
``v1_3_to_v1_4`` steps.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)


class StateV14(BaseModel):
    """Lean from-version invariant model -- the v1.4 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.4"]


class StateV15(BaseModel):
    """Lean to-version invariant model -- the v1.5 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.5"]


class MigrationV14ToV15:
    """Migrate a ``state.json`` dict from schema ``1.4`` to ``1.5``."""

    from_version = "1.4"
    to_version = "1.5"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` ``1.4`` -> ``1.5`` (purely additive).

        The v1.5 edge only registers the additive
        :attr:`~eawf.kernel.state.enums.ArtifactKind.MATH_EXPLAINER` enum
        value, which no existing state row references, so the transform
        rewrites only the version marker and leaves every row untouched.

        Args:
            state_dict: Raw v1.4 state dict.

        Returns:
            A deep copy at schema ``1.5`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.5"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.4 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.4
                payload.
        """
        StateV14.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.5 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.5
                payload.
        """
        StateV15.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV14ToV15())


__all__ = ["STEP", "MigrationV14ToV15"]
