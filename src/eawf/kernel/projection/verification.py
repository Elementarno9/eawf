"""The verification read models: what trust, evidence and health draw at one cursor.

Four routes answer the one question an operator asks of a verification surface: what is
believed here, and on whose say-so. ``trust`` renders the claims a milestone rests on,
``evidence`` the claim and the rungs under it, ``evidence.digest`` one rung's record, and
``health`` the conformance verdicts a runtime tuple carries.

Health is the route where an honest absence matters most. Doctor observes nothing about a
runtime tuple itself: every verdict is one the conformance runner already reached, so a
row states the stage it was reached at, the daemon verb that wrote the stage record and
the artifact that stage filed its evidence under. A verdict that carries no provenance
renders the unknown token rather than a passing cell, because a health surface that draws
a gate as green when its evidence is absent is worse than one that draws nothing.

A quarantined tuple names its trigger through the failure code its stage record was filed
under. The code is the durable fact, and several triggers share one code where they are
the same check failing, so the row names every trigger consistent with the code rather
than picking one of them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    known_field,
    status_and,
    unknown_field,
    unstated,
)
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.runtime.certification import CertificationFailureCode, QuarantineTrigger
from eawf.observability.doctor.models import CheckResult, CheckStatus
from eawf.runtime.runtimes.quarantine import TRIGGER_FAILURE_CODE

logger = logging.getLogger(__name__)

#: The family name a refusal from this module names.
FAMILY: Final = "verification"

#: The console routes this module states a read model for. Every one is bound by
#: :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`, so every one is served by
#: ``projection.<route>.read`` and by ``projection.<route>.reconnect``.
VERIFICATION_ROUTES: Final[tuple[str, ...]] = (
    "trust",
    "evidence",
    "evidence.digest",
    "health",
)

#: The route whose rows are conformance verdicts rather than document records.
HEALTH_ROUTE: Final = "health"

#: The daemon verb a failed rollback is filed under. A tuple whose newest effective stage
#: was written by this verb is out of service, which is what makes a row quarantined.
QUARANTINE_PRODUCER: Final = "conformance.quarantine"

#: Why a verdict states no stage, producer or evidence artifact.
NO_PROVENANCE_REASON: Final = "the verdict names no stage record, so nothing traces it"

#: Why a quarantined row still names no trigger.
NO_TRIGGER_REASON: Final = "no failure code was recorded, so no trigger is named"

#: Why a row that is not quarantined names no trigger at all.
NOT_QUARANTINED_REASON: Final = "the tuple is in service, so no trigger fired"

#: The producer revision every verdict cell states. A conformance verdict is never
#: revised: the next stage record supersedes it, so the first revision is the only one a
#: single verdict ever stands at.
VERDICT_REVISION: Final = 1

#: What each verification route renders per row, in column order. The first field of
#: every route is the status the document states; the rest are declared columns whose
#: producers are later items, so the console draws the unknown token in their place.
VERIFICATION_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        "trust": status_and(unstated("verdict"), unstated("answered_by"), unstated("freshness")),
        "evidence": status_and(unstated("outcome"), unstated("checked"), unstated("as_of")),
        "evidence.digest": status_and(unstated("found"), unstated("input_digest")),
        "health": status_and(unstated("checked_at")),
    }
)


check_field_tables(family=FAMILY, routes=VERIFICATION_ROUTES, fields=VERIFICATION_FIELDS)


def _invert_trigger_codes() -> Mapping[CertificationFailureCode, tuple[QuarantineTrigger, ...]]:
    """Return the triggers filed under each failure code, in trigger declaration order."""
    found: dict[CertificationFailureCode, list[QuarantineTrigger]] = {}
    for trigger in QuarantineTrigger:
        found.setdefault(TRIGGER_FAILURE_CODE[trigger], []).append(trigger)
    return MappingProxyType({code: tuple(triggers) for code, triggers in found.items()})


#: Every quarantine trigger a failure code can have come from. The map is derived from
#: the forward table so the two cannot drift, and a code names two triggers where the two
#: are one check failing.
TRIGGERS_BY_FAILURE_CODE: Final[
    Mapping[CertificationFailureCode, tuple[QuarantineTrigger, ...]]
] = _invert_trigger_codes()


def triggers_for_code(code: CertificationFailureCode) -> tuple[QuarantineTrigger, ...]:
    """Return every quarantine trigger recorded under ``code``.

    Args:
        code: The failure code a stage record was filed under.

    Returns:
        The triggers consistent with the code, in trigger declaration order; empty for a
        code no trigger files under, which is a refusal the conformance runner reached
        without a quarantine.
    """
    return TRIGGERS_BY_FAILURE_CODE.get(code, ())


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeTupleVerdict:
    """One runtime tuple's newest conformance verdict, as the health route receives it.

    Attributes:
        check: The doctor check repeating the stage record, which carries the stage, the
            producing verb and the evidence artifact when one was recorded.
        reason_code: The failure code the stage record was filed under; ``None`` for a
            stage that passed, and for a verdict whose code was not carried through.
    """

    check: CheckResult
    reason_code: CertificationFailureCode | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeTupleRow:
    """One runtime tuple as the health frame draws it.

    Attributes:
        check: The check's stable name, which names the tuple by its digest.
        status: What the verdict grades as: ``ok``, ``warn`` or ``fail``.
        detail: The check's own message, verbatim.
        stage: The conformance stage the verdict was reached at.
        producer: The daemon verb that wrote the stage record.
        evidence_ref: The artifact that stage filed its evidence under.
        quarantined: Whether the tuple is out of service.
        triggers: Every trigger consistent with the recorded failure code; empty when the
            tuple is in service or no code was recorded.
        trigger: The triggers as one truth cell, unknown when none is named.
    """

    check: str
    status: CheckStatus
    detail: str
    stage: TruthField[str]
    producer: TruthField[str]
    evidence_ref: TruthField[str]
    quarantined: bool
    triggers: tuple[QuarantineTrigger, ...] = ()
    trigger: TruthField[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthReadModel(RouteReadModel):
    """The health route's read model: the projection's rows and the tuple verdicts.

    Attributes:
        tuples: One row per runtime tuple the caller held a verdict for, in the order the
            verdicts arrived. Empty when no conformance verdict is held, which the frame
            says rather than drawing a healthy tuple nobody observed.
    """

    tuples: tuple[RuntimeTupleRow, ...] = ()

    def quarantined(self) -> tuple[RuntimeTupleRow, ...]:
        """Return the tuple rows that are out of service, in row order."""
        return tuple(row for row in self.tuples if row.quarantined)


def _cell(value: str | None, *, urn: str, reason: str) -> TruthField[str]:
    """Return one verdict cell: the value when it was recorded, the unknown token when not."""
    if value is None or not value.strip():
        return unknown_field(urn=urn, revision=VERDICT_REVISION, reason=reason)
    return known_field(value=value, urn=urn, revision=VERDICT_REVISION)


def _tuple_row(verdict: RuntimeTupleVerdict) -> RuntimeTupleRow:
    """Return one health row for one conformance verdict."""
    check = verdict.check
    provenance = check.provenance
    # the row rests on the artifact the stage filed, and on the check itself when the
    # verdict names no stage record at all
    urn = str(provenance.evidence_ref) if provenance is not None else check.name
    quarantined = provenance is not None and provenance.producer == QUARANTINE_PRODUCER
    code = verdict.reason_code
    triggers = triggers_for_code(code) if quarantined and code is not None else ()
    named = " · ".join(trigger.value for trigger in triggers)
    return RuntimeTupleRow(
        check=check.name,
        status=check.status,
        detail=check.detail or "",
        stage=_cell(provenance.stage if provenance else None, urn=urn, reason=NO_PROVENANCE_REASON),
        producer=_cell(
            provenance.producer if provenance else None, urn=urn, reason=NO_PROVENANCE_REASON
        ),
        evidence_ref=_cell(
            str(provenance.evidence_ref) if provenance else None,
            urn=urn,
            reason=NO_PROVENANCE_REASON,
        ),
        quarantined=quarantined,
        triggers=triggers,
        trigger=_cell(
            named or None,
            urn=urn,
            reason=NO_TRIGGER_REASON if quarantined else NOT_QUARANTINED_REASON,
        ),
    )


def build_runtime_tuple_rows(
    verdicts: Sequence[RuntimeTupleVerdict],
) -> tuple[RuntimeTupleRow, ...]:
    """Return one health row per conformance verdict.

    A verdict with no provenance is the case this exists for: the stage, the producing
    verb and the evidence artifact all render the unknown token naming why, so a health
    frame never draws a verdict an operator cannot trace back to the run behind it.

    Args:
        verdicts: The verdicts the caller holds, in the order they are drawn.

    Returns:
        One row per verdict, in input order.
    """
    rows = tuple(_tuple_row(verdict) for verdict in verdicts)
    logger.debug(f"build_runtime_tuple_rows verdicts={len(rows)}")
    return rows


def build_verification_view(
    projection: RouteProjection,
    *,
    verdicts: Sequence[RuntimeTupleVerdict] = (),
) -> RouteReadModel:
    """Return the read model one verification route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        verdicts: The conformance verdicts the health route draws; ignored by every other
            verification route, which carries none.

    Returns:
        The route's read model. The health route returns a :class:`HealthReadModel`,
        whose tuple rows are empty when no verdict is held.

    Raises:
        ValueError: The projection is for a route this module states no read model for.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=VERIFICATION_FIELDS)
    if projection.route != HEALTH_ROUTE:
        return model
    return HealthReadModel(
        route=model.route,
        read_model=model.read_model,
        scope_id=model.scope_id,
        source_cursor=model.source_cursor,
        digest=model.digest,
        complete=model.complete,
        rows=model.rows,
        counts=model.counts,
        specs=model.specs,
        tuples=build_runtime_tuple_rows(verdicts),
    )


__all__ = [
    "FAMILY",
    "HEALTH_ROUTE",
    "NOT_QUARANTINED_REASON",
    "NO_PROVENANCE_REASON",
    "NO_TRIGGER_REASON",
    "QUARANTINE_PRODUCER",
    "TRIGGERS_BY_FAILURE_CODE",
    "VERDICT_REVISION",
    "VERIFICATION_FIELDS",
    "VERIFICATION_ROUTES",
    "HealthReadModel",
    "RuntimeTupleRow",
    "RuntimeTupleVerdict",
    "build_runtime_tuple_rows",
    "build_verification_view",
    "triggers_for_code",
]
