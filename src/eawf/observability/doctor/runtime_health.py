"""Runtime-tuple slice of the canonical doctor check set.

Doctor observes nothing about a runtime tuple itself. Every verdict here
is one the conformance runner already reached, so each check repeats a
stage record and says so: the producer verb, the stage, and the artifact
that stage filed its evidence under travel with the result. A health line
an operator cannot trace back to the run behind it is the failure mode
this slice exists to avoid.

A tree that has never run a conformance stage yields no checks at all. An
empty store is an absence of evidence rather than a healthy tuple, and
inventing an ``ok`` row for it would be the one claim doctor must never
make.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from eawf.kernel.runtime.certification import ConformanceStage, ConformanceStageRecord
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.observability.doctor.models import (
    RUNTIME_TUPLE_CHECK_PREFIX,
    CheckResult,
    CheckStatus,
    HealthProducer,
    HealthProvenance,
)
from eawf.runtime.runtimes.quarantine import is_quarantined

logger = logging.getLogger(__name__)

#: How many digest characters name a tuple in a check name. Enough to
#: tell two installed tuples apart in a table without wrapping the row.
_DIGEST_CHARS: Final = 12

#: The verb that writes each stage. A failed rollback stage is the
#: quarantine verb rather than the rollback verb: quarantine is how a
#: tuple leaves service, rollback is how a profile returns to one.
_PRODUCER_OF_STAGE: Final[Mapping[ConformanceStage, HealthProducer]] = {
    "probe": "conformance.probe",
    "canary": "conformance.canary",
    "certify": "conformance.certify",
    "rollback": "conformance.rollback",
}

#: How a stage outcome grades as health. A refusal is a warning because
#: nothing ran: the tuple is neither proved nor disproved.
_STATUS_OF_OUTCOME: Final[Mapping[str, CheckStatus]] = {
    "passed": "ok",
    "failed": "fail",
    "refused": "warn",
}


def run_runtime_tuple_health_checks(*, workspace: Path | None) -> list[CheckResult]:
    """Return one health check per runtime tuple the conformance store knows.

    Args:
        workspace: The ``.ea/`` parent directory, or ``None`` when doctor
            resolved no anchor.

    Returns:
        One :class:`CheckResult` per tuple, ordered by tuple digest, each
        carrying the provenance of the newest stage recorded for it. The
        list is empty when there is no anchor or no conformance store.
    """
    if workspace is None:
        return []
    path = store_path(workspace / ".ea" / "state.json", StoreKind.CONFORMANCE_STAGE)
    if not path.is_file():
        return []
    history = _history_by_tuple(path)
    checks = [
        _tuple_check(tuple_digest=digest, records=records)
        for digest, records in sorted(history.items())
    ]
    logger.info(f"run_runtime_tuple_health_checks tuples={len(checks)}")
    return checks


def _history_by_tuple(path: Path) -> dict[str, list[ConformanceStageRecord]]:
    """Return every stage record of every tuple in *path*, in append order.

    A line that is not a stage envelope is skipped rather than raised on:
    doctor reports on the store it finds, and a single malformed row must
    not take the whole surface down with it.
    """
    history: dict[str, list[ConformanceStageRecord]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = _stage_record(line)
        if record is None:
            continue
        digest, stage = record
        history.setdefault(digest, []).append(stage)
    return history


def _stage_record(line: str) -> tuple[str, ConformanceStageRecord] | None:
    """Return the tuple digest and stage record one line carries, if it does."""
    try:
        envelope = Envelope.model_validate_json(line)
    except ValidationError:
        return None
    if envelope.kind is not StoreKind.CONFORMANCE_STAGE or envelope.scope_id is None:
        return None
    try:
        record = ConformanceStageRecord.model_validate(envelope.payload)
    except ValidationError:
        return None
    return envelope.scope_id, record


def _tuple_check(*, tuple_digest: str, records: list[ConformanceStageRecord]) -> CheckResult:
    """Return the health of one tuple, graded on its newest stage record."""
    newest = records[-1]
    quarantined = is_quarantined(records)
    status: CheckStatus = "fail" if quarantined else _STATUS_OF_OUTCOME[newest.outcome]
    reason = f" ({newest.reason_code.value})" if newest.reason_code is not None else ""
    state = "quarantined" if quarantined else newest.outcome
    short = tuple_digest.removeprefix("sha256:")[:_DIGEST_CHARS]
    return CheckResult(
        name=f"{RUNTIME_TUPLE_CHECK_PREFIX}_{short}",
        status=status,
        detail=f"{newest.stage} {state}{reason}; {len(records)} stage record(s)",
        provenance=HealthProvenance(
            producer=_producer_of(newest),
            stage=newest.stage,
            evidence_ref=newest.evidence_ref,
        ),
    )


def _producer_of(record: ConformanceStageRecord) -> HealthProducer:
    """Return the daemon verb that wrote *record*."""
    if record.stage == "rollback" and record.outcome == "failed":
        return "conformance.quarantine"
    return _PRODUCER_OF_STAGE[record.stage]


__all__ = ["run_runtime_tuple_health_checks"]
