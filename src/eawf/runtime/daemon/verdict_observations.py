"""The verdict observations Trust lists: each audit verdict a Batch's current cycle holds.

A Batch's verification cycle is filed on the Batch ledger, one line per pass, and the
newest line is the cycle at the head the Batch delivers now. Every audit that line holds
is one independent verdict on one criterion of that Batch, reached by one reviewer Run.
Trust lists each as a verdict observation keyed by site, subject and producer, where the
producer is the reviewer's identity as ``(agent_role, runtime)``: the role its capsule
was sealed with and the harness its vendor session ran under, never a runner's name.

Beside them Trust draws the jury's calibration: every verdict any cycle line ever held is
joined to the outcome its subject went on to have -- the Batch merged, or a repair or a
head move refuted it -- or to the gold label a principal pinned on the subject, and the
labelled cohort is scored and held to the ``verify.jury_max_brier`` and
``verify.jury_max_co_error`` ceilings the calibration gate reads. Each juror's share of
that cohort is scored the same way, so the track record states how many of a producer's
verdicts were settled and how well they forecast what happened.

Nothing is stored here. The rows are read off the ledgers each time a projection is
built, so the Milestone a verdict is listed under holds no verdict of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from eawf.kernel.config.schema import VerifyConfig
from eawf.kernel.delivery.batch_proof import BatchAudit, BatchVerificationCycle
from eawf.kernel.delivery.gold_label import latest_gold_labels
from eawf.kernel.projection.compute import (
    CALIBRATION_KEY,
    JUROR_KEY_PREFIX,
    JUROR_SCORE_KIND,
    JURY_CALIBRATION_KIND,
    VERDICT_OBSERVATION_KIND,
)
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.eval.native_cohort import native_cohort, observe_verdict_outcomes
from eawf.observability.eval.trust_projection import calibrate, score_jurors
from eawf.runtime.daemon.methods.delivery import CYCLE_KEY_PREFIX

logger = logging.getLogger(__name__)

#: The site a Batch audit verdict is reached at.
VERIFICATION_SITE: Final = "verification"

#: The Batch states that mean it merged, so a verdict nothing refuted held.
MERGED_STATUSES: Final = frozenset(
    {BatchStatus.MERGED_PENDING_RECONCILIATION.value, BatchStatus.COMPLETED.value}
)


def _stored(
    document: dict[str, Any], records: tuple[LedgerRecord, ...], collection: Epoch2Collection
) -> dict[str, dict[str, Any]]:
    """Return a collection's records by key: the document's, else the ledger's newest."""
    rows = {
        item.record_key: item.payload
        for item in effective_records(records)
        if "payload_kind" not in item.payload and item.payload.get("key") == item.record_key
    }
    rows.update(
        (key, row)
        for key, row in document_rows(document, collection).items()
        if isinstance(row, dict)
    )
    return rows


def _roles(records: tuple[LedgerRecord, ...]) -> Mapping[str, str]:
    """Return the agent role each bound Run's capsule was sealed with, by Run key."""
    roles: dict[str, str] = {}
    for item in records:
        if item.payload.get("payload_kind") != "run_binding":
            continue
        binding = RunBinding.model_validate(item.payload)
        if binding.capsule is not None:
            roles[binding.run_ref.entity_key] = binding.capsule.agent_role.value
    return roles


def _cycle_lines(records: tuple[LedgerRecord, ...]) -> tuple[BatchVerificationCycle, ...]:
    """Return every verification cycle line, in the order the ledger appended them."""
    return tuple(
        BatchVerificationCycle.model_validate(item.payload)
        for item in records
        if item.record_key.startswith(CYCLE_KEY_PREFIX)
    )


def _current_cycles(records: tuple[LedgerRecord, ...]) -> tuple[BatchVerificationCycle, ...]:
    """Return each Batch's newest verification cycle, in the order the Batches were first cycled."""
    newest: dict[str, dict[str, Any]] = {}
    for item in records:
        if item.record_key.startswith(CYCLE_KEY_PREFIX):
            newest[item.record_key] = item.payload
    return tuple(BatchVerificationCycle.model_validate(payload) for payload in newest.values())


def _runtimes(document: dict[str, Any], run_lines: tuple[LedgerRecord, ...]) -> Mapping[str, str]:
    """Return the harness each Run's vendor session ran under, by Run key."""
    runtimes: dict[str, str] = {}
    for key, stored in _stored(document, run_lines, Epoch2Collection.RUN).items():
        session = Run.model_validate(stored).vendor_session
        if session is not None:
            runtimes[key] = session.harness
    return runtimes


def jury_producers(
    document: dict[str, Any], run_lines: tuple[LedgerRecord, ...]
) -> Mapping[str, tuple[AgentSessionRole, str]]:
    """Return the ``(agent_role, runtime)`` each reviewer Run answers for, by Run key.

    Args:
        document: The tree's document, which holds the live Runs.
        run_lines: Every line the run ledger holds.

    Returns:
        One entry per Run both a sealed capsule and a vendor session name.
    """
    roles, runtimes = _roles(run_lines), _runtimes(document, run_lines)
    return {
        key: (AgentSessionRole(role), runtimes[key])
        for key, role in roles.items()
        if key in runtimes
    }


def _row(
    audit: BatchAudit, *, milestone_ref: str | None, role: str | None, runtime: str | None
) -> dict[str, Any]:
    """Return one audit verdict as the notice row Trust lists."""
    return {
        "payload_kind": VERDICT_OBSERVATION_KIND,
        "key": f"{audit.batch_ref.entity_key}-{audit.id}",
        "urn": str(audit.batch_ref),
        "revision": 1,
        "status": audit.verdict.value,
        "site": VERIFICATION_SITE,
        "subject": audit.criterion_id,
        "batch_ref": str(audit.batch_ref),
        "milestone_ref": milestone_ref,
        "verdict": audit.verdict.value,
        "agent_role": role,
        "runtime": runtime,
        "occurred_at": audit.recorded_at.isoformat(),
    }


def verdict_observation_rows(
    document_file: Path, document: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Return every audit verdict the Batches' current cycles hold, as notice rows.

    Args:
        document_file: The selected generation's document, which the ledgers sit beside.
        document: That document, as read.

    Returns:
        One row per audit of each Batch's newest cycle, with the Milestone its Batch is
        filed under and the reviewer's role and runtime; a part no record states is
        ``None``, which Trust renders unknown.

    Raises:
        pydantic.ValidationError: A cycle, binding or Run line does not validate, which
            means the ledger is corrupt.
    """
    batch_lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.BATCH))
    run_lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.RUN))
    batches = _stored(document, batch_lines, Epoch2Collection.BATCH)
    runs = _stored(document, run_lines, Epoch2Collection.RUN)
    roles = _roles(run_lines)
    rows: list[dict[str, Any]] = []
    for cycle in _current_cycles(batch_lines):
        milestone = batches.get(cycle.batch_ref.entity_key, {}).get("milestone_ref")
        for audit in cycle.audits:
            reviewer = audit.review.run_ref.entity_key
            stored = runs.get(reviewer)
            session = Run.model_validate(stored).vendor_session if stored is not None else None
            rows.append(
                _row(
                    audit,
                    milestone_ref=milestone if isinstance(milestone, str) else None,
                    role=roles.get(reviewer),
                    runtime=session.harness if session is not None else None,
                )
            )
    logger.debug(f"verdict_observation_rows verdicts={len(rows)}")
    return tuple(rows)


def resolve_jury_thresholds(repo_root: Path) -> tuple[float, float]:
    """Return the Brier and co-error ceilings the calibration gate holds the jury to.

    Args:
        repo_root: The repository whose layered config is composed.

    Returns:
        ``(verify.jury_max_brier, verify.jury_max_co_error)`` as configured.

    Raises:
        pydantic.ValidationError: The ``verify`` section does not validate.
    """
    from eawf.kernel.config.layered import merge_config

    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    verify = VerifyConfig.model_validate(merged["verify"])
    return verify.jury_max_brier, verify.jury_max_co_error


def jury_calibration_rows(
    document_file: Path, document: dict[str, Any], *, max_brier: float, max_co_error: float
) -> tuple[dict[str, Any], ...]:
    """Return the jury's calibration and each juror's score over every verdict, as rows.

    Args:
        document_file: The selected generation's document, which the ledgers sit beside.
        document: That document, as read.
        max_brier: The Brier ceiling the calibration gate holds the report to.
        max_co_error: The co-error ceiling it holds the report to.

    Returns:
        The report's cohort, metrics and the authority the gate returned, then one row
        per juror with its share of the cohort and its Brier score, every row addressed
        to the repository the verdicts were reached in; empty when no Batch has filed a
        verification cycle, so there is no jury to calibrate.

    Raises:
        pydantic.ValidationError: A cycle, label, binding or Run line does not
            validate, which means the ledger is corrupt.
    """
    batch_lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.BATCH))
    lines = _cycle_lines(batch_lines)
    if not lines:
        return ()
    run_lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.RUN))
    merged = frozenset(
        key
        for key, row in _stored(document, batch_lines, Epoch2Collection.BATCH).items()
        if row.get("status") in MERGED_STATUSES
    )
    cohort, ballots = native_cohort(
        observe_verdict_outcomes(lines, merged_batches=merged),
        producers=jury_producers(document, run_lines),
        labels=latest_gold_labels(batch_lines),
    )
    group = calibrate(cohort, ballots, max_brier=max_brier, max_co_error=max_co_error)
    report = group.report
    jurors = score_jurors(cohort, ballots)
    logger.debug(
        f"jury_calibration_rows n={report.n} status={report.status.value} "
        f"authority={group.authority} jurors={len(jurors)}"
    )
    urn = str(lines[-1].head.repository_ref)
    calibration = {
        "payload_kind": JURY_CALIBRATION_KIND,
        "key": CALIBRATION_KEY,
        "urn": urn,
        "revision": 1,
        "status": report.status.value,
        "cohort": report.n,
        "known_bad": report.known_bad_n,
        "min_scored": group.min_scored,
        "brier": report.brier,
        "co_error": report.unanimous_pass_on_known_bad_rate,
        "authority": group.authority,
    }
    return calibration, *(
        {
            "payload_kind": JUROR_SCORE_KIND,
            "key": f"{JUROR_KEY_PREFIX}{juror.agent_role}-{juror.runtime}",
            "urn": urn,
            "revision": 1,
            "status": juror.report.status.value,
            "agent_role": juror.agent_role,
            "runtime": juror.runtime,
            "cohort": juror.report.n,
            "brier": juror.report.brier,
        }
        for juror in jurors
    )


__all__ = [
    "MERGED_STATUSES",
    "VERIFICATION_SITE",
    "jury_calibration_rows",
    "jury_producers",
    "resolve_jury_thresholds",
    "verdict_observation_rows",
]
