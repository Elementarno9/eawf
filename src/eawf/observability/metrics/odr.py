"""Oracle-Determinism-Ratio (ODR) metric and the escape-ledger primitive.

The ODR answers one question over a wave's typed success criteria: of
the criteria that actually gate the close, what fraction are falsified
by a deterministic oracle (tiers T1..T5) rather than a judgment oracle
(T6 approval / T7 jury)? A high ratio means the wave's verdict rests on
cheap reproducible checks; a low ratio means it leans on expensive,
non-reproducible judgment.

Two grandfathering / boundary rules make the metric total:

* A criterion whose ``oracle_tier`` is ``None`` (a grandfathered legacy
  row that was never assigned a tier) is NOT counted as deterministic.
  It does not enter the numerator, so an un-tiered required criterion
  drags the ratio DOWN -- the conservative reading, since an un-tiered
  criterion is exactly the kind of under-determined gate the metric
  exists to surface.
* When no criterion is ``required`` (an empty set, or a set of
  optional-only criteria) the denominator is zero. The ratio is then
  defined as ``1.0`` (vacuously fully-deterministic): a scope with no
  required gates has nothing under-determined to flag, so it never trips
  the advisory floor.

The escape ledger is a separate, minimal primitive: tag a finding with
the stage it was *caught* at (``close`` / ``review`` / ``production``)
and :func:`escape_rate` returns the fraction caught after the close
gate. It is a pure function over typed :class:`EscapeFinding` rows; no
live wiring into the risk-weighting verdict loop is built here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from eawf.kernel.spec.common import CriterionSpec, OracleTier

logger = logging.getLogger(__name__)

#: Oracle tiers that count as deterministic for the ODR numerator. T1..T5
#: are reproducible automated falsifiers (static / structural / snapshot /
#: contract / golden); T6 (approval) and T7 (jury) are judgment oracles
#: and are excluded.
_DETERMINISTIC_TIERS: frozenset[OracleTier] = frozenset(
    {
        OracleTier.T1_STATIC,
        OracleTier.T2_STRUCTURAL,
        OracleTier.T3_SNAPSHOT,
        OracleTier.T4_CONTRACT,
        OracleTier.T5_GOLDEN,
    }
)

#: Returned by :func:`oracle_determinism_ratio` when no criterion is
#: ``required`` (zero denominator). A scope with no required gates is
#: vacuously fully-deterministic, so it never trips the advisory floor.
EMPTY_RATIO = 1.0

#: Default advisory ODR floor mirrored by
#: :attr:`eawf.platform.profiles.models.VerifyBlock.odr_floor`. Kept here
#: so a caller that has no profile in hand can still pass a sensible
#: default to :func:`odr_below_floor`.
DEFAULT_ODR_FLOOR = 0.80


class EscapeStage(StrEnum):
    """The lifecycle stage at which a finding was caught.

    Ordered by how far the defect travelled before detection: ``close``
    (caught by the wave-close gate, the cheapest place), ``review``
    (caught by the phase PR review, one stage later), ``production``
    (escaped both -- the most expensive). The escape rate is the
    fraction caught at ``review`` or ``production``.
    """

    CLOSE = "close"
    REVIEW = "review"
    PRODUCTION = "production"


#: Stages that count as an escape (caught after the close gate fired).
_ESCAPED_STAGES: frozenset[EscapeStage] = frozenset({EscapeStage.REVIEW, EscapeStage.PRODUCTION})


class EscapeFinding(BaseModel):
    """One tagged finding row for the escape ledger.

    The minimal escape-ledger primitive: a finding id plus the
    :class:`EscapeStage` it was caught at. The model is strict and
    frozen so an unknown ``caught_at`` value fails at the ingestion
    boundary with :class:`pydantic.ValidationError` rather than silently
    skewing the escape rate.

    Attributes:
        finding_id: Stable id of the finding (free-form; the ledger does
            not enforce a pattern).
        caught_at: The :class:`EscapeStage` the finding was detected at.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str
    caught_at: EscapeStage


def oracle_determinism_ratio(criteria: list[CriterionSpec]) -> float:
    """Return the fraction of required criteria gated by a deterministic oracle.

    Numerator: required criteria whose ``oracle_tier`` is in T1..T5 (a
    ``None`` tier does NOT count -- see the module docstring). Denominator:
    the count of ``required`` criteria. When the denominator is zero the
    ratio is :data:`EMPTY_RATIO` (``1.0``), never a ``ZeroDivisionError``.

    Args:
        criteria: The wave's typed criterion rows. Optional
            (``required=False``) criteria are ignored by both the
            numerator and the denominator.

    Returns:
        The ODR in ``[0.0, 1.0]``; :data:`EMPTY_RATIO` when no criterion
        is required.
    """
    required = [c for c in criteria if c.required]
    if not required:
        return EMPTY_RATIO
    deterministic = sum(
        1 for c in required if c.oracle_tier is not None and c.oracle_tier in _DETERMINISTIC_TIERS
    )
    return deterministic / len(required)


def odr_below_floor(
    criteria: list[CriterionSpec],
    floor: float = DEFAULT_ODR_FLOOR,
    *,
    scope_id: str | None = None,
) -> bool:
    """Return whether the ODR is below *floor*, emitting an advisory finding when so.

    The check is advisory: a sub-floor ratio is logged at WARNING and the
    function returns ``True``, but nothing here blocks the close path. The
    caller (an iter-close seam) decides what to do with the bit. When the
    ratio meets or exceeds the floor nothing is logged and ``False`` is
    returned.

    Args:
        criteria: The wave / iter / phase criterion rows to score.
        floor: The advisory floor. Defaults to :data:`DEFAULT_ODR_FLOOR`.
        scope_id: Optional scope id surfaced in the advisory log line so
            the finding is attributable.

    Returns:
        ``True`` iff the computed ODR is strictly below *floor*.
    """
    ratio = oracle_determinism_ratio(criteria)
    if ratio >= floor:
        return False
    scope = scope_id if scope_id is not None else "unknown"
    logger.warning(
        f"odr_below_floor scope={scope!r} odr={ratio:.4f} floor={floor:.4f} "
        f"severity=advisory required={sum(1 for c in criteria if c.required)}"
    )
    return True


class OdrAdvisory(BaseModel):
    """One advisory finding row surfaced when the ODR falls below floor.

    Produced by :func:`iter_odr_advisory` and surfaced in an iter-close
    result so a low-determinism criterion set is visible at close time. The
    finding is advisory by construction -- it records the under-determined
    ratio for an operator to read, it never gates the close. The model is
    strict and frozen so a malformed advisory fails at construction rather
    than skewing a downstream rollup.

    Attributes:
        scope_id: The iter (or wave / phase) the advisory was computed for.
        odr: The computed Oracle-Determinism-Ratio in ``[0.0, 1.0]``.
        floor: The advisory floor the ratio fell below.
        required: Count of ``required`` criteria scored for the ratio.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str
    odr: float
    floor: float
    required: int


def iter_odr_advisory(
    criteria: list[CriterionSpec],
    *,
    scope_id: str,
    floor: float = DEFAULT_ODR_FLOOR,
) -> OdrAdvisory | None:
    """Score *criteria* and return an advisory finding when below *floor*.

    The binding seam between :func:`odr_below_floor` and an iter-close
    result. Delegates the floor decision (and the WARNING log line) to
    :func:`odr_below_floor`, then wraps a sub-floor verdict in a typed
    :class:`OdrAdvisory`. When the ratio meets or exceeds the floor -- which
    includes the sentinel empty / no-required-criteria path, where the ratio
    is :data:`EMPTY_RATIO` (``1.0``) -- nothing is logged and ``None`` is
    returned. The result is purely advisory: a caller surfaces the finding,
    it never blocks the close.

    Args:
        criteria: The closing scope's typed criterion rows (e.g. the
            aggregated criteria of an iter's closed waves).
        scope_id: The iter / wave / phase id the advisory is attributed to.
        floor: The advisory floor. Defaults to :data:`DEFAULT_ODR_FLOOR`.

    Returns:
        An :class:`OdrAdvisory` when the ODR is strictly below *floor*;
        ``None`` otherwise (including the empty / no-required-criteria
        sentinel path).
    """
    if not odr_below_floor(criteria, floor, scope_id=scope_id):
        return None
    return OdrAdvisory(
        scope_id=scope_id,
        odr=oracle_determinism_ratio(criteria),
        floor=floor,
        required=sum(1 for c in criteria if c.required),
    )


def escape_rate(findings: Iterable[EscapeFinding]) -> float:
    """Return the fraction of findings caught after the close gate.

    A finding caught at ``review`` or ``production`` (see
    :data:`EscapeStage`) is an escape; a finding caught at ``close`` is
    not. The rate is ``|escaped| / |findings|``. An empty ledger has no
    escapes, so the rate is ``0.0`` (never a ``ZeroDivisionError``).

    Args:
        findings: The tagged :class:`EscapeFinding` rows for one wave.

    Returns:
        The escape rate in ``[0.0, 1.0]``; ``0.0`` for an empty ledger.
    """
    rows = list(findings)
    if not rows:
        return 0.0
    escaped = sum(1 for f in rows if f.caught_at in _ESCAPED_STAGES)
    return escaped / len(rows)


__all__ = [
    "DEFAULT_ODR_FLOOR",
    "EMPTY_RATIO",
    "EscapeFinding",
    "EscapeStage",
    "OdrAdvisory",
    "escape_rate",
    "iter_odr_advisory",
    "odr_below_floor",
    "oracle_determinism_ratio",
]
