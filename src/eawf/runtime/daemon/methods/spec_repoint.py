"""``spec.repoint_gates`` JSON-RPC method: repair a closed wave's gate argv.

Sibling of :mod:`eawf.runtime.daemon.methods.spec_convert`, split out of
:mod:`eawf.runtime.daemon.methods.spec` for the same reason (module-length
budget): the repoint verb owns its params / result models and its locked
write transaction, and reuses the shared spec-method plumbing
(idempotency cache, mutator-path resolution, post-mutation validation,
publish bus) so every spec mutator rides one implementation.

Unlike ``spec.sync`` this mutation deliberately targets a CLOSED wave --
that is the whole point. A wave's recorded gates are its verification
record, and a later test-tree move leaves that record naming paths which
no longer resolve, so replaying it exits on a usage error. The repoint
keeps the record re-runnable without reopening the wave; the bounded
mutation itself lives in
:mod:`eawf.workflow.lifecycle.gate_repoint`, which refuses any edit that
moves anything other than gate argv.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.spec_context import (
    cache_replay,
    idempotent_replay,
    publish_envelope,
    validate_post_sync,
)
from eawf.runtime.daemon.methods.state_context import (
    read_state,
    resolve_mutator_paths,
    state_version,
)
from eawf.runtime.daemon.wal import WalRecord
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.gate_repoint import (
    GateArgvRepoint,
    GateRepointReport,
    build_argv_repoint,
    repoint_closed_wave_gates,
)

logger = logging.getLogger(__name__)


class RepointGatesParams(BaseModel):
    """Params for :func:`repoint_gates`.

    Attributes:
        wave_id: ``P##-I##-W##`` -- the CLOSED wave whose recorded gates
            are repointed. Phase / iter scopes are refused: a repoint is
            evidence repair and is authorised one wave at a time.
        repoints: Per-gate argv rewrites; at least one is required.
        dry_run: When ``True`` the repoint is computed against an
            in-memory copy and reported with no state write.
        repo_root: Optional absolute repo working-tree path (default cwd).
        idempotency_key: Optional caller-supplied retry key.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    repoints: list[GateArgvRepoint] = Field(min_length=1)
    dry_run: bool = False
    repo_root: str | None = None
    idempotency_key: str | None = None


class SpecRepointGatesResult(BaseModel):
    """Result shape for the :func:`repoint_gates` RPC."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    wave_id: str
    dry_run: bool
    changed_count: int
    changed: list[dict[str, Any]]
    unchanged_gate_ids: list[str]
    before_version: str | None
    after_version: str | None
    envelope: dict[str, Any] | None
    idempotent_replay: bool = False


def _apply_repoint(state: State, args: RepointGatesParams) -> GateRepointReport:
    """Run the bounded repoint against *state*, mapping refusals to RPC errors.

    Args:
        state: Loaded state to mutate in place.
        args: Validated repoint params.

    Returns:
        The lifecycle report naming the gates whose argv moved.

    Raises:
        DaemonValidationError: When the wave is unknown or not CLOSED, a
            named gate is not recorded or carries no argv, an argv fails
            the L0 policy, or the replacement would change anything other
            than gate argv (mapped to ``-32002``).
    """
    wave = state.waves.get(args.wave_id)
    if wave is None:
        raise DaemonValidationError(f"validation_failed: unknown wave: {args.wave_id!r}")
    try:
        candidates = build_argv_repoint(wave, list(args.repoints))
        return repoint_closed_wave_gates(state, wave_id=args.wave_id, gates=candidates)
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


@register("spec.repoint_gates")
async def repoint_gates(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Repoint the recorded gate argv of a CLOSED wave, and nothing else.

    The audited repair path for a verification record whose gate argv
    names a retired path (rule 4: the daemon is the canonical writer).
    The wave keeps its CLOSED status, its criteria, its outcome and its
    ``closed_at``; only the argv of the named gates moves, and the
    lifecycle guard refuses the whole mutation when anything else does.

    Args:
        ctx: Server context. ``ctx.wal_dir`` MUST be configured for a
            non-dry-run call.
        params: JSON-RPC params per :class:`RepointGatesParams`.

    Returns:
        Dict matching :class:`SpecRepointGatesResult`; ``dry_run=True``
        reports the would-change set with no state write
        (``before_version`` / ``after_version`` / ``envelope`` are
        ``None``).

    Raises:
        ValueError: When the params do not validate or *wave_id* is not a
            wave scope (mapped to ``-32602``).
        DaemonValidationError: When the repoint is refused or the
            post-mutation state fails schema / invariant validation
            (mapped to ``-32002``).
    """
    try:
        args = RepointGatesParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    try:
        kind = spec_writer.classify_scope(args.wave_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    if kind != "wave":
        raise ValueError(
            f"validation_failed: gate repoint targets a wave scope, got {args.wave_id!r}"
        )

    replay = idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"repoint_gates idempotent_replay wave={args.wave_id!r}")
        return replay

    state_path, event_path, wal_path = resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    if args.dry_run:
        state, _payload = read_state(state_path)
        report = _apply_repoint(state.model_copy(deep=True), args)
        return _repoint_result(args, report, before=None, after=None, envelope=None)

    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            result = _apply_repoint_locked(
                ctx,
                args=args,
                state_path=state_path,
                event_path=event_path,
                wal_path=wal_path,
            )
        cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


def _apply_repoint_locked(
    ctx: MethodContext,
    *,
    args: RepointGatesParams,
    state_path: Path,
    event_path: Path,
    wal_path: Path,
) -> dict[str, Any]:
    """Run the locked repoint transaction: repoint, validate, write.

    The caller holds the state-path portalock. Mirrors the spec-sync
    transaction (WAL -> atomic write -> event append -> publish); when the
    recorded argv already matches the request, nothing is written and the
    report alone is returned so a replayed repair is a true no-op.

    Args:
        ctx: Server context (publish bus).
        args: Validated repoint params.
        state_path: Path to ``state.json``.
        event_path: Path to the event JSONL store.
        wal_path: Path to the daemon WAL directory.

    Returns:
        Dict matching :class:`SpecRepointGatesResult`.

    Raises:
        DaemonValidationError: When the repoint is refused or the
            post-mutation state fails schema / invariant validation.
    """
    state, _payload = read_state(state_path)
    before_version = state_version(state.model_dump(mode="json"))
    report = _apply_repoint(state, args)
    if not report.changed:
        return _repoint_result(
            args, report, before=before_version, after=before_version, envelope=None
        )

    state.updated_at = datetime.now(UTC)
    new_payload = state.model_dump(mode="json")
    after_version = validate_post_sync(new_payload)

    mutation_id = uuid.uuid4().hex
    envelope = _build_repoint_envelope(
        wave_id=args.wave_id,
        changed_count=len(report.changed),
        changed_gate_ids=[change.gate_id for change in report.changed],
        before_version=before_version,
        after_version=after_version,
    )
    record = WalRecord(
        record_id=mutation_id,
        envelope=envelope,
        idempotency_key=args.idempotency_key,
        written_at=datetime.now(UTC),
        before_state_version=before_version,
        after_state_version=after_version,
        state_path=str(state_path),
    )
    wal.write_pending(wal_path, record)
    atomic_write_json_locked(state_path, new_payload)
    wal.mark_applied(wal_path, mutation_id)
    append_envelope(event_path, envelope)
    wal.mark_fsynced(wal_path, mutation_id)
    publish_envelope(ctx, envelope)
    logger.info(
        f"repoint_gates ok wave={args.wave_id} changed={len(report.changed)} "
        f"before={before_version} after={after_version}"
    )
    return _repoint_result(
        args,
        report,
        before=before_version,
        after=after_version,
        envelope=envelope.model_dump(mode="json"),
    )


def _repoint_result(
    args: RepointGatesParams,
    report: GateRepointReport,
    *,
    before: str | None,
    after: str | None,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the :class:`SpecRepointGatesResult` payload dict.

    Args:
        args: Validated repoint params.
        report: The lifecycle repoint report.
        before: State digest before the write, ``None`` under dry-run.
        after: State digest after the write, ``None`` under dry-run.
        envelope: The canonical event envelope, ``None`` when nothing was
            written.

    Returns:
        The JSON-safe result dict.
    """
    return SpecRepointGatesResult(
        operation="repoint_gates",
        wave_id=args.wave_id,
        dry_run=args.dry_run,
        changed_count=len(report.changed),
        changed=[change.model_dump(mode="json") for change in report.changed],
        unchanged_gate_ids=list(report.unchanged_gate_ids),
        before_version=before,
        after_version=after,
        envelope=envelope,
    ).model_dump(mode="json")


def _build_repoint_envelope(
    *,
    wave_id: str,
    changed_count: int,
    changed_gate_ids: list[str],
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical event envelope for a gate repoint.

    The envelope is what makes the repair audited rather than silent: it
    names the wave and the exact gate ids whose argv moved, so the event
    log alone reconstructs which verification records were repaired.

    Args:
        wave_id: The closed wave that was repointed.
        changed_count: How many gate argv vectors moved.
        changed_gate_ids: The ids of those gates.
        before_version: State digest before the write.
        after_version: State digest after the write.

    Returns:
        The canonical event envelope, ready for the WAL + event log.
    """
    now = datetime.now(UTC)
    summary = f"spec.repoint_gates wave={wave_id} changed={changed_count}"
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.spec_repoint_gates",
        actor="daemon",
        command="spec.repoint_gates",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras={
            "changed_count": changed_count,
            # ``extras`` is a scalar map, so the id list rides as a joined
            # string rather than being dropped from the audit trail.
            "changed_gate_ids": ",".join(changed_gate_ids),
        },
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


__all__ = [
    "RepointGatesParams",
    "SpecRepointGatesResult",
    "repoint_gates",
]
