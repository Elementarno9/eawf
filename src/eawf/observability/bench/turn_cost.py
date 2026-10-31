"""Turn-cost bench surface — corpus sourcing plus the recorded-threshold check.

The measurement itself lives in
:func:`eawf.observability.telemetry.turn_cost.build_turn_cost_record`; this module
supplies the two things a bench verb needs around it — *where the corpus
comes from* and *what it means to compare one record against a baseline*.

A seeded corpus comes from :func:`seed_turn_cost_corpus`, which replays a
deterministic in-memory fixture. Replaying the same fixture id twice yields
a byte-identical record, which is what makes a threshold check meaningful at
all: without a fixed corpus, a moved p90 cannot be told apart from a
different corpus.

The ``live`` corpus comes from :func:`live_turn_cost_corpus`, which reads
the Task Runs the repository itself recorded. It moves as work lands, so it
answers whether the producer measures real Runs rather than whether a
number regressed.

The comparison is deliberately three-valued. A baseline recorded against a
different fixture or a different harness revision is **not** comparable to
the current record, so :data:`TurnCostVerdict.COMPARISON_INVALID` refuses
rather than either comparing incomparable numbers or silently adopting the
new record as the baseline. Silent rebaselining is the failure mode that
makes a regression gate useless: it turns every regression into the new
normal without anyone deciding to accept it.

The threshold is read from the baseline artifact rather than supplied at
check time so the tolerance a number was accepted under travels with that
number. All threshold arithmetic is ``Decimal``: an exactly-at-threshold
candidate must count as a regression, and binary-float multiplication
turns that boundary into a coin flip.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.epoch2.measurement import CounterName, MeasuredRuntime, Observed
from eawf.kernel.state.epoch2.run import Run, RunPurpose, RunStatus, TaskScope
from eawf.observability.reflect.runs import read_tree_run_records
from eawf.observability.telemetry.models import PriceSourceKind, RuntimeName
from eawf.observability.telemetry.turn_cost import (
    CompletedUnitRun,
    TurnCostRecord,
    WorkUnit,
    build_turn_cost_record,
)
from eawf.runtime.vcs.branch_name import main_checkout

logger = logging.getLogger(__name__)

__all__ = [
    "COMPARABILITY_FIELDS",
    "LIVE_FIXTURE_ID",
    "TURN_COST_FIXTURE_IDS",
    "TURN_COST_HARNESS_REVISION",
    "TurnCostBaseline",
    "TurnCostComparison",
    "TurnCostCorpus",
    "TurnCostVerdict",
    "baseline_from_record",
    "build_corpus_record",
    "compare_turn_cost",
    "live_turn_cost_corpus",
    "load_baseline",
    "seed_turn_cost_corpus",
    "write_baseline",
]


TURN_COST_HARNESS_REVISION: Final[str] = "turn-cost-1"
"""Revision stamp of the corpus-sourcing + record-building code here.

Bump it whenever a change to this module can move a record built from an
unchanged corpus. The check refuses across revisions, so a stale baseline
surfaces as an explicit refusal instead of a phantom regression.
"""

_SMALL_FIXTURE_ID: Final[str] = "turn-cost-small"

TURN_COST_FIXTURE_IDS: Final[tuple[str, ...]] = (_SMALL_FIXTURE_ID,)
"""Every seeded fixture id :func:`seed_turn_cost_corpus` accepts."""

LIVE_FIXTURE_ID: Final[str] = "live"
"""The fixture id of the corpus :func:`live_turn_cost_corpus` reads from the tree."""

#: The record runtime each harness a Run is captured under reports as.
_LIVE_RUNTIMES: Final[Mapping[str, RuntimeName]] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}

#: The agent role a Run's purpose is charged under, total over
#: :class:`RunPurpose` so a new purpose fails here instead of going unattributed.
_PURPOSE_ROLES: Final[Mapping[RunPurpose, AgentSessionRole]] = {
    RunPurpose.IMPLEMENT: AgentSessionRole.EXECUTOR,
    RunPurpose.INTEGRATE: AgentSessionRole.EXECUTOR,
    RunPurpose.REPAIR: AgentSessionRole.EXECUTOR,
    RunPurpose.REVIEW: AgentSessionRole.REVIEWER,
    RunPurpose.AUDIT: AgentSessionRole.AUDITOR,
    RunPurpose.RESEARCH: AgentSessionRole.RESEARCHER,
    RunPurpose.PLAN: AgentSessionRole.PLANNER,
    RunPurpose.OBSERVE: AgentSessionRole.OPERATOR,
}

#: Run states that leave the Task's work unfinished.
_UNFINISHED_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.SUSPENDED}
)

_FIXTURE_PHASE_ID: Final[str] = "P00"
# The fixture unit ids keep the P<NN>-I<NN>-W<NN> shape the seeded corpus has always used.
_FIXTURE_ITER_ID: Final[str] = f"{_FIXTURE_PHASE_ID}-I00"
_FIXTURE_UNIT_COUNT: Final[int] = 10
_FIXTURE_RUNTIME: Final[RuntimeName] = "claude"
_FIXTURE_MODEL: Final[str] = "fixture-model-v1"
_FIXTURE_RATE_TABLE_VERSION: Final[str] = "fixture-v1"


class TurnCostVerdict(StrEnum):
    """Outcome of checking a turn-cost record against a baseline.

    Values:
        OK: Every compared percentile stayed inside the recorded threshold.
        REGRESSED: At least one percentile crossed it.
        COMPARISON_INVALID: The two artifacts describe different
            measurements, so no comparison was attempted.
    """

    OK = "ok"
    REGRESSED = "regressed"
    COMPARISON_INVALID = "comparison_invalid"


#: Record fields that must agree before two records may be compared. A
#: difference in any of them means the baseline measured something else.
COMPARABILITY_FIELDS: Final[tuple[str, ...]] = (
    "fixture_id",
    "harness_revision",
    "runtime",
    "model",
)


class TurnCostBaseline(BaseModel):
    """An accepted turn-cost measurement plus the tolerance it was accepted under.

    Attributes:
        fixture_id: Fixture the baseline was measured from.
        harness_revision: Harness revision that produced it.
        runtime: Runtime the measurement was declared against.
        model: Model id the measurement was declared against.
        threshold: Fractional tolerance recorded with the baseline (``0.10``
            for +10%). Read at check time so the tolerance travels with the
            number it guards.
        p90_wall_clock_ms: Accepted 90th-percentile per-unit wall clock.
        p90_cost_usd: Accepted 90th-percentile per-unit cost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(min_length=1)
    harness_revision: str = Field(min_length=1)
    runtime: RuntimeName
    model: str = Field(min_length=1)
    threshold: Decimal = Field(ge=Decimal("0"))
    p90_wall_clock_ms: int = Field(ge=0)
    p90_cost_usd: Decimal = Field(ge=Decimal("0"))


class TurnCostComparison(BaseModel):
    """One baseline-versus-candidate turn-cost check.

    On :data:`TurnCostVerdict.COMPARISON_INVALID` the percentile fields are
    left unset: reporting a ratio between incomparable measurements would
    invite exactly the reading the refusal exists to prevent.

    Attributes:
        verdict: The three-valued outcome.
        threshold: The tolerance read off the baseline.
        mismatched_fields: Comparability fields that differ, in
            :data:`COMPARABILITY_FIELDS` order. Empty unless the verdict is
            ``COMPARISON_INVALID``.
        baseline_p90_wall_clock_ms: Baseline p90 wall clock, when compared.
        candidate_p90_wall_clock_ms: Candidate p90 wall clock, when compared.
        baseline_p90_cost_usd: Baseline p90 cost, when compared.
        candidate_p90_cost_usd: Candidate p90 cost, when compared.
        wall_clock_regressed: Whether wall clock crossed the threshold.
        cost_regressed: Whether cost crossed the threshold.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: TurnCostVerdict
    threshold: Decimal = Field(ge=Decimal("0"))
    mismatched_fields: tuple[str, ...] = ()
    baseline_p90_wall_clock_ms: int | None = None
    candidate_p90_wall_clock_ms: int | None = None
    baseline_p90_cost_usd: Decimal | None = None
    candidate_p90_cost_usd: Decimal | None = None
    wall_clock_regressed: bool = False
    cost_regressed: bool = False


@dataclass(frozen=True, slots=True)
class TurnCostCorpus:
    """A measurable corpus: closed waves, their runs, and the declared tuple.

    Attributes:
        fixture_id: Id the resulting record is stamped with.
        runtime: Runtime the measurement is declared against.
        model: Model id the measurement is declared against.
        waves: Candidate waves (only ``CLOSED`` ones become units).
        runs: Runs attributed to those waves.
    """

    fixture_id: str
    runtime: RuntimeName
    model: str
    waves: tuple[WorkUnit, ...]
    runs: tuple[CompletedUnitRun, ...]


def seed_turn_cost_corpus(fixture_id: str) -> TurnCostCorpus:
    """Return the deterministic corpus for *fixture_id*.

    The corpus is constructed rather than randomised, so two calls produce
    equal records down to the last ``Decimal`` digit.

    The ``turn-cost-small`` fixture is ten closed waves whose per-unit wall
    clock runs 1000..10000 ms and whose per-unit executor cost runs
    0.01..0.10 USD. The first wave additionally carries three
    zero-wall-clock runs that exercise every exclusion the producer
    implements: an auditor run (verification rather than execution), an
    operator run (unattributed), and a run with no price source (unpriced).

    Args:
        fixture_id: One of :data:`TURN_COST_FIXTURE_IDS`.

    Returns:
        The seeded corpus.

    Raises:
        ValueError: When *fixture_id* is not a known fixture.
    """
    if fixture_id != _SMALL_FIXTURE_ID:
        raise ValueError(
            f"unknown turn-cost fixture: {fixture_id!r} (want one of {list(TURN_COST_FIXTURE_IDS)})"
        )

    waves = tuple(_fixture_wave(index) for index in range(1, _FIXTURE_UNIT_COUNT + 1))
    runs: list[CompletedUnitRun] = [
        _fixture_run(
            run_id=f"exec-{index:02d}",
            wave_index=index,
            role=AgentSessionRole.EXECUTOR,
            wall_clock_ms=index * 1_000,
            cost_usd=Decimal("0.01") * index,
        )
        for index in range(1, _FIXTURE_UNIT_COUNT + 1)
    ]
    runs += [
        _fixture_run(
            run_id="audit-01",
            wave_index=1,
            role=AgentSessionRole.AUDITOR,
            wall_clock_ms=0,
            cost_usd=Decimal("0.05"),
        ),
        _fixture_run(
            run_id="operator-01",
            wave_index=1,
            role=AgentSessionRole.OPERATOR,
            wall_clock_ms=0,
            cost_usd=Decimal("9.99"),
        ),
        _fixture_run(
            run_id="unpriced-01",
            wave_index=1,
            role=AgentSessionRole.EXECUTOR,
            wall_clock_ms=0,
            cost_usd=None,
        ),
    ]
    return TurnCostCorpus(
        fixture_id=_SMALL_FIXTURE_ID,
        runtime=_FIXTURE_RUNTIME,
        model=_FIXTURE_MODEL,
        waves=waves,
        runs=tuple(runs),
    )


def live_turn_cost_corpus(state_dir: Path) -> TurnCostCorpus:
    """Return the corpus of the Task Runs the repository recorded.

    The Runs are read from the main checkout of the repository *state_dir*
    belongs to: a linked worktree, such as the pinned checkout a release
    proof runs in, carries no machine-local Run ledger of its own. Reading
    takes no lock and writes nothing.

    A unit is one Task, closed once none of its Runs is still queued,
    running or suspended. Only Runs that ended with a measured capture
    carry a wall clock and a cost, so only they are measured, and only
    those on the most common harness and model: the record declares one
    runtime and one model, and a Run on another is another measurement.
    A tie goes to the greater harness and model, so it falls the same way
    every time.

    Args:
        state_dir: The invocation's ``.ea`` directory.

    Returns:
        The live corpus, stamped :data:`LIVE_FIXTURE_ID`.

    Raises:
        ValueError: When the tree holds no ended Task Run with a measured
            capture on a known harness, so there is nothing to measure.
        pydantic.ValidationError: When a stored Run does not validate.
    """
    tree_root = main_checkout(state_dir.parent) / state_dir.name
    runs = read_tree_run_records(tree_root)
    task_runs = [run for run in runs if isinstance(run.scope, TaskScope)]
    measured = [
        (run, run.captured_runtime)
        for run in task_runs
        if isinstance(run.captured_runtime, MeasuredRuntime)
        and run.captured_runtime.harness in _LIVE_RUNTIMES
        and run.started_at is not None
        and run.ended_at is not None
    ]
    if not measured:
        raise ValueError(
            f"live turn-cost corpus is empty: no ended Task Run carries a measured capture "
            f"in {tree_root} ({len(runs)} Run(s) read); dispatch a Task through the daemon"
        )
    tuples = Counter((captured.harness, captured.model) for _, captured in measured)
    harness, model = max(tuples, key=lambda pair: (tuples[pair], pair))
    unfinished = {_task_key(run) for run in task_runs if run.status in _UNFINISHED_RUN_STATUSES}
    selected = [
        (run, captured)
        for run, captured in measured
        if (captured.harness, captured.model) == (harness, model)
    ]
    units = tuple(
        WorkUnit(id=key, status=WaveStatus.IN_PROGRESS if key in unfinished else WaveStatus.CLOSED)
        for key in sorted({_task_key(run) for run, _ in selected})
    )
    logger.info(
        f"live_turn_cost_corpus runs={len(runs)} measured={len(measured)} "
        f"selected={len(selected)} units={len(units)} harness={harness!r} model={model!r}"
    )
    return TurnCostCorpus(
        fixture_id=LIVE_FIXTURE_ID,
        runtime=_LIVE_RUNTIMES[harness],
        model=model,
        waves=units,
        runs=tuple(
            _live_run(run, captured, runtime=_LIVE_RUNTIMES[harness]) for run, captured in selected
        ),
    )


def build_corpus_record(
    corpus: TurnCostCorpus,
    *,
    harness_revision: str = TURN_COST_HARNESS_REVISION,
) -> TurnCostRecord:
    """Run *corpus* through the completed-unit producer.

    Args:
        corpus: The corpus to measure.
        harness_revision: Revision stamp written onto the record.

    Returns:
        The :class:`TurnCostRecord` for *corpus*.

    Raises:
        KeyError: When a run names a wave absent from the corpus.
        ValueError: When no closed wave carries a run, or a run has no role.
    """
    return build_turn_cost_record(
        waves=corpus.waves,
        runs=corpus.runs,
        fixture_id=corpus.fixture_id,
        harness_revision=harness_revision,
        runtime=corpus.runtime,
        model=corpus.model,
    )


def baseline_from_record(record: TurnCostRecord, *, threshold: Decimal) -> TurnCostBaseline:
    """Return the baseline artifact accepting *record* at *threshold*.

    Args:
        record: The measurement being accepted as the new baseline.
        threshold: Fractional tolerance to record alongside it.

    Returns:
        The :class:`TurnCostBaseline` to persist.

    Raises:
        ValueError: When *threshold* is negative.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")
    return TurnCostBaseline(
        fixture_id=record.fixture_id,
        harness_revision=record.harness_revision,
        runtime=record.runtime,
        model=record.model,
        threshold=threshold,
        p90_wall_clock_ms=record.p90_wall_clock_ms,
        p90_cost_usd=record.p90_cost_usd,
    )


def compare_turn_cost(
    *,
    baseline: TurnCostBaseline,
    record: TurnCostRecord,
) -> TurnCostComparison:
    """Check *record* against *baseline* under the baseline's own threshold.

    Comparability is checked first and short-circuits: when the two
    artifacts disagree on any of :data:`COMPARABILITY_FIELDS` the verdict is
    ``COMPARISON_INVALID`` and no percentile is compared. Otherwise a
    percentile regresses when it rose *and*
    ``candidate >= baseline * (1 + threshold)`` — ``>=`` so an
    exactly-at-threshold candidate blocks, while an unchanged replay passes
    even at a zero tolerance.

    Args:
        baseline: The accepted measurement plus its recorded tolerance.
        record: The candidate measurement.

    Returns:
        The :class:`TurnCostComparison`.
    """
    mismatched = tuple(
        field
        for field in COMPARABILITY_FIELDS
        if getattr(baseline, field) != getattr(record, field)
    )
    if mismatched:
        logger.debug(f"compare_turn_cost verdict=comparison_invalid fields={list(mismatched)}")
        return TurnCostComparison(
            verdict=TurnCostVerdict.COMPARISON_INVALID,
            threshold=baseline.threshold,
            mismatched_fields=mismatched,
        )

    wall_clock_regressed = _regressed(
        baseline=Decimal(baseline.p90_wall_clock_ms),
        candidate=Decimal(record.p90_wall_clock_ms),
        threshold=baseline.threshold,
    )
    cost_regressed = _regressed(
        baseline=baseline.p90_cost_usd,
        candidate=record.p90_cost_usd,
        threshold=baseline.threshold,
    )
    regressed = wall_clock_regressed or cost_regressed
    return TurnCostComparison(
        verdict=TurnCostVerdict.REGRESSED if regressed else TurnCostVerdict.OK,
        threshold=baseline.threshold,
        baseline_p90_wall_clock_ms=baseline.p90_wall_clock_ms,
        candidate_p90_wall_clock_ms=record.p90_wall_clock_ms,
        baseline_p90_cost_usd=baseline.p90_cost_usd,
        candidate_p90_cost_usd=record.p90_cost_usd,
        wall_clock_regressed=wall_clock_regressed,
        cost_regressed=cost_regressed,
    )


def load_baseline(path: Path) -> TurnCostBaseline:
    """Read a baseline artifact from *path*.

    Args:
        path: Path to a JSON file holding one baseline object.

    Returns:
        The parsed :class:`TurnCostBaseline`.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ValueError: When the file is not JSON, or not a valid baseline
            (``pydantic.ValidationError`` is a ``ValueError`` subclass).
    """
    if not path.exists():
        raise FileNotFoundError(f"turn-cost baseline not found: {path}")
    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as exc:
        raise ValueError(f"malformed turn-cost baseline JSON in {path}: {exc}") from exc
    return TurnCostBaseline.model_validate(payload)


def write_baseline(baseline: TurnCostBaseline, path: Path) -> None:
    """Write *baseline* to *path*, creating parent directories.

    Args:
        baseline: The artifact to persist.
        path: Destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = orjson.dumps(
        baseline.model_dump(mode="json"),
        option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
    )
    path.write_bytes(raw + b"\n")


# --- Internal helpers ------------------------------------------------------


def _regressed(*, baseline: Decimal, candidate: Decimal, threshold: Decimal) -> bool:
    """Return whether *candidate* crossed *baseline* by more than *threshold*.

    A measurement that did not rise is never a regression, whatever the
    tolerance — without that first clause a zero tolerance would red on an
    unchanged replay, since ``candidate >= baseline * 1`` holds trivially.
    A rise off a zero baseline always is one: no ratio against zero exists,
    so there is no tolerance to be inside of.

    The arithmetic stays in ``Decimal`` end to end because ``100 * 1.10`` in
    binary float is ``110.00000000000001``, which silently lets through a
    candidate sitting exactly on the threshold. The comparison is ``>=`` so
    that boundary blocks, matching
    :func:`eawf.observability.bench.harness.compare_results`.
    """
    if candidate <= baseline:
        return False
    if baseline == 0:
        return True
    return candidate >= baseline * (Decimal(1) + threshold)


def _task_key(run: Run) -> str:
    """Return the key of the Task a task-scoped *run* served."""
    assert isinstance(run.scope, TaskScope)
    return run.scope.task_ref.entity_key


def _live_run(run: Run, captured: MeasuredRuntime, *, runtime: RuntimeName) -> CompletedUnitRun:
    """Return *run*'s measured share as one corpus run of its Task.

    A shared session's counters are fractional shares, so tokens are
    rounded half-even to whole counts. A cost the capture did not observe
    is unpriced rather than zero; an observed one is the runtime's own
    charge, so it is billed.
    """
    assert run.started_at is not None and run.ended_at is not None
    reading = captured.counters[CounterName.COST_USD]
    cost = reading.value if isinstance(reading, Observed) else None
    return CompletedUnitRun(
        run_id=run.key,
        wave_id=_task_key(run),
        role=_PURPOSE_ROLES[run.scope.purpose],
        runtime=runtime,
        model=captured.model,
        wall_clock_ms=(run.ended_at - run.started_at) // timedelta(milliseconds=1),
        input_tokens=_tokens(captured, CounterName.INPUT_TOKENS),
        output_tokens=_tokens(captured, CounterName.OUTPUT_TOKENS),
        cache_read_tokens=_tokens(captured, CounterName.CACHE_READ_INPUT_TOKENS),
        cache_write_tokens=_tokens(captured, CounterName.CACHE_CREATION_INPUT_TOKENS),
        cost_usd=cost,
        price_source=PriceSourceKind.UNPRICED if cost is None else PriceSourceKind.BILLED,
    )


def _tokens(captured: MeasuredRuntime, name: CounterName) -> int:
    """Return counter *name* as a whole token count; zero when unobserved."""
    reading = captured.counters[name]
    if not isinstance(reading, Observed):
        return 0
    return int(reading.value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _fixture_wave(index: int) -> WorkUnit:
    """Return the *index*-th closed fixture unit."""
    return WorkUnit(id=f"{_FIXTURE_ITER_ID}-W{index:02d}", status=WaveStatus.CLOSED)


def _fixture_run(
    *,
    run_id: str,
    wave_index: int,
    role: AgentSessionRole,
    wall_clock_ms: int,
    cost_usd: Decimal | None,
) -> CompletedUnitRun:
    """Return one fixture run attributed to the *wave_index*-th fixture wave.

    A run with a cost is list-reconstructed at the fixture rate table; a run
    with none is unpriced.
    """
    priced = cost_usd is not None
    return CompletedUnitRun(
        run_id=run_id,
        wave_id=f"{_FIXTURE_ITER_ID}-W{wave_index:02d}",
        role=role,
        runtime=_FIXTURE_RUNTIME,
        model=_FIXTURE_MODEL,
        wall_clock_ms=wall_clock_ms,
        input_tokens=100 * wave_index,
        output_tokens=10 * wave_index,
        cost_usd=cost_usd,
        price_source=PriceSourceKind.LIST_RECONSTRUCTED if priced else PriceSourceKind.UNPRICED,
        rate_table_version=_FIXTURE_RATE_TABLE_VERSION if priced else None,
    )
