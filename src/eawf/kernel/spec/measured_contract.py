"""Measured-before-build contract records.

A *measured contract* is the typed carrier for a fact that was observed
by a re-runnable probe before any implementation asserted it. It exists
so a downstream checkpoint cites a measurement rather than an intuition:
the record pins WHAT was probed (:attr:`MeasuredContract.surface`), HOW
(:attr:`MeasuredContract.probe_command`), what came back
(:attr:`MeasuredContract.observed` plus the typed
:class:`ObservedLimit` rows), and — the load-bearing field — where the
measurement stops being valid (:attr:`MeasuredContract.boundary`).

The boundary is mandatory and non-blank because an unbounded
measurement is the failure mode this record exists to prevent: a number
observed once on one population, then quoted forever as if it held
everywhere. :class:`MeasurementEnvironment` carries the population the
number came from, and :attr:`MeasurementEnvironment.scale_band` lets a
consumer refuse a contract measured at a smaller scale than the
checkpoint it is being asked to back — see :func:`scale_band_satisfies`.

Every record here is frozen and ``extra="forbid"``: a contract is a
historical observation, so mutating one after the fact would silently
rewrite evidence. The freeze is shallow — :attr:`MeasuredContract.observed`
is a mapping whose contents Pydantic does not deep-copy — so callers
build the mapping at construction and never retain a handle to it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, ConfigDict, Field

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


def _reject_blank(value: str) -> str:
    """Reject a whitespace-only string.

    ``Field(min_length=1)`` alone admits ``"   "``, which reads as a
    populated field in JSON but carries no information. The measured
    contract treats a whitespace-only boundary as an absent boundary.

    Args:
        value: Candidate string, already length-checked by Pydantic.

    Returns:
        The unchanged *value* when it holds at least one non-space
        character.

    Raises:
        ValueError: When *value* is whitespace-only. Pydantic wraps this
            into a ``ValidationError`` at the model boundary.
    """
    if not value.strip():
        raise ValueError("must not be blank or whitespace-only")
    return value


#: A string field that must carry at least one non-whitespace character.
NonBlankStr = Annotated[str, Field(min_length=1), AfterValidator(_reject_blank)]

#: Measured-contract id: ``MCT-`` plus eight digits. The first six are the
#: observation date as ``YYMMDD``; the last two are an ordinal within that
#: date, so two contracts measured in the same session stay distinct and
#: the id sorts chronologically.
ContractIdStr = Annotated[str, Field(pattern=r"^MCT-\d{8}$")]

#: JSON-scalar value space for a raw observation. Keeping observations
#: scalar means a contract round-trips through the JSONL evidence store
#: and the ``Artifact.metadata`` map losslessly.
ObservedValue = str | int | float | bool

#: Whether an :class:`ObservedLimit` is an upper or a lower bound.
LimitDirection = Literal["ceiling", "floor"]


class ScaleBand(StrEnum):
    """Ordered population scale a measurement was taken at.

    The band is coarse on purpose: a consumer only needs to answer "was
    this measured at least as big as what I am about to assert over?",
    which is an ordering question, not a magnitude one. Rank order is
    :data:`SCALE_BAND_ORDER`.
    """

    TOY = "toy"
    DEV = "dev"
    PRODUCTION = "production"
    FLEET = "fleet"


#: Ascending rank order for :class:`ScaleBand`. Index in this tuple IS the
#: rank, so inserting a band mid-tuple re-ranks every band above it.
SCALE_BAND_ORDER: Final[tuple[ScaleBand, ...]] = (
    ScaleBand.TOY,
    ScaleBand.DEV,
    ScaleBand.PRODUCTION,
    ScaleBand.FLEET,
)


def _scale_band_rank(band: ScaleBand) -> int:
    """Return the ascending rank of *band* within :data:`SCALE_BAND_ORDER`.

    Args:
        band: Band to rank.

    Returns:
        Zero-based rank; ``TOY`` is ``0`` and ``FLEET`` is the maximum.

    Raises:
        ValueError: When *band* is not a member of
            :data:`SCALE_BAND_ORDER` (a new enum member added without
            extending the order tuple).
    """
    try:
        return SCALE_BAND_ORDER.index(band)
    except ValueError as exc:
        raise ValueError(f"scale band {band!r} is absent from SCALE_BAND_ORDER") from exc


def scale_band_satisfies(observed: ScaleBand, *, required: ScaleBand) -> bool:
    """Return whether *observed* is at least as large a band as *required*.

    Args:
        observed: Band the measurement was actually taken at.
        required: Band the implementing checkpoint asserts over.

    Returns:
        ``True`` when the measurement was taken at *required* scale or
        larger, so the checkpoint may cite it.

    Raises:
        ValueError: When either band is absent from
            :data:`SCALE_BAND_ORDER`.
    """
    return _scale_band_rank(observed) >= _scale_band_rank(required)


class MeasurementEnvironment(_StrictModel):
    """The population and host a measurement was taken on.

    Attributes:
        scale_band: Coarse population scale (see :class:`ScaleBand`).
        population: Prose description of exactly what was measured over,
            dense enough that a reader can tell whether their own case
            falls inside it.
        population_size: Count of units in the measured population (rows,
            objects, RPC calls). At least ``1`` — a zero-sized population
            measured nothing.
        host_platform: Platform the probe ran on, e.g. ``darwin``.
        toolchain: Toolchain the probe ran under, e.g. ``python 3.14.3``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scale_band: ScaleBand
    population: NonBlankStr
    population_size: Annotated[int, Field(ge=1)]
    host_platform: NonBlankStr
    toolchain: NonBlankStr


class ObservedLimit(_StrictModel):
    """One numeric bound read off a probe run.

    A limit is directional: a ``ceiling`` is the largest value the probe
    saw hold, a ``floor`` the smallest. :attr:`basis` names the probe
    output the number came from so the limit is re-derivable rather than
    merely asserted.

    Attributes:
        name: Snake-case identifier for the bound, unique within its
            contract.
        value: The measured number.
        unit: Unit the value is expressed in, e.g. ``bytes``, ``ms``,
            ``count``, ``percent``.
        direction: ``ceiling`` when the value is an upper bound,
            ``floor`` when it is a lower bound.
        basis: How the number was obtained — the probe output row or
            derivation it came from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonBlankStr
    value: float
    unit: NonBlankStr
    direction: LimitDirection
    basis: NonBlankStr


class MeasuredContract(_StrictModel):
    """A probe observation promoted into a citable contract.

    Attributes:
        contract_id: ``MCT-`` plus eight digits (see
            :data:`ContractIdStr`).
        surface: What was measured — the system boundary the observation
            characterises.
        probe_command: Re-runnable command that reproduces the
            observation. A consumer that doubts the contract re-runs this
            rather than arguing about it.
        observed: Raw scalar observations keyed by fact name. At least
            one entry; a contract with no observations measured nothing.
        limits: Typed numeric bounds derived from the observation. At
            least one row, for the same reason.
        boundary: Where the measurement stops being valid. Mandatory and
            non-blank: an unbounded number is the defect this record
            exists to prevent.
        observed_at: When the probe ran (timezone-aware UTC).
        observed_at_ref: Repo-relative pointer to the raw probe output the
            contract was extracted from.
        environment: Population and host the measurement was taken on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: ContractIdStr
    surface: NonBlankStr
    probe_command: NonBlankStr
    observed: Annotated[Mapping[str, ObservedValue], Field(min_length=1)]
    limits: Annotated[tuple[ObservedLimit, ...], Field(min_length=1)]
    boundary: NonBlankStr
    observed_at: UtcDatetime
    observed_at_ref: NonBlankStr
    environment: MeasurementEnvironment

    def limit(self, name: str) -> ObservedLimit:
        """Return the :class:`ObservedLimit` named *name*.

        Args:
            name: Limit name to look up.

        Returns:
            The matching limit row.

        Raises:
            KeyError: When no limit on this contract carries *name*.
        """
        for row in self.limits:
            if row.name == name:
                return row
        raise KeyError(f"contract {self.contract_id} has no limit named {name!r}")


__all__ = [
    "SCALE_BAND_ORDER",
    "ContractIdStr",
    "LimitDirection",
    "MeasuredContract",
    "MeasurementEnvironment",
    "NonBlankStr",
    "ObservedLimit",
    "ObservedValue",
    "ScaleBand",
    "scale_band_satisfies",
]
