"""Metering a turn while it runs, with reasoning counted once.

Two contracts are pinned here.

The first is arithmetic. Codex reports ``reasoning_output_tokens`` INSIDE
``output_tokens``, never beside it: the vendor's own ``total_tokens``
equals input plus output on every observed rollout. A meter that added the
reasoning slice would bill it twice and terminate a Run on tokens nobody
spent, so a sample claiming more reasoning than output is refused at the
boundary rather than folded.

The second is direction. Provider counters are cumulative, so the fold
adopts rather than adds, and it only ever ratchets up. A cumulative
counter that moves backwards mid-turn means the source re-based, and
applying it would walk a Run back under a cap it had already crossed --
the one direction a safety meter must never move.

Nothing here sleeps. The stream is a list, and the arrival order is the
list order, so the timing that matters (which reading the cap is tested
against) is decided by the fold, not the clock.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.metering import (
    InFlightMeter,
    MeterReading,
    UsageSample,
    meter_stream,
)


def sample(**overrides: int) -> UsageSample:
    """Build one cumulative reading, with *overrides* applied."""
    fields: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    fields.update(overrides)
    return UsageSample.model_validate(fields)


# ---------------------------------------------------------------------------
# D50: reasoning is a subset of output, never a summand
# ---------------------------------------------------------------------------


def test_reasoning_is_counted_inside_output_not_added_to_it() -> None:
    """Output stays 100 with 40 of reasoning inside, not 140."""
    reading = meter_stream([sample(input_tokens=10, output_tokens=100, reasoning_output_tokens=40)])

    assert reading.output_tokens == 100
    assert reading.reasoning_output_tokens == 40
    assert reading.observed_tokens == 110


def test_a_pure_reasoning_turn_bills_its_output_once() -> None:
    """Every output token being reasoning is the boundary of the subset rule."""
    reading = meter_stream([sample(output_tokens=64, reasoning_output_tokens=64)])

    assert reading.observed_tokens == 64


def test_a_sample_claiming_more_reasoning_than_output_is_refused() -> None:
    """That reading can only be the double-counted summand this rule removed."""
    with pytest.raises(ValidationError, match="reasoning is a subset of output"):
        sample(output_tokens=100, reasoning_output_tokens=101)


def test_a_sample_disclosing_no_reasoning_meters_the_same_total() -> None:
    """The billable total does not depend on the reasoning attribution."""
    with_reasoning = sample(input_tokens=10, output_tokens=100, reasoning_output_tokens=40)
    without = sample(input_tokens=10, output_tokens=100)

    assert with_reasoning.billable_tokens == without.billable_tokens


def test_cache_reads_are_billed_into_the_metered_total() -> None:
    reading = meter_stream([sample(input_tokens=1, output_tokens=2, cache_read_input_tokens=7)])

    assert reading.observed_tokens == 10


# ---------------------------------------------------------------------------
# The fold: cumulative readings are adopted, and only ratchet up
# ---------------------------------------------------------------------------


def test_an_empty_stream_meters_zero() -> None:
    """A turn that disclosed nothing crosses no positive cap."""
    reading = meter_stream([])

    assert reading == MeterReading()
    assert reading.observed_tokens == 0
    assert reading.sample_count == 0


def test_a_single_reading_is_the_whole_fold() -> None:
    reading = meter_stream([sample(output_tokens=5)])

    assert reading.observed_tokens == 5
    assert reading.sample_count == 1
    assert reading.ratcheted is False


def test_cumulative_readings_are_adopted_rather_than_summed() -> None:
    """Three readings of a running total meter the last one, not their sum."""
    reading = meter_stream(
        [sample(output_tokens=10), sample(output_tokens=25), sample(output_tokens=40)]
    )

    assert reading.observed_tokens == 40
    assert reading.sample_count == 3


def test_a_reading_that_moved_backwards_is_held_not_applied() -> None:
    """A re-based counter must not walk a Run back under a crossed cap."""
    meter = InFlightMeter()
    meter.observe(sample(output_tokens=90))

    reading = meter.observe(sample(output_tokens=3))

    assert reading.observed_tokens == 90
    assert reading.ratcheted is True


def test_the_ratchet_flag_survives_a_later_forward_reading() -> None:
    """One re-base taints the fold, so the surface never claims a clean count."""
    meter = InFlightMeter()
    meter.observe(sample(output_tokens=90))
    meter.observe(sample(output_tokens=3))

    reading = meter.observe(sample(output_tokens=120))

    assert reading.observed_tokens == 120
    assert reading.ratcheted is True


def test_a_repeated_reading_advances_the_count_but_not_the_total() -> None:
    """A provider re-emitting its totals is not a turn spending them twice."""
    meter = InFlightMeter()
    meter.observe(sample(output_tokens=50))

    reading = meter.observe(sample(output_tokens=50))

    assert reading.observed_tokens == 50
    assert reading.sample_count == 2
    assert reading.ratcheted is False


def test_a_fresh_meter_reads_zero_before_anything_is_observed() -> None:
    assert InFlightMeter().reading.observed_tokens == 0


# ---------------------------------------------------------------------------
# Boundary exactness: the fold lands ON the number, not near it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last_output", "expected"),
    [(40, 50), (89, 99), (90, 100), (91, 101)],
    ids=["well-under", "one-under", "exactly-at", "one-over"],
)
def test_the_fold_lands_exactly_on_the_streamed_total(last_output: int, expected: int) -> None:
    """Off-by-one on either side of a 100-token cap is exact, not approximate."""
    stream = [sample(input_tokens=10, output_tokens=value) for value in (0, 40, last_output)]

    assert meter_stream(stream).observed_tokens == expected
    assert meter_stream(stream).ratcheted is False


def test_a_long_stream_meters_its_maximum_exactly() -> None:
    stream = [sample(output_tokens=value) for value in range(0, 1_000)]

    assert meter_stream(stream).observed_tokens == 999
    assert meter_stream(stream).sample_count == 1_000


# ---------------------------------------------------------------------------
# Error paths of the reading itself
# ---------------------------------------------------------------------------


def test_a_negative_output_reading_is_refused() -> None:
    with pytest.raises(ValidationError, match="output_tokens"):
        sample(output_tokens=-1)


def test_a_negative_input_reading_is_refused() -> None:
    with pytest.raises(ValidationError, match="input_tokens"):
        sample(input_tokens=-1)


def test_an_unknown_usage_field_is_refused() -> None:
    """A misspelled token class would meter silently as zero."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        UsageSample.model_validate({"output_tokens": 5, "reasoning_tokens": 5})


def test_a_usage_sample_is_frozen() -> None:
    row = sample(output_tokens=5)

    with pytest.raises(ValidationError):
        row.output_tokens = 9  # type: ignore[misc]


def test_a_reading_is_frozen() -> None:
    reading = meter_stream([sample(output_tokens=5)])

    with pytest.raises(ValidationError):
        reading.observed_tokens = 9  # type: ignore[misc]
