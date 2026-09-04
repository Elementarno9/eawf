"""Perf harness proving the turn-cost regression gate actually fires.

A gate nobody has watched go red is not evidence of anything. This harness
replays one fixed fixture through the completed-unit producer twice with
the same fixture id and harness revision declared on both passes, accepts
the first pass as the baseline, then injects a synthetic regression into
the second pass and asserts the check blocks.

The injection is deliberately p90-shaped rather than uniform: it scales
only the two slowest units, which under nearest-rank leaves p50 (rank 5 of
10) untouched while moving p90 (rank 9 of 10). A gate that only reddened
when the whole distribution shifted would miss the tail regression this
measurement exists to catch, so the p50-unchanged assertion is part of the
proof rather than decoration.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from eawf.observability.bench.turn_cost import (
    TURN_COST_HARNESS_REVISION,
    TurnCostCorpus,
    TurnCostVerdict,
    baseline_from_record,
    build_corpus_record,
    compare_turn_cost,
    seed_turn_cost_corpus,
)
from eawf.observability.telemetry.turn_cost import CompletedUnitRun

pytestmark = pytest.mark.unit

_FIXTURE_ID = "turn-cost-small"
_THRESHOLD = Decimal("0.10")

#: Wave ids of the two slowest fixture units — the p90 tail under
#: nearest-rank over ten units.
_TAIL_WAVE_IDS = frozenset({"P00-I00-W09", "P00-I00-W10"})


def _inject_p90_regression(corpus: TurnCostCorpus, *, factor: Decimal) -> TurnCostCorpus:
    """Return *corpus* with only its tail units scaled by *factor*.

    The fixture id and harness revision are carried through unchanged: the
    injected corpus must stay comparable to the baseline, or the check
    would refuse for the wrong reason and prove nothing about regressions.
    """
    scaled: list[CompletedUnitRun] = []
    for run in corpus.runs:
        if run.wave_id not in _TAIL_WAVE_IDS:
            scaled.append(run)
            continue
        scaled.append(
            run.model_copy(
                update={
                    "wall_clock_ms": int(run.wall_clock_ms * factor),
                    "cost_usd": run.cost_usd * factor,
                }
            )
        )
    return TurnCostCorpus(
        fixture_id=corpus.fixture_id,
        runtime=corpus.runtime,
        model=corpus.model,
        waves=corpus.waves,
        runs=tuple(scaled),
    )


def test_turn_cost_harness_replay_is_byte_identical() -> None:
    """Two passes over one fixed fixture produce the same record."""
    first = build_corpus_record(seed_turn_cost_corpus(_FIXTURE_ID))
    second = build_corpus_record(seed_turn_cost_corpus(_FIXTURE_ID))

    assert first.model_dump_json() == second.model_dump_json()
    assert first.fixture_id == second.fixture_id == _FIXTURE_ID
    assert first.harness_revision == second.harness_revision == TURN_COST_HARNESS_REVISION


def test_turn_cost_harness_unchanged_replay_passes_its_own_baseline() -> None:
    """The gate does not red on an unchanged second pass (false-positive guard)."""
    baseline = baseline_from_record(
        build_corpus_record(seed_turn_cost_corpus(_FIXTURE_ID)), threshold=_THRESHOLD
    )
    second = build_corpus_record(seed_turn_cost_corpus(_FIXTURE_ID))

    assert compare_turn_cost(baseline=baseline, record=second).verdict is TurnCostVerdict.OK


def test_turn_cost_harness_detects_injected_p90_regression() -> None:
    """A synthetic tail regression past the threshold reds the gate."""
    corpus = seed_turn_cost_corpus(_FIXTURE_ID)
    first = build_corpus_record(corpus)
    baseline = baseline_from_record(first, threshold=_THRESHOLD)

    second = build_corpus_record(_inject_p90_regression(corpus, factor=Decimal("2")))

    # Same fixture + harness revision on both passes: the refusal path must
    # not be what blocks here.
    assert (second.fixture_id, second.harness_revision) == (
        first.fixture_id,
        first.harness_revision,
    )
    assert second.p50_wall_clock_ms == first.p50_wall_clock_ms
    assert second.p50_cost_usd == first.p50_cost_usd
    assert second.p90_wall_clock_ms == 2 * first.p90_wall_clock_ms

    comparison = compare_turn_cost(baseline=baseline, record=second)
    assert comparison.verdict is TurnCostVerdict.REGRESSED
    assert comparison.wall_clock_regressed is True
    assert comparison.cost_regressed is True
    assert comparison.candidate_p90_wall_clock_ms == second.p90_wall_clock_ms


def test_turn_cost_harness_tolerates_injection_inside_the_threshold() -> None:
    """A tail rise under the recorded threshold is not a regression."""
    corpus = seed_turn_cost_corpus(_FIXTURE_ID)
    baseline = baseline_from_record(build_corpus_record(corpus), threshold=_THRESHOLD)

    second = build_corpus_record(_inject_p90_regression(corpus, factor=Decimal("1.05")))

    comparison = compare_turn_cost(baseline=baseline, record=second)
    assert comparison.verdict is TurnCostVerdict.OK
    assert comparison.candidate_p90_wall_clock_ms == 9_450


def test_turn_cost_harness_refuses_a_baseline_from_another_harness_revision() -> None:
    """A stale harness revision refuses instead of reading as a regression."""
    corpus = seed_turn_cost_corpus(_FIXTURE_ID)
    baseline = baseline_from_record(build_corpus_record(corpus), threshold=_THRESHOLD).model_copy(
        update={"harness_revision": "turn-cost-0"}
    )

    second = build_corpus_record(_inject_p90_regression(corpus, factor=Decimal("2")))

    comparison = compare_turn_cost(baseline=baseline, record=second)
    assert comparison.verdict is TurnCostVerdict.COMPARISON_INVALID
    assert comparison.mismatched_fields == ("harness_revision",)
