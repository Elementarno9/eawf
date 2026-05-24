"""Performance bench harness for ``eawf bench``.

The library half of the ``eawf bench`` noun-app (CLI dispatch lives at
:mod:`eawf.surfaces.cli.commands.bench` per AGENTS rule 1). Two concerns split
across two modules:

- :mod:`eawf.bench.seed` — deterministic corpus generation. A fixed
  size maps to a fixed RNG seed (``sha256("bench-fixture-v1-<size>")``)
  so re-seeding produces byte-identical output, which the bench
  baselines depend on for run-to-run comparability.
- :mod:`eawf.bench.harness` — the harness catalog plus the
  measure / compare logic. ``compare`` flags a regression when a
  harness's ``after`` wall-clock crosses ``before * (1 + threshold)``.

Per-OS thresholds and baselines live under ``.ea/bench/`` because
cross-OS wall-clock spread dwarfs any single sane threshold; the
defaults ship at ``.ea/bench/thresholds.yaml``.
"""

from __future__ import annotations

from eawf.bench.harness import (
    HARNESS_CATALOG,
    BenchResult,
    Comparison,
    HarnessSpec,
    compare_results,
    load_thresholds,
    run_harness,
    threshold_for_os,
)
from eawf.bench.seed import (
    FIXTURE_SIZES,
    FixtureSize,
    seed_corpus,
    seed_fixture,
)

__all__ = [
    "FIXTURE_SIZES",
    "HARNESS_CATALOG",
    "BenchResult",
    "Comparison",
    "FixtureSize",
    "HarnessSpec",
    "compare_results",
    "load_thresholds",
    "run_harness",
    "seed_corpus",
    "seed_fixture",
    "threshold_for_os",
]
