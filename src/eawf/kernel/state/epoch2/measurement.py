"""What a Run measured: its vendor session, its counter snapshots, its spans.

A Run is an internal episode; the usage it burns is counted by the vendor
runtime it drives, under that runtime's own session. The two identities
are different facts, so a Run names its vendor session in a typed
:class:`VendorSessionRef` rather than letting capture guess it from a Run
key, an agent session id or "the latest transcript" -- each of which has
read another episode's counters before.

Counters are read twice: once when the Run starts and once when it stops.
The start reading is the baseline, the stop reading is cumulative, and
only their difference is the Run's. The difference is typed apart from
both readings (:class:`MeasuredRuntime`) so a cumulative figure can never
be filed where a delta belongs.

Absence is a value here, not a missing number. A counter the producer
could not observe is :class:`Unobserved` with a reason; it is never zero,
because a zero joins every sum it meets and silently understates the
population, while an unobserved reading refuses to. The same rule holds
one level up: a Run whose session yielded no reading records
:class:`UncapturedRuntime`, and a Run whose two readings cannot be
differenced records :class:`ExcludedRuntime` with the reason, so a
consumer can count what it did not get instead of never learning of it.

A measured share states its quality on the four-value ladder every usage
reading uses -- ``measured``, ``derived``, ``estimated``, ``unavailable``
-- and never as ``reconstructed``: a share computed from recorded counters
is ``derived``, and how it was obtained rides beside the quality as a
:data:`ReconstructionBasis`, so a producer cannot assert its own accuracy
through the quality label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from eawf.kernel.state.enums import QualityLadder
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    SlugStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.runtime.session.vendor_id import hash_vendor_session_id

#: What a counter row carries when it cannot name its harness or model. A
#: row with no attribution is still a row, but it must say so rather than
#: borrow a default that would file it under a harness it never ran on.
UNKNOWN_ATTRIBUTION: Final = "unknown"

#: The Run fields the daemon reads off the vendor session at the edge
#: rather than taking from the caller. A retry reads them again and gets
#: different numbers, so they never identify the request they ride on. The
#: runtime tuple is among them because its version and model are read off
#: the session's transcript, which grows between a request and its retry.
DAEMON_READ_RUN_FIELDS: Final = frozenset({"counter_baseline", "captured_runtime", "runtime_tuple"})

#: How a quantity not read from the record was obtained: computed from
#: counters the runtime's transcript recorded, matched against a
#: per-version table, or read from disk at report time (and so possibly
#: not what the session saw). A quantity read from the record has none.
ReconstructionBasis = Literal["recorded_in_transcript", "version_table", "read_from_disk_now"]

#: A harness or model name as a counter row records it: the runtime's own
#: id, or :data:`UNKNOWN_ATTRIBUTION`.
AttributionName = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)
]

#: A vendor session id in the digest form state stores. A raw id names a
#: file on the operator's machine, so it is hashed on the way in and the
#: digest is what capture matches against.
VendorSessionDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^vsid-[0-9a-f]{32}$")]


class VendorSessionRef(Epoch2Model):
    """The vendor runtime session a Run's usage is counted under.

    Attributes:
        harness: The runtime that owns the session, such as
            ``claude-code``. Capture resolves a transcript or sidecar only
            through a reader registered for this harness.
        session_digest: The session id, hashed. A raw id is accepted and
            hashed on validation, so the raw id never reaches a record.
    """

    harness: SlugStr
    session_digest: VendorSessionDigest

    @field_validator("session_digest", mode="before")
    @classmethod
    def _hash_raw_id(cls, value: object) -> object:
        """Hash a raw vendor session id; pass anything else to validation."""
        if isinstance(value, str) and value:
            return hash_vendor_session_id(value)
        return value


class CaptureSource(StrEnum):
    """Which surface a counter reading was taken from.

    The source is a measurement-quality marker in its own right: the
    transcript is the runtime's own row-by-row record, while the sidecar
    is the last figure a statusline happened to render.
    """

    TRANSCRIPT = "transcript"
    SIDECAR = "sidecar"


#: The basis a derived share takes from the surface its counters came from:
#: the transcript is the runtime's own record, while the sidecar is a file
#: read off disk when the reading was taken.
_BASIS_BY_SOURCE: Final[Mapping[CaptureSource, ReconstructionBasis]] = {
    CaptureSource.TRANSCRIPT: "recorded_in_transcript",
    CaptureSource.SIDECAR: "read_from_disk_now",
}


class CounterName(StrEnum):
    """The counters one reading carries, every one of them in each reading."""

    DURATION_MS = "duration_ms"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    CACHE_CREATION_INPUT_TOKENS = "cache_creation_input_tokens"
    CACHE_READ_INPUT_TOKENS = "cache_read_input_tokens"
    COST_USD = "cost_usd"


class Observed(Epoch2Model):
    """A quantity the producer saw, including a genuine zero."""

    state: Literal["observed"] = "observed"
    value: Annotated[Decimal, Field(ge=0)]


class Unobserved(Epoch2Model):
    """A quantity the producer could not see, and why. Never a zero."""

    state: Literal["unobserved"] = "unobserved"
    reason: NonEmptyStr


#: One counter's reading: seen, or not seen with a reason.
CounterReading = Annotated[Observed | Unobserved, Field(discriminator="state")]


def _require_every_counter[ReadingT](
    counters: Mapping[CounterName, ReadingT],
) -> Mapping[CounterName, ReadingT]:
    """Refuse a reading that silently omits a counter.

    An omitted counter would read as absent to one consumer and as zero to
    another; spelling it :class:`Unobserved` leaves one answer.

    Raises:
        ValueError: A counter is missing.
    """
    missing = sorted(set(CounterName) - set(counters))
    if missing:
        raise ValueError(f"counter reading omits {missing}; record each as unobserved instead")
    return counters


class CounterSnapshot(Epoch2Model):
    """One cumulative reading of a vendor session, at a Run's start or stop.

    Attributes:
        captured_at: The instant the reading stands at.
        source: The surface the reading came from.
        harness: The runtime that counted, or ``unknown``.
        model: The model it billed against, or ``unknown``.
        measurement_version: The producer's definition of the counters.
            Two readings are comparable only under the same version.
        concurrent_run_count: How many active Runs shared the vendor
            session when the reading was taken, this one included. It is
            required because an absent divisor makes every later share of
            the session unfalsifiable.
        derived: Whether the reading is bounded rather than read: the host
            reports a turn's usage only when the turn ends, so a Run that
            started mid-turn is given the proportional share of that turn.
        counters: Every counter, cumulative over the session.
    """

    captured_at: UtcDatetime
    source: CaptureSource
    harness: AttributionName
    model: AttributionName
    measurement_version: StrictPositiveInt
    concurrent_run_count: StrictPositiveInt
    derived: bool = False
    counters: Mapping[CounterName, CounterReading]

    @field_validator("counters")
    @classmethod
    def _every_counter(
        cls, value: Mapping[CounterName, CounterReading]
    ) -> Mapping[CounterName, CounterReading]:
        return _require_every_counter(value)


class SpanPhase(StrEnum):
    """What a measured span of a Run's time was spent on.

    Only phases a producer emits are declared: a phase with no producer
    would be a renderer row nothing can ever fill.
    """

    REASONING_REQUEST = "reasoning_request"
    PLAIN_REQUEST = "plain_request"
    TOOL_CALL = "tool_call"
    IDLE = "idle"


class MeasuredSpan(Epoch2Model):
    """One interval of a Run's time, and who measured it.

    Attributes:
        phase: What the interval was spent on.
        started_at: When the interval began.
        ended_at: When it ended.
        producer: The producer that measured it, or ``None`` when none can
            be named.
        unattributed_reason: Why no producer can be named. Present exactly
            when ``producer`` is absent, so an unattributed span always
            says why.
    """

    phase: SpanPhase
    started_at: UtcDatetime
    ended_at: UtcDatetime
    producer: SlugStr | None = None
    unattributed_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _attribution_is_named_or_explained(self) -> Self:
        """Require a producer or a reason, never both and never neither.

        Raises:
            ValueError: Both or neither of the two are set, or the span
                ends before it starts.
        """
        if (self.producer is None) == (self.unattributed_reason is None):
            raise ValueError("a span names its producer or the reason it has none, not both")
        if self.ended_at < self.started_at:
            raise ValueError("a span cannot end before it starts")
        return self


class SpanSummary(Epoch2Model):
    """An aggregate over a Run's spans, with its unattributed share beside it.

    Attributes:
        total_ms: Every span's duration, attributed or not.
        attributed_ms: The duration a named producer measured.
        unattributed_ms: The duration no producer can be named for.
        unattributed_share: ``unattributed_ms / total_ms``; zero when the
            spans cover no time at all.
        phase_ms: The attributed duration per phase.
    """

    total_ms: StrictNonNegativeInt
    attributed_ms: StrictNonNegativeInt
    unattributed_ms: StrictNonNegativeInt
    unattributed_share: Annotated[Decimal, Field(ge=0, le=1)]
    phase_ms: Mapping[SpanPhase, StrictNonNegativeInt]


def _span_ms(span: MeasuredSpan) -> int:
    """Return a span's duration in whole milliseconds."""
    return int((span.ended_at - span.started_at).total_seconds() * 1000)


def summarize_spans(spans: Sequence[MeasuredSpan]) -> SpanSummary:
    """Aggregate *spans*, reporting the unattributed share beside the total.

    An unattributed span counts toward the total and never toward a
    phase, so the phase breakdown cannot silently absorb time nobody
    measured.

    Args:
        spans: The Run's spans.

    Returns:
        The summary.

    Raises:
        ValueError: *spans* is empty; an aggregate over nothing has no
            share, and the caller records the spans as unobserved instead.
    """
    if not spans:
        raise ValueError("cannot summarize an empty span collection")
    phase_ms = dict.fromkeys(SpanPhase, 0)
    unattributed_ms = 0
    for span in spans:
        duration = _span_ms(span)
        if span.producer is None:
            unattributed_ms += duration
        else:
            phase_ms[span.phase] += duration
    attributed_ms = sum(phase_ms.values())
    total_ms = attributed_ms + unattributed_ms
    share = Decimal(unattributed_ms) / Decimal(total_ms) if total_ms else Decimal(0)
    return SpanSummary(
        total_ms=total_ms,
        attributed_ms=attributed_ms,
        unattributed_ms=unattributed_ms,
        unattributed_share=share,
        phase_ms=phase_ms,
    )


class UncapturedReason(StrEnum):
    """Why a Run holds no captured runtime at all."""

    NO_VENDOR_SESSION = "no_vendor_session"
    NO_SOURCE = "no_source"
    NO_BASELINE = "no_baseline"


class UncapturedRuntime(Epoch2Model):
    """A Run whose vendor session yielded nothing to difference.

    It is recorded rather than left null so "capture never ran" and
    "capture ran and found nothing" stay two different facts, and so a
    missing baseline is never differenced as a zero one.
    """

    outcome: Literal["uncaptured"] = "uncaptured"
    reason: UncapturedReason


class ExclusionReason(StrEnum):
    """Why a Run's two readings cannot be differenced."""

    MEASUREMENT_VERSION_CHANGED = "measurement_version_changed"
    SOURCE_CHANGED = "source_changed"
    COUNTER_RESET = "counter_reset"


class ExcludedRuntime(Epoch2Model):
    """A Run whose readings exist but do not measure its work.

    The exclusion is calibration signal in its own right, so it is kept
    with its reason and both readings' versions rather than dropped.
    """

    outcome: Literal["excluded"] = "excluded"
    reason: ExclusionReason
    detail: NonEmptyStr
    baseline_version: StrictPositiveInt
    terminal_version: StrictPositiveInt


class MeasuredRuntime(Epoch2Model):
    """A Run's own share of its vendor session, start to stop.

    Attributes:
        source: The surface both readings came from.
        harness: The runtime that counted, or ``unknown``.
        model: The model the terminal reading billed against, or
            ``unknown``.
        measurement_version: The definition both readings share; rollups
            combine only rows that agree on it.
        divisor: How many Runs shared the session when the baseline was
            taken. Every counter is already divided by it, so no share of
            a shared interval is handed whole to more than one Run.
        derived: Whether the baseline was bounded rather than read.
        measurement_quality: ``measured`` for a sole, read baseline;
            ``derived`` for a share of a shared session; ``estimated`` when
            the baseline was bounded. Never ``unavailable``: a Run with no
            reading is :class:`UncapturedRuntime`.
        reconstruction_basis: How a share not read whole from the record
            was obtained; present exactly when the quality is not
            ``measured``.
        counters: The Run's share of each counter.
        spans: The span aggregate, or why there is none.
    """

    outcome: Literal["measured"] = "measured"
    source: CaptureSource
    harness: AttributionName
    model: AttributionName
    measurement_version: StrictPositiveInt
    divisor: StrictPositiveInt
    derived: bool
    measurement_quality: QualityLadder
    reconstruction_basis: ReconstructionBasis | None = None
    counters: Mapping[CounterName, CounterReading]
    spans: SpanSummary | Unobserved

    @field_validator("counters")
    @classmethod
    def _every_counter(
        cls, value: Mapping[CounterName, CounterReading]
    ) -> Mapping[CounterName, CounterReading]:
        return _require_every_counter(value)

    @model_validator(mode="after")
    def _basis_names_every_reconstruction(self) -> Self:
        """Bind the basis to the quality, and the quality to the divisor.

        Raises:
            ValueError: The quality is not the one the divisor and the
                bounded baseline stamp, or a basis is missing on a
                reconstructed share or present on a measured one.
        """
        expected = _quality(divisor=self.divisor, derived=self.derived)
        if self.measurement_quality != expected:
            raise ValueError(
                f"a share with divisor {self.divisor} and derived={self.derived} is "
                f"{expected}, not {self.measurement_quality}"
            )
        reconstructed = self.measurement_quality != "measured"
        if reconstructed != (self.reconstruction_basis is not None):
            raise ValueError("a reconstruction_basis is stated exactly on a share not measured")
        return self


#: What a Run's capture came to once it stopped.
CapturedRuntime = Annotated[
    MeasuredRuntime | ExcludedRuntime | UncapturedRuntime, Field(discriminator="outcome")
]


def _quality(*, divisor: int, derived: bool) -> QualityLadder:
    """Return the quality a divisor and a derived baseline stamp on a row."""
    if derived:
        return "estimated"
    if divisor > 1:
        return "derived"
    return "measured"


def _counter_delta(
    name: CounterName, start: Observed | Unobserved, stop: Observed | Unobserved, divisor: int
) -> Observed | Unobserved | None:
    """Return one counter's share, or ``None`` when the counter went backwards."""
    if isinstance(start, Unobserved):
        return Unobserved(reason=f"{name.value} unobserved at start: {start.reason}")
    if isinstance(stop, Unobserved):
        return Unobserved(reason=f"{name.value} unobserved at stop: {stop.reason}")
    if stop.value < start.value:
        return None
    return Observed(value=(stop.value - start.value) / divisor)


def measure_run(
    baseline: CounterSnapshot | UncapturedRuntime,
    terminal: CounterSnapshot | None,
    *,
    spans: SpanSummary | Unobserved,
) -> MeasuredRuntime | ExcludedRuntime | UncapturedRuntime:
    """Difference a Run's two readings into its own share of the session.

    Args:
        baseline: The start reading, or the record that there was none.
        terminal: The stop reading, or ``None`` when the session yielded
            none at stop.
        spans: The span aggregate over the Run, or why there is none.

    Returns:
        The measured share; the exclusion when the two readings are not
        comparable; or the uncaptured record when either is missing.
    """
    if isinstance(baseline, UncapturedRuntime):
        return UncapturedRuntime(reason=UncapturedReason.NO_BASELINE)
    if terminal is None:
        return UncapturedRuntime(reason=UncapturedReason.NO_SOURCE)

    def excluded(reason: ExclusionReason, detail: str) -> ExcludedRuntime:
        return ExcludedRuntime(
            reason=reason,
            detail=detail,
            baseline_version=baseline.measurement_version,
            terminal_version=terminal.measurement_version,
        )

    if baseline.measurement_version != terminal.measurement_version:
        return excluded(
            ExclusionReason.MEASUREMENT_VERSION_CHANGED,
            "the readings were produced under different counter definitions",
        )
    if baseline.source is not terminal.source:
        return excluded(
            ExclusionReason.SOURCE_CHANGED,
            f"baseline read from {baseline.source.value}, stop from {terminal.source.value}",
        )
    divisor = baseline.concurrent_run_count
    counters: dict[CounterName, Observed | Unobserved] = {}
    for name in CounterName:
        delta = _counter_delta(name, baseline.counters[name], terminal.counters[name], divisor)
        if delta is None:
            return excluded(
                ExclusionReason.COUNTER_RESET, f"{name.value} fell between start and stop"
            )
        counters[name] = delta
    harness = terminal.harness
    if baseline.harness != terminal.harness:
        harness = UNKNOWN_ATTRIBUTION
    quality = _quality(divisor=divisor, derived=baseline.derived)
    return MeasuredRuntime(
        source=terminal.source,
        harness=harness,
        model=terminal.model,
        measurement_version=terminal.measurement_version,
        divisor=divisor,
        derived=baseline.derived,
        measurement_quality=quality,
        reconstruction_basis=None if quality == "measured" else _BASIS_BY_SOURCE[terminal.source],
        counters=counters,
        spans=spans,
    )


__all__ = [
    "DAEMON_READ_RUN_FIELDS",
    "UNKNOWN_ATTRIBUTION",
    "AttributionName",
    "CaptureSource",
    "CapturedRuntime",
    "CounterName",
    "CounterReading",
    "CounterSnapshot",
    "ExcludedRuntime",
    "ExclusionReason",
    "MeasuredRuntime",
    "MeasuredSpan",
    "Observed",
    "ReconstructionBasis",
    "SpanPhase",
    "SpanSummary",
    "UncapturedReason",
    "UncapturedRuntime",
    "Unobserved",
    "VendorSessionDigest",
    "VendorSessionRef",
    "measure_run",
    "summarize_spans",
]
