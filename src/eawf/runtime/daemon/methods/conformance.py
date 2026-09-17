"""``conformance.*`` JSON-RPC methods: the daemon's certification writer.

The three verbs are the three stages, and they are the only way a
:class:`DriverCertification` comes into being. Each appends its stage
record to ``<state_dir>/store/conformance_stage.jsonl`` under the tuple's
digest, so the history a certification cites is on disk before the
certification is returned.

``conformance.quarantine`` and ``conformance.rollback`` are the two verbs
that take a tuple back out of service and walk a profile back to its
last-known-good pin. Both reach the same runner, so a quarantine is
visible to the rollback that follows it.

The runner is cached per state path for the life of the process, because
the canary lease and the pin ledger live in it: a certify that arrives
while a canary is in flight must see that canary, and a rollback must see
the pins earlier certifies wrote. Both are only possible when every call
reaches the same runner.

Refusals come back as results, not as errors. A tuple whose certify is
refused for an in-flight canary or expired evidence is a fact about the
tuple that the certification route renders; only a caller walking the
stage machine out of order gets an error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.runtime.certification import ConformanceStageRecord
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.runtimes.conformance import (
    CanaryRequest,
    CertificationRequest,
    ConformanceRunner,
    ProbeRequest,
    QuarantineRequest,
    RollbackRequest,
)

logger = logging.getLogger(__name__)

#: Runners cached by resolved state path. One per tree, so the canary
#: lease a run_canary holds is the one a concurrent certify observes.
_RUNNERS: dict[Path, ConformanceRunner] = {}


class StageParams(BaseModel):
    """Wire contract shared by the three stage verbs."""

    model_config = ConfigDict(extra="forbid")

    request: dict[str, Any]


class StoreStageJournal:
    """Append-only stage journal over one tree's conformance store.

    Attributes:
        path: The JSONL collection every stage record lands in.
    """

    def __init__(self, state_path: Path) -> None:
        """Bind the journal to the store of the tree at *state_path*.

        Args:
            state_path: Path to the tree's ``state.json``.
        """
        self.path = store_path(state_path, StoreKind.CONFORMANCE_STAGE)

    def append(self, *, tuple_digest: str, record: ConformanceStageRecord) -> None:
        """Append one stage record for one tuple.

        Args:
            tuple_digest: Digest of the runtime tuple the stage ran for.
            record: The stage record to persist.

        Raises:
            StateConflict: The append lock cannot be acquired.
        """
        ordinal = len(self.records(tuple_digest=tuple_digest))
        append_envelope(
            self.path,
            Envelope(
                id=f"{tuple_digest}:{ordinal}:{record.stage}",
                kind=StoreKind.CONFORMANCE_STAGE,
                scope_id=tuple_digest,
                created_at=record.completed_at,
                summary=f"{record.stage} {record.outcome}",
                payload=record.model_dump(mode="json"),
            ),
        )

    def records(self, *, tuple_digest: str) -> tuple[ConformanceStageRecord, ...]:
        """Return every stage record of one tuple, in append order.

        Args:
            tuple_digest: Digest of the runtime tuple to read.

        Returns:
            The tuple's stage records, oldest first; empty when the tuple
            has never been probed.

        Raises:
            ValueError: A line is not an envelope of this kind, or its
                payload is not a stage record.
        """
        if not self.path.exists():
            return ()
        rows: list[ConformanceStageRecord] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            envelope = _envelope(line, path=self.path, number=number)
            if envelope.scope_id != tuple_digest:
                continue
            rows.append(_record(envelope, path=self.path, number=number))
        return tuple(rows)


def _envelope(line: str, *, path: Path, number: int) -> Envelope:
    """Return the envelope one journal line carries.

    Raises:
        ValueError: The line is not an envelope, or is filed under
            another store kind.
    """
    try:
        envelope = Envelope.model_validate_json(line)
    except ValidationError as exc:
        raise ValueError(f"conformance journal {path} line {number} is not an envelope") from exc
    if envelope.kind is not StoreKind.CONFORMANCE_STAGE:
        raise ValueError(
            f"conformance journal {path} line {number} is filed under {envelope.kind.value!r}"
        )
    return envelope


def _record(envelope: Envelope, *, path: Path, number: int) -> ConformanceStageRecord:
    """Return the stage record one envelope carries.

    Raises:
        ValueError: The payload is not a stage record.
    """
    try:
        return ConformanceStageRecord.model_validate(envelope.payload)
    except ValidationError as exc:
        raise ValueError(
            f"conformance journal {path} line {number} payload is not a stage record"
        ) from exc


def runner_for(state_path: Path) -> ConformanceRunner:
    """Return the process-wide runner of the tree at *state_path*.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The tree's one runner, created on first use. The instance is kept
        because it holds the canary leases that certify checks against.
    """
    resolved = Path(state_path).resolve()
    runner = _RUNNERS.get(resolved)
    if runner is None:
        runner = ConformanceRunner(journal=StoreStageJournal(resolved))
        _RUNNERS[resolved] = runner
    return runner


def _runner(ctx: MethodContext) -> ConformanceRunner:
    """Return the runner bound to the context's state tree.

    Raises:
        RuntimeError: The context carries no state path, so there is no
            store to append stage records to.
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    return runner_for(Path(ctx.state_path))


@register("conformance.probe")
async def probe(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Run the probe stage for one runtime tuple.

    Args:
        ctx: Server context; must carry ``state_path``.
        params: JSON-RPC params per :class:`StageParams`, whose request
            validates as :class:`ProbeRequest`.

    Returns:
        The serialised :class:`ProbeStageResult`.

    Raises:
        ValueError: The request is malformed, names an unknown runtime,
            or requires a capability the matrix has no row for.
        RuntimeError: The context carries no state path.
    """
    request = ProbeRequest.model_validate(StageParams.model_validate(params).request)
    result = _runner(ctx).run_probe(request)
    logger.info(f"probe runtime={request.runtime_id!r} outcome={result.record.outcome!r}")
    return result.model_dump(mode="json")


@register("conformance.canary")
async def canary(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Run the canary stage for one runtime tuple.

    Args:
        ctx: Server context; must carry ``state_path``.
        params: JSON-RPC params per :class:`StageParams`, whose request
            validates as :class:`CanaryRequest`.

    Returns:
        The serialised :class:`CanaryStageResult`.

    Raises:
        ValueError: The request is malformed, or the canary would run as
            the Run the tuple serves.
        ConformanceSequenceError: No passed probe precedes the canary.
        RuntimeError: The context carries no state path.
    """
    request = CanaryRequest.model_validate(StageParams.model_validate(params).request)
    result = _runner(ctx).run_canary(request)
    logger.info(
        f"canary outcome={result.record.outcome!r} "
        f"parity_mismatches={len(result.parity_mismatches)}"
    )
    return result.model_dump(mode="json")


@register("conformance.certify")
async def certify(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Run the certify stage, writing the certification it earned.

    Args:
        ctx: Server context; must carry ``state_path``.
        params: JSON-RPC params per :class:`StageParams`, whose request
            validates as :class:`CertificationRequest`.

    Returns:
        The serialised :class:`CertifyStageResult`, carrying either the
        certification or the refusal reason.

    Raises:
        ValueError: The request is malformed.
        ConformanceSequenceError: The tuple's history does not end in a
            passed probe followed by a passed canary.
        RuntimeError: The context carries no state path.
    """
    request = CertificationRequest.model_validate(StageParams.model_validate(params).request)
    result = _runner(ctx).certify(request)
    logger.info(
        f"certify certification_id={request.certification_id!r} outcome={result.record.outcome!r}"
    )
    return result.model_dump(mode="json")


@register("conformance.quarantine")
async def quarantine(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Quarantine one runtime tuple and disable everything that needs it.

    Args:
        ctx: Server context; must carry ``state_path``.
        params: JSON-RPC params per :class:`StageParams`, whose request
            validates as :class:`QuarantineRequest`.

    Returns:
        The serialised :class:`QuarantineStageResult`, carrying the
        quarantined certification and the disabled profiles and routes.

    Raises:
        ValueError: The request is malformed, or its certification covers
            another tuple.
        RuntimeError: The context carries no state path.
    """
    request = QuarantineRequest.model_validate(StageParams.model_validate(params).request)
    result = _runner(ctx).quarantine(request)
    logger.info(
        f"quarantine trigger={request.trigger.value!r} "
        f"profiles={len(result.disabled.profiles)} routes={len(result.disabled.routes)}"
    )
    return result.model_dump(mode="json")


@register("conformance.rollback")
async def rollback(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Roll one profile back to its last-known-good certification.

    Args:
        ctx: Server context; must carry ``state_path``.
        params: JSON-RPC params per :class:`StageParams`, whose request
            validates as :class:`RollbackRequest`.

    Returns:
        The serialised :class:`RollbackStageResult`. A profile with no
        admissible pin comes back refused and stays disabled.

    Raises:
        ValueError: The request is malformed.
        RuntimeError: The context carries no state path.
    """
    request = RollbackRequest.model_validate(StageParams.model_validate(params).request)
    result = _runner(ctx).roll_back(request)
    logger.info(
        f"rollback profile={request.profile.profile_id!r} "
        f"selected={result.selection.pin is not None}"
    )
    return result.model_dump(mode="json")


__all__ = [
    "StageParams",
    "StoreStageJournal",
    "canary",
    "certify",
    "probe",
    "quarantine",
    "rollback",
    "runner_for",
]
