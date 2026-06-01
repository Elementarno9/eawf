"""Concrete ``1.1`` -> ``1.2`` migration step.

The v1.2 schema delta adds :class:`~eawf.kernel.state.enums.IterTrigger` and the
``Iter.trigger`` field that classifies *why* an iter was opened, so the
planned-vs-reactive metric can split waves by intent instead of by the
``I##`` id suffix.

Migration note — reactive-share definitional artifact
-----------------------------------------------------
The prior reactive-wave figure (the ~54% reactive share that circulated
before this wave) was a **definitional artifact**: the metric classified
every wave under an ``I02+`` iter as reactive purely from the id suffix.
That heuristic conflated genuine repair / mid-flight scope-add work with
deliberate *planned* scope expansions (which also open an ``I02+`` iter)
and with pure bookkeeping iters (which should not skew the ratio at all).
Counting all three as reactive inflated the share.

v1.2 corrects the *definition*: the denominator is now driven by the
typed ``Iter.trigger`` reason, and iters whose ``trigger`` is
:attr:`~eawf.kernel.state.enums.IterTrigger.NONE` drop out of the
denominator entirely. Because a historical iter's true reason is not
recoverable from the stored shape alone, this migration backfills every
existing iter to ``trigger="none"`` — the conservative, artifact-retiring
choice the wave spec blesses ("historical iters are backfilled or left
``none``, and thus excluded"). The live metric therefore reports ``n/a``
for the historical corpus rather than re-asserting the inflated 54%;
iters opened after the lifecycle surface wires the real value carry an
explicit ``reactive`` / ``proactive`` trigger and re-populate the ratio.

The transform operates on the **raw** state dict and only ever *adds* the
``trigger`` key to an iter row that lacks it, so it never clobbers a value
a later hand-edit or a future lifecycle writer set — keeping the step
idempotent and a lossless round-trip of the on-disk shape. The pre/post
invariants Pydantic-load against lean fixture models that read only the
``schema_version`` marker, matching the ``v1_0_to_v1_1`` step.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register
from eawf.kernel.state.enums import IterTrigger

logger = logging.getLogger(__name__)

#: Trigger written onto every historical iter that lacks the key. ``none``
#: drops the iter out of the planned-vs-reactive denominator, retiring the
#: inflated reactive-share artifact rather than re-deriving it from the
#: id-suffix heuristic the v1.1 model relied on.
_BACKFILL_TRIGGER = IterTrigger.NONE.value


class StateV11(BaseModel):
    """Lean from-version invariant model — the v1.1 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.1"]


class StateV12(BaseModel):
    """Lean to-version invariant model — the v1.2 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.2"]


def _backfill_iter_trigger(row: dict[str, Any]) -> None:
    """Add ``row['trigger'] = "none"`` when the iter row lacks the key.

    No-op when ``trigger`` is already present, so a re-run (or a state a
    future lifecycle writer already tagged) is left untouched — the step
    stays idempotent and never overwrites an operator-set reason.
    """
    row.setdefault("trigger", _BACKFILL_TRIGGER)


class MigrationV11ToV12:
    """Migrate a ``state.json`` dict from schema ``1.1`` to ``1.2``."""

    from_version = "1.1"
    to_version = "1.2"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Backfill ``Iter.trigger`` then bump ``schema_version``.

        Adds ``trigger="none"`` to every iter row that lacks it (excluding
        the iter from the corrected planned-vs-reactive denominator) and
        rewrites ``schema_version`` ``1.1`` -> ``1.2``.

        Args:
            state_dict: Raw v1.1 state dict.

        Returns:
            A deep copy at schema ``1.2`` — the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)

        for row in self._iter_rows(migrated, "iters"):
            _backfill_iter_trigger(row)

        migrated["schema_version"] = "1.2"
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
        """Validate the input carries the v1.1 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.1
                payload.
        """
        StateV11.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.2 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.2
                payload.
        """
        StateV12.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV11ToV12())


__all__ = ["STEP", "MigrationV11ToV12"]
