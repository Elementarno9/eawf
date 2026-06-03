"""Concrete ``1.3`` -> ``1.4`` migration step.

The v1.4 schema delta adds
:attr:`~eawf.kernel.state.models.Iter.candidate_tag`, an optional
``vMAJOR.MINOR.PATCH`` release tag an operator pencils onto an iter
before the phase-close release pre-flight pins the real version. The
field is strictly optional, so the migration is purely additive: there
is no historical fact to recover, and an iter without a proposed tag is
exactly the unset default the v1.4 model loads as ``None``.

The transform operates on the **raw** state dict and only ever *adds*
the ``candidate_tag`` key to an iter row that lacks it (idempotent
``setdefault`` to ``None``), so a re-run -- or an iter a later
lifecycle writer already tagged -- is left untouched. The step is a
lossless round-trip of the on-disk shape and is replay-safe. The
pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the ``v1_1_to_v1_2`` and
``v1_2_to_v1_3`` steps.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)

#: Value written onto every historical iter that lacks the key. ``None``
#: is the v1.4 model default -- the iter simply carries no proposed
#: release tag until an operator sets one through the lifecycle surface.
_BACKFILL_CANDIDATE_TAG = None


class StateV13(BaseModel):
    """Lean from-version invariant model -- the v1.3 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.3"]


class StateV14(BaseModel):
    """Lean to-version invariant model -- the v1.4 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.4"]


def _backfill_iter_candidate_tag(row: dict[str, Any]) -> None:
    """Add ``row['candidate_tag'] = None`` when the iter row lacks the key.

    No-op when ``candidate_tag`` is already present, so a re-run (or an
    iter a lifecycle writer already tagged) is left untouched -- the step
    stays idempotent and never overwrites an operator-set tag.
    """
    row.setdefault("candidate_tag", _BACKFILL_CANDIDATE_TAG)


class MigrationV13ToV14:
    """Migrate a ``state.json`` dict from schema ``1.3`` to ``1.4``."""

    from_version = "1.3"
    to_version = "1.4"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Backfill ``Iter.candidate_tag`` then bump ``schema_version``.

        Adds ``candidate_tag=None`` to every iter row that lacks it
        (the v1.4 default) and rewrites ``schema_version`` ``1.3`` ->
        ``1.4``.

        Args:
            state_dict: Raw v1.3 state dict.

        Returns:
            A deep copy at schema ``1.4`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)

        for row in self._iter_rows(migrated, "iters"):
            _backfill_iter_candidate_tag(row)

        migrated["schema_version"] = "1.4"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    @staticmethod
    def _iter_rows(state_dict: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Return the dict-valued rows under ``state_dict[key]``.

        Returns an empty list when the sub-dict is absent or ``None``.
        Non-dict rows are skipped so a malformed payload cannot crash the
        iteration.
        """
        section = state_dict.get(key)
        if not isinstance(section, dict):
            return []
        return [row for row in section.values() if isinstance(row, dict)]

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.3 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.3
                payload.
        """
        StateV13.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.4 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.4
                payload.
        """
        StateV14.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV13ToV14())


__all__ = ["STEP", "MigrationV13ToV14"]
