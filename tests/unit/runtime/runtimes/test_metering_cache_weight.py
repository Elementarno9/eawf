"""Cache reads are metered at their price, not one for one, against a Run's cap.

A prompt-cache read bills at a fraction of base input, so a long session
that re-reads a large cached prefix would otherwise reach its token cap on
tokens that cost a tenth as much. The in-flight governor carries the weight
a cache-read token counts at, and the native dispatch meters every Run with
the weight of the economics policy it loaded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from eawf.kernel.economics.governor import (
    DEFAULT_GOVERNOR,
    InFlightGovernor,
    economics_policy_from,
)
from eawf.kernel.identity import EntityKind, parse_qualified_urn
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.admission import admission_drift
from eawf.runtime.daemon.methods import run_budget
from eawf.runtime.runtimes.metering import InFlightMeter, UsageSample

pytestmark = pytest.mark.unit

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
GOVERNOR = {"max_concurrent_runs": 8, "max_in_flight_tokens": 4_000_000, "admission": "queue"}


def reading(input_tokens: int, output_tokens: int, cache_read: int) -> UsageSample:
    """Build one cumulative reading with the given token classes."""
    return UsageSample(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=cache_read
    )


# ---------------------------------------------------------------------------
# The weighted reading
# ---------------------------------------------------------------------------


def test_weighted_reading_meters_cache_reads_at_a_tenth() -> None:
    """10k in, 2k out and 1M cache read meter 112k at weight 0.1."""
    meter = InFlightMeter(cache_read_weight=0.1)

    assert meter.observe(reading(10_000, 2_000, 1_000_000)).observed_tokens == 112_000


def test_weighted_reading_at_full_weight_meters_as_before() -> None:
    """Weight 1.0 counts every cache read, so the same reading meters 1,012,000."""
    meter = InFlightMeter(cache_read_weight=1.0)

    assert meter.observe(reading(10_000, 2_000, 1_000_000)).observed_tokens == 1_012_000


def test_weighted_reading_default_is_full_weight() -> None:
    """A meter built without a weight keeps the one-for-one fold."""
    assert InFlightMeter().observe(reading(1, 2, 7)).observed_tokens == 10


def test_weighted_reading_rounds_a_fractional_cache_share_up() -> None:
    """One cache-read token at weight 0.1 still meters one token, never zero."""
    meter = InFlightMeter(cache_read_weight=0.1)

    assert meter.observe(reading(0, 0, 1)).observed_tokens == 1


def test_weighted_reading_at_zero_weight_ignores_cache_reads() -> None:
    """Weight 0 meters only input and output."""
    meter = InFlightMeter(cache_read_weight=0.0)

    assert meter.observe(reading(5, 6, 1_000_000)).observed_tokens == 11


def test_weighted_reading_with_no_tokens_meters_zero() -> None:
    """An empty reading meters zero at any weight."""
    assert InFlightMeter(cache_read_weight=0.1).observe(reading(0, 0, 0)).observed_tokens == 0


def test_weighted_reading_lower_than_running_total_is_held() -> None:
    """A later reading whose weighted total falls is held by the ratchet."""
    meter = InFlightMeter(cache_read_weight=0.1)
    meter.observe(reading(10_000, 2_000, 1_000_000))

    held = meter.observe(reading(10_000, 2_000, 500_000))

    assert held.observed_tokens == 112_000
    assert held.ratcheted is True
    assert held.sample_count == 2


# ---------------------------------------------------------------------------
# The governor weight
# ---------------------------------------------------------------------------


def test_governor_weight_defaults_to_a_tenth() -> None:
    """The shipped governor and an unweighted mapping both carry 0.1."""
    assert DEFAULT_GOVERNOR.cache_read_weight == pytest.approx(0.1)
    assert InFlightGovernor.model_validate(GOVERNOR).cache_read_weight == pytest.approx(0.1)
    assert economics_policy_from({}).governor.cache_read_weight == pytest.approx(0.1)


def test_governor_weight_is_read_from_the_economics_governor_mapping() -> None:
    """The ``economics.governor`` mapping sets the weight."""
    merged = {"economics": {"governor": {**GOVERNOR, "cache_read_weight": 0.25}}}

    assert economics_policy_from(merged).governor.cache_read_weight == pytest.approx(0.25)


@pytest.mark.parametrize("weight", [0, 0.0, 1, 1.0])
def test_governor_weight_accepts_the_closed_bounds(weight: float) -> None:
    """Both ends of [0, 1] are valid weights."""
    merged = {"economics": {"governor": {**GOVERNOR, "cache_read_weight": weight}}}

    assert economics_policy_from(merged).governor.cache_read_weight == pytest.approx(weight)


@pytest.mark.parametrize("weight", [1.5, -0.1, 1.0000001])
def test_governor_weight_out_of_range_is_refused(weight: float) -> None:
    """A weight outside [0, 1] is a validation error."""
    merged = {"economics": {"governor": {**GOVERNOR, "cache_read_weight": weight}}}

    with pytest.raises(ValidationError, match="cache_read_weight"):
        economics_policy_from(merged)


@pytest.mark.parametrize("weight", ["0.1", None, True])
def test_governor_weight_of_the_wrong_type_is_refused(weight: object) -> None:
    """A string, null or boolean weight is a validation error."""
    merged = {"economics": {"governor": {**GOVERNOR, "cache_read_weight": weight}}}

    with pytest.raises(ValidationError, match="cache_read_weight"):
        economics_policy_from(merged)


def test_governor_weight_changes_the_governor_digest() -> None:
    """A revised weight is a revised governor, so admissions can tell them apart."""
    revised = DEFAULT_GOVERNOR.model_copy(update={"cache_read_weight": 0.5})

    assert revised.governor_digest != DEFAULT_GOVERNOR.governor_digest


def test_governor_weight_default_keeps_the_digest_of_a_governor_without_it() -> None:
    """A governor at the default weight digests as it did before the weight existed."""
    unweighted = {**GOVERNOR, "max_in_flight_cost_microusd": None}

    assert InFlightGovernor.model_validate(GOVERNOR).governor_digest == canonical_digest(unweighted)


def test_governor_weight_default_strands_no_run_admitted_before_it() -> None:
    """A Run admitted before the weight existed resumes under the default without drift."""
    policy = economics_policy_from({"economics": {"governor": GOVERNOR}})
    earlier = {**GOVERNOR, "max_in_flight_cost_microusd": None}
    receipt = SimpleNamespace(
        prompt_budget=SimpleNamespace(policy_digest=policy.prompt_budget.policy_digest),
        governor_digest=canonical_digest(earlier),
    )

    assert admission_drift(cast(Any, receipt), policy) is None


def test_governor_weight_set_explicitly_to_the_default_digests_as_the_default() -> None:
    """Naming the default weight is the same governor as leaving it out."""
    explicit = InFlightGovernor.model_validate({**GOVERNOR, "cache_read_weight": 0.1})

    assert explicit.governor_digest == InFlightGovernor.model_validate(GOVERNOR).governor_digest


# ---------------------------------------------------------------------------
# The run meter wiring
# ---------------------------------------------------------------------------


def test_run_meter_weight_comes_from_the_loaded_economics_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under weight 0.25, 400k cache read meters 100k against the sealed cap."""
    seen: list[int] = []

    def guard(**kwargs: Any) -> Any:
        seen.append(kwargs["reading"].observed_tokens)
        return SimpleNamespace(notice=None, terminated=False)

    monkeypatch.setattr(run_budget, "guard_in_flight_budget", guard)
    economics = economics_policy_from(
        {"economics": {"governor": {**GOVERNOR, "cache_read_weight": 0.25}}}
    )
    args = SimpleNamespace(
        urn=parse_qualified_urn(RUN_URN, expected_kind=EntityKind.RUN), actor="OP-0001"
    )
    meter = native_dispatch.run_meter(
        cast(Any, SimpleNamespace(root=Path("unused"))),
        cast(Any, args),
        attempt=cast(Any, SimpleNamespace(attempt_ref="ATT-0001")),
        capsule=cast(Any, SimpleNamespace(budget=SimpleNamespace(tokens=2_000_000))),
        identity=cast(Any, None),
        cache_read_weight=economics.governor.cache_read_weight,
    )

    assert meter is not None
    assert asyncio.run(meter.observe(reading(0, 0, 400_000), None)) is False
    assert seen == [100_000]


def test_run_meter_weight_absent_cap_builds_no_meter() -> None:
    """An uncapped Run has no meter, whatever the weight."""
    meter = native_dispatch.run_meter(
        cast(Any, None),
        cast(Any, None),
        attempt=cast(Any, None),
        capsule=cast(Any, SimpleNamespace(budget=SimpleNamespace(tokens=None))),
        identity=cast(Any, None),
        cache_read_weight=0.1,
    )

    assert meter is None
