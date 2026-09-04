"""Performance bench harness for ``eawf bench``.

The library half of the ``eawf bench`` noun-app (CLI dispatch lives at
:mod:`eawf.surfaces.cli.commands.bench` per AGENTS rule 1). Three concerns
split across three modules:

- :mod:`eawf.observability.bench.seed` — deterministic corpus generation. A fixed
  size maps to a fixed RNG seed (``sha256("bench-fixture-v1-<size>")``)
  so re-seeding produces byte-identical output, which the bench
  baselines depend on for run-to-run comparability.
- :mod:`eawf.observability.bench.harness` — the harness catalog plus the
  measure / compare logic. ``compare`` flags a regression when a
  harness's ``after`` wall-clock crosses ``before * (1 + threshold)``.
- :mod:`eawf.observability.bench.turn_cost` — the turn-cost corpus sources
  (a deterministic fixture, or the live state x telemetry join) plus the
  baseline check that reads its threshold off the baseline artifact and
  refuses rather than rebaselines when the two are not comparable.

Per-OS thresholds and baselines live under ``.ea/bench/`` because
cross-OS wall-clock spread dwarfs any single sane threshold; the
defaults ship at ``.ea/bench/thresholds.yaml``. The turn-cost baseline is
a separate per-artifact file: its threshold is recorded *inside* the
baseline, not resolved per OS, because the number it guards is a cost and
a completed-unit clock rather than a machine-local microbenchmark.
"""

from __future__ import annotations

from eawf.observability.bench.harness import (
    HARNESS_CATALOG,
    BenchResult,
    Comparison,
    HarnessSpec,
    compare_results,
    load_thresholds,
    run_harness,
    threshold_for_os,
)
from eawf.observability.bench.seed import (
    FIXTURE_SIZES,
    FixtureSize,
    seed_corpus,
    seed_fixture,
)
from eawf.observability.bench.turn_cost import (
    LIVE_FIXTURE_ID,
    TURN_COST_FIXTURE_IDS,
    TURN_COST_HARNESS_REVISION,
    CorpusResolution,
    TurnCostBaseline,
    TurnCostComparison,
    TurnCostCorpus,
    TurnCostVerdict,
    baseline_from_record,
    build_corpus_record,
    collect_live_corpus,
    compare_turn_cost,
    load_baseline,
    seed_turn_cost_corpus,
    write_baseline,
)

__all__ = [
    "FIXTURE_SIZES",
    "HARNESS_CATALOG",
    "LIVE_FIXTURE_ID",
    "TURN_COST_FIXTURE_IDS",
    "TURN_COST_HARNESS_REVISION",
    "BenchResult",
    "Comparison",
    "CorpusResolution",
    "FixtureSize",
    "HarnessSpec",
    "TurnCostBaseline",
    "TurnCostComparison",
    "TurnCostCorpus",
    "TurnCostVerdict",
    "baseline_from_record",
    "build_corpus_record",
    "collect_live_corpus",
    "compare_results",
    "compare_turn_cost",
    "load_baseline",
    "load_thresholds",
    "run_harness",
    "seed_corpus",
    "seed_fixture",
    "seed_turn_cost_corpus",
    "threshold_for_os",
    "write_baseline",
]
