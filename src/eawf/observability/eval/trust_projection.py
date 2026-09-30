"""TrustView: one Milestone's verdicts, the jury's calibration and the producers' record.

The Trust route draws three truth-field groups, and each is derived here at read time
from what the route's projection carried, so nothing is stored beside the Milestone.

``verdicts`` are the verdict observations the Batches under the Milestone hold: one
independent audit verdict per criterion at the head each Batch delivers, keyed by site,
subject and the producer that reached it as ``(agent_role, runtime)``. Each is marked
with the authority the calibration gate returned, because a verification-site verdict
blocks only where calibration has earned it.

``calibration`` is the jury-validation report over the labelled cohort. The reducer is
the one the close gate scores against; it is handed the cohort of native verdicts some
ground truth labels, and no label names a native subject yet, so the report is
``INSUFFICIENT``, every numeric field is absent, and the gate refuses on the cohort size.

``track_record`` tallies each producer's verdicts that cleared their criterion against
those that did not. A producer that judged nothing has no rate: a rate over zero judged
attempts is undefined, never zero.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from eawf.kernel.delivery.batch_proof import AuditVerdict
from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.eval.jury_validation import (
    JuryValidationConfig,
    JuryValidationReport,
    JuryValidationStatus,
    ValidationCohort,
    validate_jury,
)

logger = logging.getLogger(__name__)

#: The metric a calibration gate refuses on when the scored cohort is under its floor.
COHORT_METRIC: Final = "n"

#: What a producer part no record states reads as.
UNSTATED_PART: Final = "? unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationGroup:
    """The jury-validation report and the authority the calibration gate returns for it.

    Attributes:
        report: The report over the labelled cohort; its numeric fields are ``None``
            whenever it is ``INSUFFICIENT``.
        min_scored: The cohort floor the report is held against.
        authority: What the gate returned: ``advisory`` or ``refused · <metric>``.
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
    """

    agent_role: str
    runtime: str
    accepted: int
    rejected: int

    @property
    def judged(self) -> int:
        """Return how many of its verdicts judged a criterion either way."""
        return self.accepted + self.rejected

    @property
    def rate(self) -> float | None:
        """Return the accepted share of its judged verdicts; ``None`` over zero judged."""
        return self.accepted / self.judged if self.judged else None


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


def calibrate(config: JuryValidationConfig | None = None) -> CalibrationGroup:
    """Return the calibration group over the labelled cohort of native verdicts.

    Args:
        config: The jury-validation config; ``None`` takes its defaults.

    Returns:
        The report and the gate's authority. No ground-truth label names a native
        verdict subject, so the cohort is empty and the report is ``INSUFFICIENT``.
    """
    cfg = config if config is not None else JuryValidationConfig()
    report = validate_jury(ValidationCohort(silver=[], gold=[]), {}, cfg)
    refused = report.status is JuryValidationStatus.INSUFFICIENT
    authority = f"refused · {COHORT_METRIC}" if refused else "advisory"
    return CalibrationGroup(report=report, min_scored=cfg.min_validation_n, authority=authority)


def track_record(verdicts: Iterable[RouteRecord]) -> tuple[TrackRecordRow, ...]:
    """Return each producer's tally over ``verdicts``, keyed ``(agent_role, runtime)``.

    An unverified verdict judged nothing, so it counts toward neither side.
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
    return tuple(
        TrackRecordRow(agent_role=role, runtime=runtime, accepted=accepted, rejected=rejected)
        for (role, runtime), (accepted, rejected) in tally.items()
    )


def build_trust_view(
    model: RouteReadModel,
    *,
    milestone: str | None,
    config: JuryValidationConfig | None = None,
) -> TrustView:
    """Return the Trust view of one Milestone from the route's read model.

    Args:
        model: The Trust route's read model: the verdict observations and the claims.
        milestone: The Milestone to scope the verdicts to; ``None`` keeps them all.
        config: The jury-validation config the calibration is held against.

    Returns:
        The three groups, each derived from the rows the projection carried.
    """
    verdicts = tuple(
        row
        for row in model.rows
        if row.collection is Epoch2Collection.BATCH
        and (milestone is None or _stated(row, "milestone") == milestone)
    )
    unobserved = tuple(row for row in model.rows if row.collection is Epoch2Collection.CLAIM)
    view = TrustView(
        milestone=milestone,
        verdicts=verdicts,
        unobserved=unobserved,
        calibration=calibrate(config),
        track_record=track_record(verdicts),
    )
    logger.debug(f"build_trust_view milestone={milestone} verdicts={len(verdicts)}")
    return view


__all__ = [
    "COHORT_METRIC",
    "UNSTATED_PART",
    "CalibrationGroup",
    "TrackRecordRow",
    "TrustView",
    "build_trust_view",
    "calibrate",
    "track_record",
]
