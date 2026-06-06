"""Concrete ``1.6`` -> ``1.7`` migration step.

The v1.7 schema delta retypes :attr:`~eawf.kernel.state.models.Wave.success_criteria`
from a free-form ``list[str]`` into ``list[CriterionSpec]`` -- the typed
success-criterion row the v0.4 spec layer defines in
:mod:`eawf.kernel.spec.common`. Unlike the prior additive edges, this is a
real backfill: every legacy criterion string is wrapped into a grandfathered
:class:`~eawf.kernel.spec.common.CriterionSpec` dict so the retyped field
validates after the bump. An un-migrated state with bare strings would reject
the typed field, so the transform rewrites every wave's criteria in place.

Each legacy string ``s`` (1-based index ``n`` within its wave) becomes::

    {
        "id": "CR-0n",
        "text": s,
        "kind": "legacy",
        "acceptance_style": "binary",
        "evidence_kind": "attested",
        "quality_dimension": "functional_suitability",
        "measurable_signal": s[:300] if len(s) >= 20 else "grandfathered legacy criterion",
    }

An empty ``success_criteria`` list migrates to ``[]``. The backfill is a
deterministic pure function of the input rows, so the step is replay-safe and
a lossless wrap (the original string is preserved verbatim in ``text``).

The pre/post invariants Pydantic-load against lean fixture models that read
only the ``schema_version`` marker, matching the ``v1_4_to_v1_5`` and
``v1_5_to_v1_6`` steps.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)

#: Sentinel ``kind`` for a grandfathered criterion (mirrors
#: :data:`eawf.kernel.spec.common.GRANDFATHERED_KIND`). Inlined rather than
#: imported so the migration module stays independent of the live spec model
#: -- the migration writes a raw dict that the model validates afterward.
_GRANDFATHERED_KIND = "legacy"

#: Fallback ``measurable_signal`` for a legacy string under the 20-char floor
#: (mirrors :data:`eawf.kernel.spec.common.GRANDFATHERED_SIGNAL`).
_GRANDFATHERED_SIGNAL = "grandfathered legacy criterion"

#: Floor the :class:`~eawf.kernel.spec.common.CriterionSpec.measurable_signal`
#: bound enforces; legacy strings shorter than this get the fallback signal.
_SIGNAL_MIN = 20

#: Cap the ``measurable_signal`` bound enforces; legacy strings are truncated
#: to this width when they already clear the floor.
_SIGNAL_MAX = 300


class StateV16(BaseModel):
    """Lean from-version invariant model -- the v1.6 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.6"]


class StateV17(BaseModel):
    """Lean to-version invariant model -- the v1.7 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.7"]


def _grandfather_criterion(text: str, *, index: int) -> dict[str, Any]:
    """Wrap a legacy criterion string into a grandfathered CriterionSpec dict.

    Mirrors :func:`eawf.kernel.spec.common.grandfather_criterion` at the raw
    dict layer so the migrated row validates against the now-1.7 model.

    Args:
        text: The legacy success-criterion string.
        index: 1-based position of the criterion within its wave.

    Returns:
        A dict shaped like a :class:`~eawf.kernel.spec.common.CriterionSpec`.
    """
    measurable_signal = text[:_SIGNAL_MAX] if len(text) >= _SIGNAL_MIN else _GRANDFATHERED_SIGNAL
    return {
        "id": f"CR-{index:02d}",
        "text": text,
        "kind": _GRANDFATHERED_KIND,
        "acceptance_style": "binary",
        "evidence_kind": "attested",
        "quality_dimension": "functional_suitability",
        "measurable_signal": measurable_signal,
    }


class MigrationV16ToV17:
    """Migrate a ``state.json`` dict from schema ``1.6`` to ``1.7``."""

    from_version = "1.6"
    to_version = "1.7"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Bump ``schema_version`` and backfill every wave's criteria.

        Walks ``waves`` (a flat ``{wave_id: wave_dict}`` map on the State
        model) and rewrites each wave's ``success_criteria`` legacy-string
        list into a list of grandfathered CriterionSpec dicts. A
        non-list or absent ``success_criteria`` is normalised to ``[]``;
        an entry that is already a dict (re-run safety) is passed through
        untouched so the step is idempotent on an already-migrated row.

        Args:
            state_dict: Raw v1.6 state dict.

        Returns:
            A deep copy at schema ``1.7`` -- the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)
        migrated["schema_version"] = "1.7"
        waves = migrated.get("waves")
        if isinstance(waves, dict):
            for wave in waves.values():
                if not isinstance(wave, dict):
                    continue
                criteria = wave.get("success_criteria")
                if not isinstance(criteria, list):
                    wave["success_criteria"] = []
                    continue
                rebuilt: list[dict[str, Any]] = []
                for index, entry in enumerate(criteria, start=1):
                    if isinstance(entry, dict):
                        # Already typed (idempotent re-run) -- keep as-is.
                        rebuilt.append(entry)
                    else:
                        rebuilt.append(_grandfather_criterion(str(entry), index=index))
                wave["success_criteria"] = rebuilt
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.6 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.6
                payload.
        """
        StateV16.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.7 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.7
                payload.
        """
        StateV17.model_validate(state_dict)


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV16ToV17())


__all__ = ["STEP", "MigrationV16ToV17"]
