"""The builtin conduct module and the deviations recorded against it.

The conduct obligations are the behavioural contract a working agent is held
to: goal adherence, minimality, terminal honesty, continuation and stop
discipline, rule reachability, operator-interaction quality and practice
recall. They ship as package data (``data/conduct.yaml``), one rule record per
obligation, and compile through :func:`compile_rule_records` like any other
source, so the authority-widening screen and the one-owner check apply to
them unchanged.

A deviation is one observed breach of an obligation. It is appended to the
machine-local store, never the committed one, and its rate -- per completed
task, per obligation and per runtime -- is derived from those rows alone, so
an obligation that is never breached is visible as a zero rather than absent.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import IncidentSeverity, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.conduct_deviation import (
    KNOWN_OBLIGATIONS_CONTEXT,
    ConductDeviation,
    DeviationDetection,
    DeviationDisposition,
)
from eawf.kernel.store.kinds.events.base import RuntimeTriple
from eawf.kernel.store.paths import local_store_path
from eawf.platform.rules.compile import (
    RuleGraph,
    compile_rule_records,
    registered_enforcement_refs,
    registered_projection_readers,
)
from eawf.platform.rules.records import (
    AuthoredRule,
    RuleModel,
    RuleRecord,
    RuleSourceIdentity,
)

logger = logging.getLogger(__name__)

#: The registered reference of the builtin conduct module.
CONDUCT_MODULE_REF: Final = "eawf.conduct"

_DATA_PACKAGE: Final = "eawf.platform.rules.data"
_DATA_FILE: Final = "conduct.yaml"


class ConductModuleDocument(RuleModel):
    """The on-disk shape of the builtin conduct module.

    Attributes:
        schema_version: Gates unknown future formats.
        rules: One authored rule per conduct obligation.
    """

    schema_version: Literal[1]
    rules: tuple[AuthoredRule, ...]


class ConductDeviationRate(BaseModel):
    """Conduct deviations as a rate rather than a list.

    Attributes:
        deviation_count: Every recorded deviation.
        completed_task_count: Tasks completed over the same history.
        rate_per_completed_task: ``deviation_count / completed_task_count``;
            ``None`` before any task has completed, since there is no
            denominator to divide by.
        by_obligation: Count per obligation, covering every compiled
            obligation so one never breached reads as zero.
        by_runtime: Count per runtime that produced a deviation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deviation_count: int = Field(ge=0)
    completed_task_count: int = Field(ge=0)
    rate_per_completed_task: float | None
    by_obligation: dict[str, int]
    by_runtime: dict[str, int]


def load_conduct_rules() -> tuple[RuleRecord, ...]:
    """Read the builtin conduct module and stamp each rule with its source.

    Returns:
        One builtin rule record per conduct obligation, in authored order.

    Raises:
        pydantic.ValidationError: When the packaged module fails the closed
            schema, which is a packaging defect.
    """
    raw = files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_bytes()
    document = ConductModuleDocument.model_validate(yaml.safe_load(raw))
    source = RuleSourceIdentity(
        kind="builtin",
        locator=CONDUCT_MODULE_REF,
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )
    return tuple(
        RuleRecord.model_validate({**rule.model_dump(), "source": source})
        for rule in document.rules
    )


def compile_conduct_graph() -> RuleGraph:
    """Compile the conduct module on its own.

    Returns:
        The graph holding one effective rule per conduct obligation.

    Raises:
        RuleCompileError: When a conduct rule fails compilation, such as
            prose that reads as an authority grant.
    """
    return compile_rule_records(
        load_conduct_rules(),
        modules=(),
        enforcement_refs=registered_enforcement_refs(),
        projection_readers=registered_projection_readers(),
    )


@cache
def conduct_obligation_ids() -> frozenset[str]:
    """Return every obligation the compiled conduct module defines.

    Cached because the packaged module cannot change within a process.

    Returns:
        The obligation identifiers.
    """
    return frozenset(rule.record.obligation_id for rule in compile_conduct_graph().rules)


def record_conduct_deviation(
    state_path: Path,
    *,
    obligation_id: str,
    scope_id: str,
    run_id: str,
    runtime: RuntimeTriple,
    detection: DeviationDetection,
    severity: IncidentSeverity,
    evidence_ref: str,
    disposition: DeviationDisposition = "open",
) -> ConductDeviation:
    """Append one deviation row to the machine-local store.

    Args:
        state_path: Path to the tree's ``state.json``.
        obligation_id: The breached conduct obligation.
        scope_id: The lifecycle scope the breaching run served.
        run_id: The run that produced the deviation.
        runtime: The runtime the run executed on.
        detection: How the deviation was noticed.
        severity: How much the breach matters.
        evidence_ref: Where the evidence of the breach can be read.
        disposition: What was done about it.

    Returns:
        The recorded deviation.

    Raises:
        pydantic.ValidationError: When ``obligation_id`` is not a compiled
            conduct obligation, or any field fails the closed schema.
        StateConflict: When the store's append lock cannot be acquired.
    """
    deviation = ConductDeviation.model_validate(
        {
            "obligation_id": obligation_id,
            "scope_id": scope_id,
            "run_id": run_id,
            "runtime": runtime,
            "detection": detection,
            "severity": severity,
            "evidence_ref": evidence_ref,
            "disposition": disposition,
        },
        context={KNOWN_OBLIGATIONS_CONTEXT: conduct_obligation_ids()},
    )
    append_envelope(
        local_store_path(state_path, StoreKind.CONDUCT_DEVIATION),
        Envelope(
            id=f"CDV-{uuid.uuid4().hex[:16].upper()}",
            kind=StoreKind.CONDUCT_DEVIATION,
            scope_id=scope_id,
            created_at=datetime.now(UTC),
            summary=f"conduct deviation {obligation_id} in {scope_id}",
            payload=deviation.model_dump(mode="json"),
        ),
    )
    logger.info(f"conduct deviation recorded obligation={obligation_id} scope={scope_id}")
    return deviation


def read_conduct_deviations(state_path: Path) -> tuple[ConductDeviation, ...]:
    """Read every recorded deviation from the machine-local store.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The deviations in append order; empty when none was recorded.

    Raises:
        pydantic.ValidationError: When a row fails the envelope or payload
            schema.
    """
    path = local_store_path(state_path, StoreKind.CONDUCT_DEVIATION)
    if not path.is_file():
        return ()
    return tuple(
        ConductDeviation.model_validate(Envelope.model_validate_json(line).payload)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def conduct_deviation_rate(
    deviations: Iterable[ConductDeviation],
    *,
    obligations: Iterable[str],
    completed_task_count: int,
) -> ConductDeviationRate:
    """Derive the deviation rate from recorded rows.

    Args:
        deviations: The recorded deviations.
        obligations: Every obligation to report, including never-breached
            ones; a recorded obligation outside this set is still counted.
        completed_task_count: Tasks completed over the same history.

    Returns:
        The rate, with per-obligation and per-runtime counts.

    Raises:
        ValueError: When ``completed_task_count`` is negative.
    """
    if completed_task_count < 0:
        raise ValueError(f"completed_task_count must be >= 0, got {completed_task_count}")
    rows = tuple(deviations)
    by_obligation = dict.fromkeys(sorted(obligations), 0)
    for obligation, count in Counter(row.obligation_id for row in rows).items():
        by_obligation[obligation] = count
    by_runtime = Counter(row.runtime for row in rows)
    return ConductDeviationRate(
        deviation_count=len(rows),
        completed_task_count=completed_task_count,
        rate_per_completed_task=(
            len(rows) / completed_task_count if completed_task_count else None
        ),
        by_obligation=dict(sorted(by_obligation.items())),
        by_runtime=dict(sorted(by_runtime.items())),
    )


__all__ = [
    "CONDUCT_MODULE_REF",
    "ConductDeviationRate",
    "ConductModuleDocument",
    "compile_conduct_graph",
    "conduct_deviation_rate",
    "conduct_obligation_ids",
    "load_conduct_rules",
    "read_conduct_deviations",
    "record_conduct_deviation",
]
