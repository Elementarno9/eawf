"""The verification read models: what trust, evidence and health draw at one cursor.

Four routes answer the one question an operator asks of a verification surface: what is
believed here, and on whose say-so. ``trust`` renders the audit verdicts the Batches'
current verification cycles hold, each with the producer that reached it, and the jury's
calibration over them, beside the claims no verdict has been recorded for,
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
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from eawf.kernel.delivery.acceptance import EvidenceRow
from eawf.kernel.projection.compute import FACTS_FIELD, RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    known_field,
    stated,
    status_and,
    unknown_field,
)
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.runtime.certification import CertificationFailureCode, QuarantineTrigger
from eawf.kernel.state.epoch2.evidence_rung import (
    EvidenceRungRecord,
    RungOutcome,
    latest_rungs,
)
from eawf.kernel.store.ledger import LedgerRecord, effective_records
from eawf.kernel.store.tiers import Epoch2Collection
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

#: Why a Trust subject states no verdict: no observation was recorded for it.
NO_OUTCOME_REASON: Final = "no outcome recorded"

#: Why a calibration cell is silent: only the calibration row states it, and a metric the
#: report left undefined is not stated there either.
NO_CALIBRATION_REASON: Final = "the calibration row states no such value"

#: Why a verdict names no Milestone: the Batch it was reached on is filed under none.
NO_MILESTONE_REASON: Final = "the Batch this verdict was reached on is filed under no Milestone"

#: Why an Evidence row states no ladder: only a claim is scored, and this one has no rung
#: record yet -- or the row is an evidence record, which is scored through its claims.
NOT_SCORED_REASON: Final = "no rung record scores this row; only a claim carries a ladder"

#: Why a scored claim states no instant: no rung of its ladder has returned.
NO_RUNG_RETURNED_REASON: Final = "no rung of this claim's ladder has returned yet"

#: Why a rung card row states nothing: no rung of the claim ran over this record.
NOT_RUN_OVER_REASON: Final = "no rung of the claim ran over this record"

#: The routes whose rows carry what the claims' ladders state.
LADDER_ROUTES: Final[tuple[str, ...]] = ("evidence", "evidence.digest")

#: What separates a claim's key from the rung a record in its ledger scores.
_RUNG_KEY_MARK: Final = "#rung-"

#: The producer revision every verdict cell states. A conformance verdict is never
#: revised: the next stage record supersedes it, so the first revision is the only one a
#: single verdict ever stands at.
VERDICT_REVISION: Final = 1

#: What each verification route renders per row, in column order. The first field of
#: every route is the status the document states; the rest are declared columns whose
#: producers are later items, so the console draws the unknown token in their place.
#: When a health check last ran is no column of a row: it is stated on the tuple row,
#: from the stage record the conformance runner wrote when the check completed.
VERIFICATION_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        "trust": status_and(
            stated("verdict", absent=NO_OUTCOME_REASON),
            stated("answered_by", absent=NO_OUTCOME_REASON),
            stated("occurred_at", absent=NO_OUTCOME_REASON),
            stated("site", absent=NO_OUTCOME_REASON),
            stated("subject", absent=NO_OUTCOME_REASON),
            stated("batch", absent=NO_OUTCOME_REASON),
            stated("milestone", absent=NO_MILESTONE_REASON),
            stated("agent_role", absent=NO_OUTCOME_REASON),
            stated("runtime", absent=NO_OUTCOME_REASON),
            stated("cohort", absent=NO_CALIBRATION_REASON),
            stated("known_bad", absent=NO_CALIBRATION_REASON),
            stated("min_scored", absent=NO_CALIBRATION_REASON),
            stated("brier", absent=NO_CALIBRATION_REASON),
            stated("co_error", absent=NO_CALIBRATION_REASON),
            stated("authority", absent=NO_CALIBRATION_REASON),
        ),
        "evidence": status_and(
            stated("outcome", absent=NOT_SCORED_REASON),
            stated("checked", absent=NOT_SCORED_REASON),
            stated("as_of", absent=NO_RUNG_RETURNED_REASON),
        ),
        "evidence.digest": status_and(
            stated("found", absent=NOT_RUN_OVER_REASON),
            stated("input_digest", absent=NOT_RUN_OVER_REASON),
        ),
        "health": status_and(),
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
        checked_at: When that stage completed, which is when the check last ran.
    """

    check: CheckResult
    checked_at: datetime
    reason_code: CertificationFailureCode | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeTupleRow:
    """One runtime tuple as the health frame draws it.

    Attributes:
        check: The check's stable name, which names the tuple by its digest.
        status: What the verdict grades as: ``ok``, ``warn`` or ``fail``.
        checked_at: When the check last ran.
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
    checked_at: datetime
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
        checked_at=verdict.checked_at,
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


def claim_ladder_facts(ladder: Sequence[EvidenceRungRecord]) -> dict[str, str]:
    """Return what one claim's Evidence row states of its ladder.

    Args:
        ladder: The latest record of each rung scoring the claim, lowest rung first.

    Returns:
        ``outcome``, each rung's outcome by number; ``checked``, ``yes`` when every rung
        has returned an outcome and ``no`` while one is open or waiting; and ``as_of``,
        when the latest rung returned, absent while none has.
    """
    returned = [r.evaluated_at for r in ladder if r.evaluated_at is not None]
    complete = all(r.outcome in (RungOutcome.PASSED, RungOutcome.FAILED) for r in ladder)
    facts = {
        "outcome": " · ".join(f"{r.rung} {r.outcome.value}" for r in ladder),
        "checked": "yes" if ladder and complete else "no",
    }
    if returned:
        facts["as_of"] = max(returned).isoformat()
    return facts


def _subject_key(subject: str) -> str:
    """Return the record key a route subject names, whether a bare key or a URN."""
    return subject.split("#", 1)[0].rsplit("/", 1)[-1]


def _input_rows(
    ladder: Sequence[EvidenceRungRecord], evidence: Mapping[str, LedgerRecord]
) -> tuple[dict[str, Any], ...]:
    """Return one row per held evidence record *ladder* ran over, with what it found there.

    A record is drawn once, under the highest rung that ran over it; its digest is the one
    rung 1 resolved it at.
    """
    found: dict[str, tuple[str, EvidenceRungRecord]] = {}
    digests: dict[str, str] = {}
    for record in ladder:
        for item in record.input_refs:
            found[item.ref.entity_key] = (str(item.ref), record)
            if record.rung == 1 and item.digest is not None:
                digests[item.ref.entity_key] = item.digest
    rows: list[dict[str, Any]] = []
    for key, (urn, record) in found.items():
        line = evidence.get(key)
        if line is None:
            continue
        facts = {"found": f"rung {record.rung} {record.outcome.value}: {record.finding}"}
        if key in digests:
            facts["input_digest"] = digests[key]
        rows.append(
            {
                "key": key,
                "urn": urn,
                "revision": 1,
                "status": line.status,
                "title": EvidenceRow.model_validate(line.payload).summary,
                FACTS_FIELD: facts,
            }
        )
    return tuple(rows)


def ladder_route_rows(
    *,
    route: str,
    rows: Mapping[Epoch2Collection, Sequence[Mapping[str, Any]]],
    claim_lines: Sequence[LedgerRecord],
    evidence_lines: Sequence[LedgerRecord],
    subject: str | None,
) -> dict[Epoch2Collection, tuple[Mapping[str, Any], ...]]:
    """Return *rows* with what the claims' ladders state carried on the rows they concern.

    Each claim row carries its ladder's outcome, whether every rung returned, and when the
    latest one did. The rung card of a claim lists the evidence records its rungs ran
    over, each with what the highest of those rungs found there and the digest rung 1
    resolved it at.

    Args:
        route: The route being built; one of :data:`LADDER_ROUTES`.
        rows: The ledger rows the route already reads, by collection.
        claim_lines: The claim ledger's lines, in file order.
        evidence_lines: The evidence ledger's lines, in file order.
        subject: The record the route was opened on, a key or a URN; the rung card
            lists nothing without one.

    Returns:
        The rows by collection, the claim rows carrying their ladder facts.
    """
    scored: dict[str, list[EvidenceRungRecord]] = {}
    for line in effective_records(tuple(claim_lines)):
        if _RUNG_KEY_MARK in line.record_key:
            record = EvidenceRungRecord.model_validate(line.payload)
            scored.setdefault(record.claim_ref.entity_key, []).append(record)
    ladders = {key: latest_rungs(records) for key, records in scored.items()}
    merged = {collection: tuple(held) for collection, held in rows.items()}
    merged[Epoch2Collection.CLAIM] = tuple(
        {**row, FACTS_FIELD: claim_ladder_facts(ladders[row["key"]])}
        if row.get("key") in ladders
        else row
        for row in rows.get(Epoch2Collection.CLAIM, ())
    )
    if route == "evidence.digest" and subject is not None:
        evidence = {line.record_key: line for line in effective_records(tuple(evidence_lines))}
        held = _input_rows(ladders.get(_subject_key(subject), ()), evidence)
        merged[Epoch2Collection.EVIDENCE] = (*held, *rows.get(Epoch2Collection.EVIDENCE, ()))
    logger.debug(f"ladder_route_rows route={route} claims={len(ladders)}")
    return merged


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
    "LADDER_ROUTES",
    "NOT_QUARANTINED_REASON",
    "NOT_RUN_OVER_REASON",
    "NOT_SCORED_REASON",
    "NO_CALIBRATION_REASON",
    "NO_MILESTONE_REASON",
    "NO_OUTCOME_REASON",
    "NO_PROVENANCE_REASON",
    "NO_RUNG_RETURNED_REASON",
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
    "claim_ladder_facts",
    "ladder_route_rows",
    "triggers_for_code",
]
