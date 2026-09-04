"""Turn-cost bench surface — corpus sourcing plus the recorded-threshold check.

The measurement itself lives in
:func:`eawf.observability.telemetry.turn_cost.build_turn_cost_record`; this module
supplies the two things a bench verb needs around it — *where the corpus
comes from* and *what it means to compare one record against a baseline*.

Two corpus sources exist:

- :func:`seed_turn_cost_corpus` replays a deterministic in-memory fixture.
  Replaying the same fixture id twice yields a byte-identical record, which
  is what makes a threshold check meaningful at all: without a fixed
  corpus, a moved p90 cannot be told apart from a different corpus.
- :func:`collect_live_corpus` joins the projected telemetry sessions onto
  the closed waves in ``state.json``. An empty result is an ordinary
  outcome rather than an error — a project that has never projected a
  session has nothing to measure, and saying so is more useful than
  raising.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.models import Wave
from eawf.observability.telemetry.models import RuntimeName
from eawf.observability.telemetry.turn_cost import (
    CompletedUnitRun,
    PriceSource,
    TurnCostRecord,
    build_turn_cost_record,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from eawf.kernel.state.models import State
    from eawf.observability.telemetry.models import TelemetrySession

logger = logging.getLogger(__name__)

__all__ = [
    "COMPARABILITY_FIELDS",
    "LIVE_FIXTURE_ID",
    "TURN_COST_FIXTURE_IDS",
    "TURN_COST_HARNESS_REVISION",
    "CorpusResolution",
    "TurnCostBaseline",
    "TurnCostComparison",
    "TurnCostCorpus",
    "TurnCostVerdict",
    "baseline_from_record",
    "build_corpus_record",
    "collect_live_corpus",
    "compare_turn_cost",
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

LIVE_FIXTURE_ID: Final[str] = "live"
"""Fixture id selecting the live corpus instead of a seeded one."""

_SMALL_FIXTURE_ID: Final[str] = "turn-cost-small"

TURN_COST_FIXTURE_IDS: Final[tuple[str, ...]] = (_SMALL_FIXTURE_ID,)
"""Every seeded fixture id :func:`seed_turn_cost_corpus` accepts."""

_FIXTURE_ITER_ID: Final[str] = "P00-I00"
_FIXTURE_OPENED_AT: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
_FIXTURE_UNIT_COUNT: Final[int] = 10
_FIXTURE_RUNTIME: Final[RuntimeName] = "claude"
_FIXTURE_MODEL: Final[str] = "fixture-model-v1"
_FIXTURE_PRICE_SOURCE: Final[PriceSource] = PriceSource(
    kind="fixture", pricing_version="fixture-v1"
)

_LIVE_PRICE_SOURCE: Final[PriceSource] = PriceSource(kind="session_rollup")
"""Provenance stamped on a run sourced from a projected session rollup.

``pricing_version`` stays ``None`` because the session row records the
projector's priced total without stamping which pricing snapshot produced
it. The cost is still summable — it was priced — so it is a priced row with
an unknown snapshot rather than an unpriced one.
"""


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
    waves: tuple[Wave, ...]
    runs: tuple[CompletedUnitRun, ...]


@dataclass(frozen=True, slots=True)
class CorpusResolution:
    """A resolved corpus, or the honest reason there is nothing to measure.

    Attributes:
        corpus: The measurable corpus, or ``None`` when there is nothing to
            measure. ``None`` is an ordinary outcome, not a failure.
        skipped_session_count: Sessions dropped because the source row could
            not be turned into a run (no wave, an unknown or still-open
            wave, a wave with no role, or no model). Surfaced so an empty
            result is visibly distinguishable from an unread one.
        reason: Why *corpus* is ``None``; ``None`` when a corpus was built.
    """

    corpus: TurnCostCorpus | None
    skipped_session_count: int
    reason: str | None


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
            cost_usd=Decimal("7.77"),
            price_source=None,
        ),
    ]
    return TurnCostCorpus(
        fixture_id=_SMALL_FIXTURE_ID,
        runtime=_FIXTURE_RUNTIME,
        model=_FIXTURE_MODEL,
        waves=waves,
        runs=tuple(runs),
    )


def collect_live_corpus(
    *,
    state: State,
    sessions: Sequence[TelemetrySession],
) -> CorpusResolution:
    """Join projected telemetry *sessions* onto the closed waves in *state*.

    A session becomes a run only when it names a closed wave that carries an
    agent role and the session records a model. Everything else is counted
    into ``skipped_session_count``: those are gaps in the source rows, not
    measurement facts, and the record's own counters already carry the
    measurement-level exclusions.

    Args:
        state: The loaded project state supplying the waves.
        sessions: Projected session rows from the telemetry cache.

    Returns:
        A :class:`CorpusResolution`. Its ``corpus`` is ``None`` when no session
        survived the join, or when the surviving runs span more than one
        ``(runtime, model)`` tuple — a percentile taken across models
        measures the mix rather than the work.
    """
    runs: list[CompletedUnitRun] = []
    waves: dict[str, Wave] = {}
    skipped = 0
    for session in sessions:
        run = _run_from_session(session, state=state)
        if run is None:
            skipped += 1
            continue
        runs.append(run)
        waves[run.wave_id] = state.waves[run.wave_id]

    if not runs:
        logger.debug(f"collect_live_corpus runs=0 skipped={skipped}")
        return CorpusResolution(
            corpus=None,
            skipped_session_count=skipped,
            reason="no projected session joins a closed wave with an agent role",
        )

    declared = sorted({(run.runtime, run.model) for run in runs})
    if len(declared) > 1:
        rendered = ", ".join(f"{runtime}/{model}" for runtime, model in declared)
        return CorpusResolution(
            corpus=None,
            skipped_session_count=skipped,
            reason=(
                f"live corpus spans {len(declared)} runtime/model tuples ({rendered}): "
                "a percentile across models measures the mix, not the work"
            ),
        )

    runtime, model = declared[0]
    return CorpusResolution(
        corpus=TurnCostCorpus(
            fixture_id=LIVE_FIXTURE_ID,
            runtime=runtime,
            model=model,
            waves=tuple(waves[wave_id] for wave_id in sorted(waves)),
            runs=tuple(runs),
        ),
        skipped_session_count=skipped,
        reason=None,
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


def _fixture_wave(index: int) -> Wave:
    """Return the *index*-th closed fixture wave."""
    return Wave(
        id=f"{_FIXTURE_ITER_ID}-W{index:02d}",
        iter_id=_FIXTURE_ITER_ID,
        title=f"Turn-cost fixture unit {index:02d}",
        status=WaveStatus.CLOSED,
        opened_at=_FIXTURE_OPENED_AT,
    )


def _fixture_run(
    *,
    run_id: str,
    wave_index: int,
    role: AgentSessionRole,
    wall_clock_ms: int,
    cost_usd: Decimal,
    price_source: PriceSource | None = _FIXTURE_PRICE_SOURCE,
) -> CompletedUnitRun:
    """Return one fixture run attributed to the *wave_index*-th fixture wave."""
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
        price_source=price_source,
    )


def _run_from_session(session: TelemetrySession, *, state: State) -> CompletedUnitRun | None:
    """Return the run *session* contributes, or ``None`` when it contributes none."""
    if session.wave_id is None or session.model_primary is None:
        return None
    wave = state.waves.get(session.wave_id)
    if wave is None or wave.status is not WaveStatus.CLOSED or wave.agent_role is None:
        return None
    return CompletedUnitRun(
        run_id=session.session_id,
        wave_id=session.wave_id,
        role=wave.agent_role,
        runtime=session.runtime,
        model=session.model_primary,
        wall_clock_ms=session.duration_ms if session.duration_ms is not None else 0,
        input_tokens=session.total_input_tokens,
        output_tokens=session.total_output_tokens,
        cache_read_tokens=session.total_cache_read,
        cache_write_tokens=session.total_cache_write,
        cost_usd=session.total_cost_usd,
        price_source=_LIVE_PRICE_SOURCE,
    )
