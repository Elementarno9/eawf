"""The coverage an aggregate declares, and pricing coverage weighted by tokens.

An aggregate that states only its figure reads as a whole population even
when half the subjects never contributed. :class:`Coverage` travels with
the figure instead: how many subjects there were, how many contributed,
how many could not be attributed, which fields were null, and whether the
population was a bounded sample. A share over no subjects is undefined
and is ``None``, never zero.

Pricing coverage is the same idea on the price axis, and it is weighted
by tokens rather than by rows: a row count reads healthy while the few
largest rows carry all the unpriced volume. The unpriced share is broken
down by the cause each unpriced row can name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from eawf.observability.telemetry.models import ObservedSession, PriceSourceKind

_Count = Annotated[int, Field(ge=0)]

#: Why an unpriced row carries no price: the record named no model, or it
#: named one the rate table has no row for.
UnpricedCause = Literal["model_unrecorded", "model_not_in_rate_table"]

#: The price sources in the order a coverage report states them.
_SOURCES: Final = (
    PriceSourceKind.BILLED,
    PriceSourceKind.LIST_RECONSTRUCTED,
    PriceSourceKind.UNPRICED,
)


def _share(part: int, whole: int) -> float | None:
    """Return ``part / whole``, or ``None`` when there is no whole to divide."""
    return part / whole if whole else None


class Coverage(BaseModel):
    """What an aggregate covered, stated beside it.

    Attributes:
        subjects_total: Every subject the aggregate was taken over.
        subjects_contributing: Those that contributed a value.
        subjects_unattributed: Those that contributed a value no named
            producer or subject can be attributed to.
        null_fields: The fields that were null on at least one subject.
        sampled: Whether the subjects are a bounded sample rather than the
            whole population, so a reader never takes the figure for one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subjects_total: _Count
    subjects_contributing: _Count
    subjects_unattributed: _Count = 0
    null_fields: tuple[str, ...] = ()
    sampled: bool = False

    @model_validator(mode="after")
    def _parts_fit_the_whole(self) -> Self:
        """Refuse a coverage whose parts exceed its population.

        Raises:
            ValueError: More subjects contributed, or went unattributed,
                than there are.
        """
        if self.subjects_contributing > self.subjects_total:
            raise ValueError(
                f"{self.subjects_contributing} contributing subjects exceed "
                f"the {self.subjects_total} there are"
            )
        if self.subjects_unattributed > self.subjects_contributing:
            raise ValueError("an unattributed subject is one that contributed")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def contributing_share(self) -> float | None:
        """The share of subjects that contributed; ``None`` over no subjects."""
        return _share(self.subjects_contributing, self.subjects_total)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unattributed_share(self) -> float | None:
        """The share of contributing subjects left unattributed."""
        return _share(self.subjects_unattributed, self.subjects_contributing)


class PricingCoverage(BaseModel):
    """How much of a token volume was priced, and from where.

    Attributes:
        total_tokens: The tokens every row counted.
        tokens_by_source: The tokens each price source covers.
        unpriced_tokens_by_cause: The unpriced tokens by the cause of each
            unpriced row.
        coverage: The rows the figure was taken over.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_tokens: _Count
    tokens_by_source: Mapping[PriceSourceKind, _Count]
    unpriced_tokens_by_cause: Mapping[UnpricedCause, _Count]
    coverage: Coverage

    @model_validator(mode="after")
    def _breakdowns_reconcile(self) -> Self:
        """Hold both breakdowns to the total they split.

        Raises:
            ValueError: The sources do not sum to the total, or the causes
                do not sum to the unpriced tokens.
        """
        if sum(self.tokens_by_source.values()) != self.total_tokens:
            raise ValueError("the tokens by price source do not sum to the total")
        unpriced = self.tokens_by_source.get(PriceSourceKind.UNPRICED, 0)
        if sum(self.unpriced_tokens_by_cause.values()) != unpriced:
            raise ValueError("the unpriced tokens by cause do not sum to the unpriced tokens")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def share_by_source(self) -> dict[str, float | None]:
        """Each price source's share of the tokens; ``None`` over no tokens."""
        return {
            source.value: _share(self.tokens_by_source.get(source, 0), self.total_tokens)
            for source in _SOURCES
        }


def unpriced_cause(session: ObservedSession) -> UnpricedCause:
    """Return why an unpriced *session* carries no price.

    Args:
        session: An observed session whose price source is ``unpriced``.

    Returns:
        ``model_unrecorded`` when the record named no model, otherwise
        ``model_not_in_rate_table``.
    """
    return "model_unrecorded" if session.model is None else "model_not_in_rate_table"


def pricing_coverage(sessions: Iterable[ObservedSession]) -> PricingCoverage:
    """Weigh the price sources of *sessions* by the tokens each counted.

    Args:
        sessions: The observed sessions a sweep wrote.

    Returns:
        The token-weighted coverage, whose ``coverage`` counts a session as
        contributing when it counted any token, as unattributed when it did
        so under no recorded model, and names ``cost_usd`` as a null field
        when any session was unpriced.
    """
    rows = tuple(sessions)
    by_source = dict.fromkeys(_SOURCES, 0)
    by_cause: dict[UnpricedCause, int] = {}
    for row in rows:
        by_source[row.price_source] += row.total_tokens
        if row.price_source is PriceSourceKind.UNPRICED:
            cause = unpriced_cause(row)
            by_cause[cause] = by_cause.get(cause, 0) + row.total_tokens
    unpriced_rows = any(row.price_source is PriceSourceKind.UNPRICED for row in rows)
    return PricingCoverage(
        total_tokens=sum(by_source.values()),
        tokens_by_source=by_source,
        unpriced_tokens_by_cause=by_cause,
        coverage=Coverage(
            subjects_total=len(rows),
            subjects_contributing=sum(1 for row in rows if row.total_tokens),
            subjects_unattributed=sum(1 for row in rows if row.total_tokens and row.model is None),
            null_fields=("cost_usd",) if unpriced_rows else (),
        ),
    )


__all__ = [
    "Coverage",
    "PricingCoverage",
    "UnpricedCause",
    "pricing_coverage",
    "unpriced_cause",
]
