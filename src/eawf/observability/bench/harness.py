"""Bench harness catalog + measure / compare logic.

A *harness* is a named, repeatable measurement of one operation against a
seeded corpus. ``run_harness`` times the operation across N iterations
and returns the best (minimum) wall-clock — the minimum is the stablest
estimator of the floor cost because it is the run least perturbed by
scheduler noise and GC pauses.

``compare_results`` is the regression gate: for each harness present in
both the *before* and *after* sets it flags a regression when

    after >= before * (1 + threshold)

The threshold is per-OS (Linux ±10%, macOS ±20%, Windows ±15%) because
cross-OS wall-clock spread dwarfs any single sane threshold. Defaults
ship at ``.ea/bench/thresholds.yaml``; the loader resolves the active
threshold for the running OS, with an explicit override taking
precedence.

Public API:
    run_harness(name, corpus, iterations) -> BenchResult
    run_all(corpus, iterations) -> list[BenchResult]
    load_thresholds(path) -> dict[str, float]
    threshold_for_os(thresholds, os_name) -> float
    compare_results(before, after, threshold) -> list[Comparison]
"""

from __future__ import annotations

import logging
import math
import platform
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from eawf.observability.bench.seed import BenchCorpus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """One entry in the harness catalog.

    Attributes:
        name: Stable harness id (snake_case) used on the wire + in
            baseline files.
        description: One-line human summary of what the harness measures.
        operation: Callable taking the corpus and performing the work
            once. Its return value is discarded; only wall-clock matters.
    """

    name: str
    description: str
    operation: Callable[[BenchCorpus], object]


@dataclass(frozen=True, slots=True)
class BenchResult:
    """The result of running one harness against one corpus.

    Attributes:
        name: The harness id.
        size: The corpus size the harness ran against.
        iterations: How many timed iterations were taken.
        best_ms: Minimum wall-clock across iterations, in milliseconds.
    """

    name: str
    size: str
    iterations: int
    best_ms: float


@dataclass(frozen=True, slots=True)
class Comparison:
    """One before/after comparison row from :func:`compare_results`.

    Attributes:
        name: The harness id compared.
        before_ms: Best wall-clock from the baseline run (ms).
        after_ms: Best wall-clock from the candidate run (ms).
        threshold: The fractional regression threshold applied.
        ratio: ``after_ms / before_ms`` (1.0 == no change).
        regressed: ``True`` when ``after_ms >= before_ms * (1 + threshold)``.
    """

    name: str
    before_ms: float
    after_ms: float
    threshold: float
    ratio: float
    regressed: bool


# --- Harness operations ----------------------------------------------------
# Each operation exercises a representative cost class against the seeded
# corpus. They stay dependency-light (pure-Python over the corpus dicts)
# so the harness can run anywhere the fixtures are present.


def _op_state_load_validate(corpus: BenchCorpus) -> object:
    """Re-serialise + re-parse the corpus state — a stand-in for the
    per-call state load + validate cost."""
    import orjson

    raw = orjson.dumps(corpus.state, option=orjson.OPT_SORT_KEYS)
    return orjson.loads(raw)


def _op_plan_view_render(corpus: BenchCorpus) -> object:
    """Render every wave to a one-line summary — the collection cost a
    plan-view table walk pays."""
    waves = corpus.state["waves"]
    assert isinstance(waves, list)
    return [f"{w['id']} {w['status']} {w['title']}" for w in waves]


def _op_event_append(corpus: BenchCorpus) -> object:
    """Serialise the full event stream — the projection-input cost a
    telemetry rebuild walks line by line."""
    import orjson

    return [orjson.dumps(ev, option=orjson.OPT_SORT_KEYS) for ev in corpus.events]


# Registry of every harness. Insertion order is the canonical listing
# order for ``eawf bench list``.
HARNESS_CATALOG: dict[str, HarnessSpec] = {
    "state_load_validate": HarnessSpec(
        name="state_load_validate",
        description="Re-serialise + re-parse state.json (per-call cost).",
        operation=_op_state_load_validate,
    ),
    "plan_view_render": HarnessSpec(
        name="plan_view_render",
        description="Render every wave row (collection cost).",
        operation=_op_plan_view_render,
    ),
    "event_append": HarnessSpec(
        name="event_append",
        description="Serialise the full event stream (projection cost).",
        operation=_op_event_append,
    ),
}


def run_harness(name: str, corpus: BenchCorpus, iterations: int = 50) -> BenchResult:
    """Run harness *name* against *corpus* and return the best wall-clock.

    The minimum across *iterations* is reported because it is the floor
    cost least perturbed by scheduler noise.

    Args:
        name: A key of :data:`HARNESS_CATALOG`.
        corpus: The seeded corpus to measure against.
        iterations: Number of timed runs (must be >= 1).

    Returns:
        A :class:`BenchResult` with the minimum wall-clock in ms.

    Raises:
        ValueError: When *name* is not a known harness, or *iterations*
            is below 1.
    """
    if name not in HARNESS_CATALOG:
        raise ValueError(f"unknown harness: {name!r} (want one of {sorted(HARNESS_CATALOG)})")
    if iterations < 1:
        raise ValueError(f"iterations must be >= 1, got {iterations}")

    op = HARNESS_CATALOG[name].operation
    best_ns = None
    for _ in range(iterations):
        start = time.perf_counter_ns()
        op(corpus)
        elapsed = time.perf_counter_ns() - start
        if best_ns is None or elapsed < best_ns:
            best_ns = elapsed

    # best_ns is set: the loop runs at least once (iterations >= 1).
    assert best_ns is not None
    best_ms = best_ns / 1_000_000
    logger.debug(f"run_harness name={name} size={corpus.size} best_ms={best_ms:.4f}")
    return BenchResult(name=name, size=corpus.size, iterations=iterations, best_ms=best_ms)


def run_all(corpus: BenchCorpus, iterations: int = 50) -> list[BenchResult]:
    """Run every catalog harness against *corpus* in catalog order.

    Args:
        corpus: The seeded corpus to measure against.
        iterations: Number of timed runs per harness.

    Returns:
        One :class:`BenchResult` per harness, in :data:`HARNESS_CATALOG`
        insertion order.
    """
    return [run_harness(name, corpus, iterations) for name in HARNESS_CATALOG]


# --- Threshold resolution --------------------------------------------------

# Fallback per-OS thresholds — used only when ``.ea/bench/thresholds.yaml``
# is absent. The committed YAML is the source of truth; these mirror it so
# a missing file degrades gracefully rather than crashing.
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "Linux": 0.10,
    "Darwin": 0.20,
    "Windows": 0.15,
}

# Map ``platform.system()`` values onto the YAML's friendly OS keys.
_OS_ALIASES: dict[str, str] = {
    "Linux": "linux",
    "Darwin": "macos",
    "Windows": "windows",
}


def load_thresholds(path: Path) -> dict[str, float]:
    """Load the per-OS threshold map from a YAML file.

    The YAML shape is::

        thresholds:
          linux: 0.10
          macos: 0.20
          windows: 0.15

    Args:
        path: Path to ``thresholds.yaml``.

    Returns:
        A mapping of friendly OS key (``linux`` / ``macos`` / ``windows``)
        to fractional threshold. When *path* is absent, the built-in
        defaults are returned under their friendly keys.

    Raises:
        ValueError: When the file exists but is not a mapping with a
            ``thresholds`` mapping, or a threshold is not a number.
    """
    if not path.exists():
        logger.debug(f"load_thresholds path={str(path)!r} missing=true using_defaults=true")
        return {alias: _DEFAULT_THRESHOLDS[osname] for osname, alias in _OS_ALIASES.items()}

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or "thresholds" not in parsed:
        raise ValueError(f"malformed thresholds file (want top-level 'thresholds' mapping): {path}")
    raw = parsed["thresholds"]
    if not isinstance(raw, dict):
        raise ValueError(f"'thresholds' must be a mapping in {path}")

    out: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(value, int | float):
            raise ValueError(f"threshold for {key!r} must be a number, got {value!r}")
        out[str(key)] = float(value)
    return out


def threshold_for_os(thresholds: dict[str, float], os_name: str | None = None) -> float:
    """Resolve the active threshold for *os_name*.

    Args:
        thresholds: Friendly-keyed map from :func:`load_thresholds`.
        os_name: ``platform.system()`` value (e.g. ``"Linux"``). When
            ``None`` the running platform is used.

    Returns:
        The fractional threshold for the OS. Falls back to the Linux
        default when the OS is unmapped or absent from *thresholds*.
    """
    system = os_name if os_name is not None else platform.system()
    alias = _OS_ALIASES.get(system, "linux")
    return thresholds.get(alias, _DEFAULT_THRESHOLDS.get(system, 0.10))


# --- Regression comparison -------------------------------------------------


def _index(results: Iterable[BenchResult]) -> dict[str, BenchResult]:
    """Index results by harness name (last write wins on duplicates)."""
    return {r.name: r for r in results}


def compare_results(
    before: Iterable[BenchResult],
    after: Iterable[BenchResult],
    threshold: float,
) -> list[Comparison]:
    """Compare two result sets and flag regressions.

    A harness regresses when ``after_ms >= before_ms * (1 + threshold)``.
    Only harnesses present in *both* sets are compared; harnesses unique
    to one side are skipped (the caller surfaces those separately).

    Args:
        before: Baseline results.
        after: Candidate results.
        threshold: Fractional regression threshold (e.g. ``0.10`` for
            10%). Must be >= 0.

    Returns:
        One :class:`Comparison` per shared harness, sorted by name.

    Raises:
        ValueError: When *threshold* is negative.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")

    before_idx = _index(before)
    after_idx = _index(after)
    shared = sorted(before_idx.keys() & after_idx.keys())

    comparisons: list[Comparison] = []
    for name in shared:
        b = before_idx[name].best_ms
        a = after_idx[name].best_ms
        # Guard div-by-zero: a zero baseline can only regress if the
        # candidate also rose above zero.
        ratio = (a / b) if b > 0 else (float("inf") if a > 0 else 1.0)
        # Compare the ratio against (1 + threshold) rather than computing
        # ``b * (1 + threshold)`` directly: the multiplication introduces
        # a representation artifact (e.g. 100.0 * 1.10 == 110.00000000001)
        # that drops an exactly-at-threshold regression. ``isclose`` pins
        # the boundary so ``after == before * (1 + threshold)`` counts as a
        # regression per the spec's ``>=``.
        limit = 1 + threshold
        regressed = ratio >= limit or math.isclose(ratio, limit, rel_tol=1e-9)
        comparisons.append(
            Comparison(
                name=name,
                before_ms=b,
                after_ms=a,
                threshold=threshold,
                ratio=ratio,
                regressed=regressed,
            )
        )
    return comparisons
