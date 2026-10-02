"""TrustView: one Milestone's verdicts, the jury's calibration and the producers' record.

The Trust route draws three truth-field groups, and each is derived here at read time
from what the route's projection carried, so nothing is stored beside the Milestone.

``verdicts`` are the verdict observations the Batches under the Milestone hold: one
independent audit verdict per criterion at the head each Batch delivers, keyed by site,
subject and the producer that reached it as ``(agent_role, runtime)``. Each is marked
with the authority the calibration gate returned, because a verification-site verdict
blocks only where calibration has earned it.

``calibration`` is the jury-validation report over the labelled cohort of native
verdicts, each joined to the outcome its subject went on to have or to the gold label a
principal pinned on it, and the authority the calibration gate returned for that report.
The daemon scores it when it serves the route and files it as one row beside the
verdicts; a projection that carries no such row has scored nothing, so the report reads
``INSUFFICIENT`` with every numeric field absent and the gate refuses on the cohort.

``track_record`` tallies each producer's verdicts that cleared their criterion against
those that did not. A producer that judged nothing has no rate: a rate over zero judged
attempts is undefined, never zero. Beside the tally each producer carries its own score as
a juror: how many of its verdicts the labelled cohort holds and their Brier score, which
the daemon files as one row per juror and which is absent while the juror's share of the
cohort is under the same floor the whole jury is held to.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from eawf.kernel.delivery.batch_proof import AuditVerdict
from eawf.kernel.projection.compute import CALIBRATION_KEY, JUROR_KEY_PREFIX
from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.eval.jury import JurorBallot
from eawf.observability.eval.jury_validation import (
    JuryValidationConfig,
    JuryValidationReport,
    JuryValidationStatus,
    LabeledVerdict,
    ValidationCohort,
    validate_jury,
)

logger = logging.getLogger(__name__)

#: The metric a calibration gate refuses on when the scored cohort is under its floor.
COHORT_METRIC: Final = "n"

#: The metric it refuses on when the jury's forecast is too poorly calibrated.
BRIER_METRIC: Final = "Brier"

#: The metric it refuses on when the jury waves known-bad subjects through, or was never
#: tested against one.
CO_ERROR_METRIC: Final = "co-error"

#: The authority a jury holds once every threshold cleared.
EARNED_AUTHORITY: Final = "blocking · earned"

#: What a producer part no record states reads as.
UNSTATED_PART: Final = "? unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationGroup:
    """The jury-validation report and the authority the calibration gate returns for it.

    Attributes:
        report: The report over the labelled cohort; its numeric fields are ``None``
            whenever it is ``INSUFFICIENT``.
        min_scored: The cohort floor the report is held against.
        authority: What the gate returned: ``blocking · earned`` or ``refused · <metric>``.
    """

    report: JuryValidationReport
    min_scored: int
    authority: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TrackRecordRow:
    """One producer's tally of verdicts that cleared their criterion and that did not.

    Attributes:
        agent_role: The role the producer ran as.
        runtime: The harness it ran under.
        accepted: Its verdicts that verified the criterion true.
        rejected: Its verdicts that verified the criterion false.
        scored: Its verdicts the labelled cohort holds, across every Batch.
        brier: Their Brier score; ``None`` while ``scored`` is under the cohort floor.
    """

    agent_role: str
    runtime: str
    accepted: int
    rejected: int
    scored: int = 0
    brier: float | None = None

    @property
    def judged(self) -> int:
        """Return how many of its verdicts judged a criterion either way."""
        return self.accepted + self.rejected

    @property
    def rate(self) -> float | None:
        """Return the accepted share of its judged verdicts; ``None`` over zero judged."""
        return self.accepted / self.judged if self.judged else None


@dataclass(frozen=True, slots=True, kw_only=True)
class JurorScore:
    """One juror's validation report over the labelled verdicts it cast.

    Attributes:
        agent_role: The role the juror ran as.
        runtime: The harness it ran under.
        report: The report over its share of the cohort, ``INSUFFICIENT`` under the floor.
    """

    agent_role: str
    runtime: str
    report: JuryValidationReport


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustView:
    """The Trust route's read model for one Milestone.

    Attributes:
        milestone: The Milestone the view is scoped to; ``None`` lists every verdict.
        verdicts: The verdict observations of the Milestone's Batches, in ledger order.
        unobserved: The subjects no verdict has been recorded for.
        calibration: The calibration report and the authority it earns.
        track_record: One row per producer, in the order its first verdict was listed.
    """

    milestone: str | None
    verdicts: tuple[RouteRecord, ...]
    unobserved: tuple[RouteRecord, ...]
    calibration: CalibrationGroup
    track_record: tuple[TrackRecordRow, ...]


def _stated(row: RouteRecord, name: str) -> str | None:
    """Return a row's stated fact, or ``None`` when the row does not state it."""
    field = row.field(name)
    return field.value if field.state is TruthState.KNOWN else None


def calibration_authority(
    report: JuryValidationReport, *, max_brier: float, max_co_error: float
) -> str:
    """Return the authority the calibration gate grants the jury on one report.

    The gate is pure: it reads the report and never recomputes a metric. A report that
    refused to score carries no number, and a cohort with no known-bad subject leaves the
    co-error rate undefined, so neither can clear a ceiling.

    Args:
        report: The jury-validation report over the labelled cohort.
        max_brier: The highest Brier score that still earns authority.
        max_co_error: The highest co-error rate that still earns authority.

    Returns:
        ``blocking · earned`` when every threshold cleared, else ``refused · <metric>``
        naming the first metric that did not, in the order cohort, Brier, co-error.
    """
    co_error = report.unanimous_pass_on_known_bad_rate
    if report.status is not JuryValidationStatus.SCORED:
        metric = COHORT_METRIC
    elif report.brier is None or report.brier > max_brier:
        metric = BRIER_METRIC
    elif co_error is None or co_error > max_co_error:
        metric = CO_ERROR_METRIC
    else:
        return EARNED_AUTHORITY
    return f"refused · {metric}"


def calibrate(
    cohort: ValidationCohort,
    ballots: Mapping[str, tuple[JurorBallot, ...]],
    *,
    max_brier: float,
    max_co_error: float,
    config: JuryValidationConfig | None = None,
) -> CalibrationGroup:
    """Return the calibration group over a labelled cohort of native verdicts.

    Args:
        cohort: The verdicts joined to their ground truth.
        ballots: The ballots each labelled verdict cast, by the verdict's key.
        max_brier: The Brier ceiling the gate holds the report to.
        max_co_error: The co-error ceiling the gate holds the report to.
        config: The jury-validation config; ``None`` takes its defaults.

    Returns:
        The report and the authority the gate returned for it.

    Raises:
        ValueError: A labelled verdict cast no ballot.
    """
    cfg = config if config is not None else JuryValidationConfig()
    report = validate_jury(cohort, ballots, cfg)
    authority = calibration_authority(report, max_brier=max_brier, max_co_error=max_co_error)
    return CalibrationGroup(report=report, min_scored=cfg.min_validation_n, authority=authority)


def score_jurors(
    cohort: ValidationCohort,
    ballots: Mapping[str, tuple[JurorBallot, ...]],
    config: JuryValidationConfig | None = None,
) -> tuple[JurorScore, ...]:
    """Return each juror's report over its own share of the labelled cohort.

    A juror is the ``(agent_role, runtime)`` that answered for a verdict, and each share
    is held to the same floor as the whole cohort, so a juror with too few settled
    verdicts reads ``INSUFFICIENT`` rather than a score drawn from a handful.

    Args:
        cohort: The verdicts joined to their ground truth.
        ballots: The ballots each labelled verdict cast, by the verdict's key.
        config: The jury-validation config; ``None`` takes its defaults.

    Returns:
        One score per juror, in the order its first labelled verdict was listed, silver
        rows before gold.

    Raises:
        ValueError: A labelled verdict cast no ballot.
    """
    shares: dict[tuple[str, str], tuple[list[LabeledVerdict], list[LabeledVerdict]]] = {}
    for tier, rows in enumerate((cohort.silver, cohort.gold)):
        for row in rows:
            juror = (row.outcome.agent_role.value, row.outcome.runtime)
            shares.setdefault(juror, ([], []))[tier].append(row)
    return tuple(
        JurorScore(
            agent_role=role,
            runtime=runtime,
            report=validate_jury(ValidationCohort(silver=silver, gold=gold), ballots, config),
        )
        for (role, runtime), (silver, gold) in shares.items()
    )


def _number(row: RouteRecord, name: str) -> float | None:
    """Return a calibration row's stated metric, or ``None`` when it states none."""
    value = _stated(row, name)
    return None if value is None else float(value)


def calibration_of(model: RouteReadModel) -> CalibrationGroup:
    """Return the calibration group the daemon filed beside the verdicts.

    Args:
        model: The Trust route's read model.

    Returns:
        The report and authority the calibration row states; with no such row, the
        empty cohort's ``INSUFFICIENT`` report, refused on the cohort.
    """
    row = next(
        (
            item
            for item in model.rows
            if item.collection is Epoch2Collection.BATCH and item.key == CALIBRATION_KEY
        ),
        None,
    )
    floor = JuryValidationConfig().min_validation_n
    if row is None:
        report = validate_jury(ValidationCohort(silver=[], gold=[]), {})
        return CalibrationGroup(
            report=report, min_scored=floor, authority=f"refused · {COHORT_METRIC}"
        )
    report = JuryValidationReport(
        n=int(_stated(row, "cohort") or 0),
        status=JuryValidationStatus(_stated(row, "status") or JuryValidationStatus.INSUFFICIENT),
        brier=_number(row, "brier"),
        unanimous_pass_on_known_bad_rate=_number(row, "co_error"),
        known_bad_n=int(_stated(row, "known_bad") or 0),
    )
    return CalibrationGroup(
        report=report,
        min_scored=int(_stated(row, "min_scored") or floor),
        authority=_stated(row, "authority") or f"refused · {COHORT_METRIC}",
    )


def juror_scores_of(model: RouteReadModel) -> Mapping[tuple[str, str], tuple[int, float | None]]:
    """Return the scored count and Brier score the daemon filed for each juror.

    Args:
        model: The Trust route's read model.

    Returns:
        ``(scored, brier)`` by ``(agent_role, runtime)``, in the order the rows were filed.
    """
    return {
        (
            _stated(row, "agent_role") or UNSTATED_PART,
            _stated(row, "runtime") or UNSTATED_PART,
        ): (int(_stated(row, "cohort") or 0), _number(row, "brier"))
        for row in model.rows
        if row.collection is Epoch2Collection.BATCH and row.key.startswith(JUROR_KEY_PREFIX)
    }


def track_record(
    verdicts: Iterable[RouteRecord],
    scores: Mapping[tuple[str, str], tuple[int, float | None]],
) -> tuple[TrackRecordRow, ...]:
    """Return each producer's tally over ``verdicts``, keyed ``(agent_role, runtime)``.

    An unverified verdict judged nothing, so it counts toward neither side. A juror the
    daemon scored but that answered for none of ``verdicts`` is listed after the rest with
    nothing tallied, because its score covers every Batch rather than these verdicts.

    Args:
        verdicts: The verdict observations to tally.
        scores: Each juror's scored count and Brier score, as :func:`juror_scores_of` reads.
    """
    tally: dict[tuple[str, str], list[int]] = {}
    for row in verdicts:
        key = (
            _stated(row, "agent_role") or UNSTATED_PART,
            _stated(row, "runtime") or UNSTATED_PART,
        )
        counts = tally.setdefault(key, [0, 0])
        verdict = _stated(row, "verdict")
        if verdict == AuditVerdict.VERIFIED_TRUE.value:
            counts[0] += 1
        elif verdict == AuditVerdict.VERIFIED_FALSE.value:
            counts[1] += 1
    for juror in scores:
        tally.setdefault(juror, [0, 0])
    return tuple(
        TrackRecordRow(
            agent_role=role,
            runtime=runtime,
            accepted=accepted,
            rejected=rejected,
            scored=scored,
            brier=brier,
        )
        for (role, runtime), (accepted, rejected) in tally.items()
        for scored, brier in (scores.get((role, runtime), (0, None)),)
    )


def build_trust_view(
    model: RouteReadModel,
    *,
    milestone: str | None,
) -> TrustView:
    """Return the Trust view of one Milestone from the route's read model.

    Args:
        model: The Trust route's read model: the verdict observations and the claims.
        milestone: The Milestone to scope the verdicts to; ``None`` keeps them all.

    Returns:
        The three groups, each derived from the rows the projection carried.
    """
    verdicts = tuple(
        row
        for row in model.rows
        if row.collection is Epoch2Collection.BATCH
        and row.key != CALIBRATION_KEY
        and not row.key.startswith(JUROR_KEY_PREFIX)
        and (milestone is None or _stated(row, "milestone") == milestone)
    )
    unobserved = tuple(row for row in model.rows if row.collection is Epoch2Collection.CLAIM)
    view = TrustView(
        milestone=milestone,
        verdicts=verdicts,
        unobserved=unobserved,
        calibration=calibration_of(model),
        track_record=track_record(verdicts, juror_scores_of(model)),
    )
    logger.debug(f"build_trust_view milestone={milestone} verdicts={len(verdicts)}")
    return view


__all__ = [
    "BRIER_METRIC",
    "COHORT_METRIC",
    "CO_ERROR_METRIC",
    "EARNED_AUTHORITY",
    "UNSTATED_PART",
    "CalibrationGroup",
    "JurorScore",
    "TrackRecordRow",
    "TrustView",
    "build_trust_view",
    "calibrate",
    "calibration_authority",
    "calibration_of",
    "juror_scores_of",
    "score_jurors",
    "track_record",
]
