"""Strict projection header and the truth field every displayed value validates as.

The console never renders a bare value. Each one arrives as a :class:`TruthField` stating
whether the value is known, how it came to exist, how precise and how fresh it is, which
producer stated it at which revision, and what it rests on. Unknown, unavailable, denied,
purged and invalidated are distinct states that carry no value and always say why, so a
missing value never renders as a zero or a blank. :class:`ProjectionHeader` tops every
projection with its kind, revision, cursor, connection and completeness.

Both models forbid unknown fields and are frozen: a projection edited after validation
states a fact no producer stated.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND, ReadModelKind
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.base import Epoch2Model, NonEmptyStr, StrictPositiveInt
from eawf.kernel.state.types import UtcDatetime


class TruthState(StrEnum):
    """Whether a field's value is known, and which kind of absence it is when it is not."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    PURGED = "purged"
    INVALIDATED = "invalidated"


class TruthKind(StrEnum):
    """How a field's value came to exist."""

    STORED = "stored"
    OBSERVED = "observed"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    IMPORTED = "imported"


class Precision(StrEnum):
    """How exactly a value states its quantity."""

    EXACT = "exact"
    BOUNDED = "bounded"
    APPROXIMATE = "approximate"
    UNAVAILABLE = "unavailable"


class Freshness(StrEnum):
    """How current a value or a projection is against its source."""

    LIVE = "live"
    AGING = "aging"
    STALE = "stale"
    OFFLINE_SNAPSHOT = "offline_snapshot"
    REPLAYING = "replaying"
    GAP = "gap"


class ConnectionState(StrEnum):
    """The console's link to the daemon projection, always exactly one value."""

    LIVE = "live"
    GAP = "gap"
    REPLAYING = "replaying"
    SNAPSHOT_REQUIRED = "snapshot_required"
    SNAPSHOT_LOADING = "snapshot_loading"
    OFFLINE_SNAPSHOT = "offline_snapshot"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"


class Completeness(StrEnum):
    """Whether a projection may claim it holds every row of its scope.

    ``unverified`` is the state after a gap or during a snapshot transfer, where the
    projection can claim neither that it is complete nor which part is missing.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


class _ProjectionModel(Epoch2Model):
    """Strict and immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TruthField[T](_ProjectionModel):
    """One displayed value with the facts that make its rendering honest.

    Parametrise with a strict type (``TruthField[StrictInt]``) so a value of the wrong
    type is refused rather than coerced. A known field may hold ``None``: that is the
    declared no-value, which renders differently from every missing state.

    Attributes:
        value: The value; always ``None`` unless ``state`` is ``known``.
        state: Whether the value is known, or which absence it is.
        truth_kind: Whether the value was stored, observed, derived, estimated or imported.
        producer: The component or principal that stated the value.
        producer_revision: The producer's revision the value was read at.
        occurred_at: When the fact happened, when the producer knows.
        received_at: When the projection received the fact; never before ``occurred_at``.
        precision: How exactly the value states its quantity; ``unavailable`` for a
            missing value and never for a present one.
        measurement_quality: The quality of the measurement behind the value, under the
            same availability rule as ``precision``.
        freshness: How current the value is against its source.
        provenance_refs: The records the value rests on; a known field names at least one.
        missing_reason: Why the value is missing; set exactly when ``state`` is not
            ``known``.

    Raises:
        pydantic.ValidationError: a field is absent, unknown or of the wrong type, or the
            fields contradict each other (see :meth:`_check_truth`).
    """

    value: T | None
    state: TruthState
    truth_kind: TruthKind
    producer: NonEmptyStr
    producer_revision: StrictPositiveInt
    occurred_at: UtcDatetime | None = None
    received_at: UtcDatetime | None = None
    precision: Precision
    measurement_quality: MeasurementQuality
    freshness: Freshness
    provenance_refs: tuple[NonEmptyStr, ...]
    missing_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _check_truth(self) -> Self:
        """Refuse a field whose value, state, precision and provenance disagree.

        Raises:
            ValueError: every contradiction found, joined in one message.
        """
        known = self.state is TruthState.KNOWN
        unavailable = (
            self.precision is Precision.UNAVAILABLE,
            self.measurement_quality is MeasurementQuality.UNAVAILABLE,
        )
        estimated = self.truth_kind is TruthKind.ESTIMATED
        checks = (
            (known or self.value is None, "only a known field carries a value"),
            (known or self.missing_reason is not None, "a missing value names its reason"),
            (not known or self.missing_reason is None, "a known field has no missing_reason"),
            (known or all(unavailable), "a missing value has no precision or quality"),
            (self.value is None or not any(unavailable), "a value states precision and quality"),
            (not known or bool(self.provenance_refs), "a known field names its provenance"),
            (not estimated or self.precision is not Precision.EXACT, "an estimate is not exact"),
            (
                self.occurred_at is None
                or self.received_at is None
                or self.received_at >= self.occurred_at,
                "received_at precedes occurred_at",
            ),
        )
        problems = [message for holds, message in checks if not holds]
        if problems:
            raise ValueError(f"contradictory truth field: {'; '.join(problems)}")
        return self


class ProjectionHeader(_ProjectionModel):
    """The header every top-level projection carries.

    Attributes:
        schema_version: The projection schema this kernel reads, ``"1.0"``; any other
            is refused.
        projection_kind: The declared read model the projection satisfies.
        scope_id: The scope the projection was built for.
        projection_revision: The projection's own revision.
        source_cursor: The opaque event cursor the projection was built through.
        generated_at: When the projection was generated.
        observed_at: When its source was last observed; never after ``generated_at``.
        connection_state: The link to the daemon when the projection was generated.
        completeness: Whether the projection claims every row of its scope; ``complete``
            only while the connection is ``live``.
        freshness: How current the projection is against its source.
        producer_refs: The producers whose output the projection carries, at least one.
        policy_revision: The policy revision the projection was filtered under.

    Raises:
        pydantic.ValidationError: a field is absent, unknown or of the wrong type, the
            kind names a read model no projection carries, or the fields contradict each
            other (see :meth:`_check_header`).
    """

    schema_version: Literal["1.0"]
    projection_kind: ReadModelKind
    scope_id: NonEmptyStr
    projection_revision: StrictPositiveInt
    source_cursor: NonEmptyStr
    generated_at: UtcDatetime
    observed_at: UtcDatetime
    connection_state: ConnectionState
    completeness: Completeness
    freshness: Freshness
    producer_refs: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    policy_revision: StrictPositiveInt

    @model_validator(mode="after")
    def _check_header(self) -> Self:
        """Refuse a header whose kind, clock or completeness claim cannot hold.

        Raises:
            ValueError: every contradiction found, joined in one message.
        """
        checks = (
            (
                READ_MODEL_BY_KIND[self.projection_kind].projection_backed,
                f"no projection carries {self.projection_kind}",
            ),
            (self.observed_at <= self.generated_at, "observed_at is after generated_at"),
            (
                self.completeness is not Completeness.COMPLETE
                or self.connection_state is ConnectionState.LIVE,
                f"a {self.connection_state} projection cannot claim completeness",
            ),
        )
        problems = [message for holds, message in checks if not holds]
        if problems:
            raise ValueError(f"contradictory projection header: {'; '.join(problems)}")
        return self
