"""``spec.repoint_*`` JSON-RPC methods: repair a recorded wave row.

Sibling of :mod:`eawf.runtime.daemon.methods.spec_convert`, split out of
:mod:`eawf.runtime.daemon.methods.spec` for the same reason (module-length
budget): the repoint verbs own their params / result models and their
locked write transactions, and reuse the shared spec-method plumbing
(idempotency cache, mutator-path resolution, post-mutation validation,
publish bus) so every spec mutator rides one implementation.

Unlike ``spec.sync`` these mutations deliberately target a wave past
plan time -- that is the whole point. ``spec.repoint_gates`` repairs a
verification record whose gate argv names a path a later test-tree move
retired, so replaying it exits on a usage error. ``spec.repoint_scopes``
repairs the other two after-the-fact defects: ``file_scopes`` that the
wave's own pinned commit contradicts, and the prose of a named success
criterion. ``spec.rewrite_gate_kind`` strengthens a record whose gates
cannot prove their criteria, turning a grep-style gate into a command
gate (or giving an ungated criterion one) and never the reverse. All
three keep the record usable without reopening the wave; the bounded
mutations live in :mod:`eawf.workflow.lifecycle.gate_repoint`,
:mod:`eawf.workflow.lifecycle.scope_repoint` and
:mod:`eawf.workflow.lifecycle.gate_kind_rewrite`, which refuse any edit
that moves anything outside the field each verb owns.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
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
from eawf.workflow.lifecycle.gate_kind_rewrite import (
    GateKindRewrite,
    GateKindRewriteReport,
    rewrite_closed_wave_gate_kinds,
)
from eawf.workflow.lifecycle.gate_repoint import (
    GateArgvRepoint,
    GateRepointReport,
    build_argv_repoint,
    repoint_closed_wave_gates,
)
from eawf.workflow.lifecycle.scope_repoint import (
    CriterionTextRepoint,
    ScopeRepointReport,
    repoint_closed_wave_record,
)

logger = logging.getLogger(__name__)


def _commit_repair(
    ctx: MethodContext,
    *,
    state: State,
    paths: tuple[Path, Path, Path],
    idempotency_key: str | None,
    before_version: str,
    build_envelope: Callable[[str], Envelope],
) -> tuple[str, Envelope]:
    """Persist one repaired wave row: validate, WAL, write, append, publish.

    Every repair verb in this module commits the same way, mirroring the
    spec-sync transaction; only the audit envelope differs, so the caller
    supplies it as a function of the post-mutation state digest.

    Args:
        ctx: Server context (publish bus).
        state: The mutated state, written as-is.
        paths: ``(state_path, event_path, wal_path)``.
        idempotency_key: Caller's retry key, recorded on the WAL row.
        before_version: State digest before the mutation.
        build_envelope: Builds the event envelope from the after digest.

    Returns:
        ``(after_version, envelope)`` for the result payload.

    Raises:
        DaemonValidationError: The post-mutation state fails schema or
            invariant validation; nothing is written.
    """
    state_path, event_path, wal_path = paths
    state.updated_at = datetime.now(UTC)
    new_payload = state.model_dump(mode="json")
    after_version = validate_post_sync(new_payload)

    mutation_id = uuid.uuid4().hex
    envelope = build_envelope(after_version)
    record = WalRecord(
        record_id=mutation_id,
        envelope=envelope,
        idempotency_key=idempotency_key,
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
    return after_version, envelope


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

    after_version, envelope = _commit_repair(
        ctx,
        state=state,
        paths=(state_path, event_path, wal_path),
        idempotency_key=args.idempotency_key,
        before_version=before_version,
        build_envelope=lambda after: _build_repoint_envelope(
            wave_id=args.wave_id,
            changed_count=len(report.changed),
            changed_gate_ids=[change.gate_id for change in report.changed],
            before_version=before_version,
            after_version=after,
        ),
    )
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


class RepointScopesParams(BaseModel):
    """Params for :func:`repoint_scopes`.

    Attributes:
        wave_id: ``P##-I##-W##`` -- the wave whose recorded row is
            repointed. Phase / iter scopes are refused: a repoint is
            record repair and is authorised one wave at a time.
        from_commit: When ``True`` the wave's ``file_scopes`` are
            re-derived from its pinned commit (CLOSED waves only).
        criterion_texts: Per-criterion prose rewrites; may be empty when
            only the scopes are repointed.
        reason: Why the prose rewrite is warranted. Required (non-blank)
            whenever *criterion_texts* is non-empty, and recorded on the
            event row so the repair is auditable.
        dry_run: When ``True`` the repoint is computed against an
            in-memory copy and reported with no state write.
        repo_root: Optional absolute repo working-tree path (default cwd).
            Also the tree the ``from_commit`` derivation reads.
        idempotency_key: Optional caller-supplied retry key.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    from_commit: bool = False
    criterion_texts: list[CriterionTextRepoint] = Field(default_factory=list)
    reason: str | None = None
    dry_run: bool = False
    repo_root: str | None = None
    idempotency_key: str | None = None


class SpecRepointScopesResult(BaseModel):
    """Result shape for the :func:`repoint_scopes` RPC."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    wave_id: str
    dry_run: bool
    scopes_changed: bool
    scopes_before: list[str]
    scopes_after: list[str]
    changed_count: int
    changed_criteria: list[dict[str, Any]]
    unchanged_criterion_ids: list[str]
    reason: str | None
    before_version: str | None
    after_version: str | None
    envelope: dict[str, Any] | None
    idempotent_replay: bool = False


def _apply_scope_repoint(
    state: State,
    args: RepointScopesParams,
    *,
    repo_root: Path,
) -> ScopeRepointReport:
    """Run the bounded record repoint against *state*, mapping refusals to RPC errors.

    Args:
        state: Loaded state to mutate in place.
        args: Validated repoint params.
        repo_root: Working tree the pinned-commit derivation reads.

    Returns:
        The lifecycle report naming what moved.

    Raises:
        DaemonValidationError: When the wave is unknown, its status does
            not permit the requested leg, a text repoint carries no
            reason, a named criterion is not recorded, the derivation
            fails, or the replacement would change anything other than
            the file scopes and the named criterion text (``-32002``).
    """
    if args.wave_id not in state.waves:
        raise DaemonValidationError(f"validation_failed: unknown wave: {args.wave_id!r}")
    try:
        return repoint_closed_wave_record(
            state,
            wave_id=args.wave_id,
            scope_source=repo_root if args.from_commit else None,
            criterion_texts=list(args.criterion_texts),
            reason=args.reason,
        )
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


@register("spec.repoint_scopes")
async def repoint_scopes(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Repoint a wave's file scopes and named criterion text, and nothing else.

    The audited repair path for a wave row whose recorded ``file_scopes``
    its own pinned commit contradicts, or whose criterion prose names the
    wrong thing (rule 4: the daemon is the canonical writer). The wave
    keeps its status, its outcome, its commit pin and its gates; the
    lifecycle guard refuses the whole mutation when anything outside the
    requested fields moves.

    Args:
        ctx: Server context. ``ctx.wal_dir`` MUST be configured for a
            non-dry-run call.
        params: JSON-RPC params per :class:`RepointScopesParams`.

    Returns:
        Dict matching :class:`SpecRepointScopesResult`; ``dry_run=True``
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
        args = RepointScopesParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    try:
        kind = spec_writer.classify_scope(args.wave_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    if kind != "wave":
        raise ValueError(
            f"validation_failed: scope repoint targets a wave scope, got {args.wave_id!r}"
        )

    replay = idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"repoint_scopes idempotent_replay wave={args.wave_id!r}")
        return replay

    state_path, event_path, wal_path = resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )
    # The pinned commit is read from the tree that owns the resolved state
    # file (``<root>/.ea/state.json``), never the daemon's cwd: a
    # cross-root serve must derive scopes from the repo it is mutating.
    repo_root = state_path.parent.parent

    if args.dry_run:
        state, _payload = read_state(state_path)
        report = _apply_scope_repoint(state.model_copy(deep=True), args, repo_root=repo_root)
        return _scope_repoint_result(args, report, before=None, after=None, envelope=None)

    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            result = _apply_scope_repoint_locked(
                ctx,
                args=args,
                state_path=state_path,
                event_path=event_path,
                wal_path=wal_path,
                repo_root=repo_root,
            )
        cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


def _apply_scope_repoint_locked(
    ctx: MethodContext,
    *,
    args: RepointScopesParams,
    state_path: Path,
    event_path: Path,
    wal_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Run the locked record repoint transaction: repoint, validate, write.

    The caller holds the state-path portalock. Mirrors the gate-repoint
    transaction (WAL -> atomic write -> event append -> publish); when the
    record already matches the request, nothing is written and the report
    alone is returned so a replayed repair is a true no-op.

    Args:
        ctx: Server context (publish bus).
        args: Validated repoint params.
        state_path: Path to ``state.json``.
        event_path: Path to the event JSONL store.
        wal_path: Path to the daemon WAL directory.
        repo_root: Working tree the pinned-commit derivation reads.

    Returns:
        Dict matching :class:`SpecRepointScopesResult`.

    Raises:
        DaemonValidationError: When the repoint is refused or the
            post-mutation state fails schema / invariant validation.
    """
    state, _payload = read_state(state_path)
    before_version = state_version(state.model_dump(mode="json"))
    report = _apply_scope_repoint(state, args, repo_root=repo_root)
    if not report.changed_criteria and not report.scopes_changed:
        return _scope_repoint_result(
            args, report, before=before_version, after=before_version, envelope=None
        )

    after_version, envelope = _commit_repair(
        ctx,
        state=state,
        paths=(state_path, event_path, wal_path),
        idempotency_key=args.idempotency_key,
        before_version=before_version,
        build_envelope=lambda after: _build_scope_repoint_envelope(
            wave_id=args.wave_id,
            report=report,
            reason=args.reason,
            before_version=before_version,
            after_version=after,
        ),
    )
    logger.info(
        f"repoint_scopes ok wave={args.wave_id} scopes_changed={report.scopes_changed} "
        f"texts_changed={len(report.changed_criteria)} "
        f"before={before_version} after={after_version}"
    )
    return _scope_repoint_result(
        args,
        report,
        before=before_version,
        after=after_version,
        envelope=envelope.model_dump(mode="json"),
    )


def _scope_repoint_result(
    args: RepointScopesParams,
    report: ScopeRepointReport,
    *,
    before: str | None,
    after: str | None,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the :class:`SpecRepointScopesResult` payload dict.

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
    return SpecRepointScopesResult(
        operation="repoint_scopes",
        wave_id=args.wave_id,
        dry_run=args.dry_run,
        scopes_changed=report.scopes_changed,
        scopes_before=list(report.scopes_before),
        scopes_after=list(report.scopes_after),
        changed_count=len(report.changed_criteria),
        changed_criteria=[change.model_dump(mode="json") for change in report.changed_criteria],
        unchanged_criterion_ids=list(report.unchanged_criterion_ids),
        reason=args.reason,
        before_version=before,
        after_version=after,
        envelope=envelope,
    ).model_dump(mode="json")


def _build_scope_repoint_envelope(
    *,
    wave_id: str,
    report: ScopeRepointReport,
    reason: str | None,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical event envelope for a record repoint.

    The envelope is what makes the repair audited rather than silent: it
    names the wave, whether the scopes moved, the exact criterion ids
    whose prose moved and the operator's reason for moving it, so the
    event log alone reconstructs which records were repaired and why.

    Args:
        wave_id: The wave that was repointed.
        report: The lifecycle report naming what moved.
        reason: The operator's reason, carried verbatim.
        before_version: State digest before the write.
        after_version: State digest after the write.

    Returns:
        The canonical event envelope, ready for the WAL + event log.
    """
    now = datetime.now(UTC)
    changed_ids = [change.criterion_id for change in report.changed_criteria]
    summary = (
        f"spec.repoint_scopes wave={wave_id} scopes_changed={report.scopes_changed} "
        f"criteria={len(changed_ids)}"
    )
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.spec_repoint_scopes",
        actor="daemon",
        command="spec.repoint_scopes",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras={
            "scopes_changed": report.scopes_changed,
            # ``extras`` is a scalar map, so the id list rides as a joined
            # string rather than being dropped from the audit trail.
            "changed_criterion_ids": ",".join(changed_ids),
            "reason": reason or "",
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


class RewriteGateKindParams(BaseModel):
    """Params for :func:`rewrite_gate_kind`.

    Attributes:
        wave_id: ``P##-I##-W##`` -- the CLOSED wave whose grep gates are
            strengthened. Phase / iter scopes are refused.
        rewrites: Per-gate rewrites or additions; at least one.
        reason: Why the record is strengthened; recorded on the event row.
        dry_run: When ``True`` the rewrite is computed against an
            in-memory copy and reported with no state write.
        repo_root: Optional absolute repo working-tree path (default cwd).
        idempotency_key: Optional caller-supplied retry key.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    rewrites: list[GateKindRewrite] = Field(min_length=1)
    reason: str | None = None
    dry_run: bool = False
    repo_root: str | None = None
    idempotency_key: str | None = None


class SpecRewriteGateKindResult(BaseModel):
    """Result shape for the :func:`rewrite_gate_kind` RPC."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    wave_id: str
    dry_run: bool
    changed_count: int
    changed: list[dict[str, Any]]
    unchanged_gate_ids: list[str]
    reason: str | None
    before_version: str | None
    after_version: str | None
    envelope: dict[str, Any] | None
    idempotent_replay: bool = False


def _apply_kind_rewrite(state: State, args: RewriteGateKindParams) -> GateKindRewriteReport:
    """Run the bounded kind rewrite against *state*, mapping refusals to RPC errors.

    Raises:
        DaemonValidationError: The wave is unknown or not CLOSED, the
            reason is blank, or the lifecycle guard refuses a rewrite
            (mapped to ``-32002``).
    """
    if args.wave_id not in state.waves:
        raise DaemonValidationError(f"validation_failed: unknown wave: {args.wave_id!r}")
    try:
        return rewrite_closed_wave_gate_kinds(
            state,
            wave_id=args.wave_id,
            rewrites=list(args.rewrites),
            reason=args.reason,
        )
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


@register("spec.rewrite_gate_kind")
async def rewrite_gate_kind(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Strengthen a CLOSED wave's grep gates into command gates, and nothing else.

    The audited path for a verification record whose gates cannot prove
    their criteria: a grep-style gate becomes a ``command_exit_zero`` gate,
    or a criterion with no gate gains one. The reverse direction does not
    exist; the lifecycle guard refuses any weakening and any movement
    outside the touched gates and their criteria's proof fields.

    Args:
        ctx: Server context. ``ctx.wal_dir`` MUST be configured for a
            non-dry-run call.
        params: JSON-RPC params per :class:`RewriteGateKindParams`.

    Returns:
        Dict matching :class:`SpecRewriteGateKindResult`.

    Raises:
        ValueError: When the params do not validate or *wave_id* is not a
            wave scope (mapped to ``-32602``).
        DaemonValidationError: When the rewrite is refused or the
            post-mutation state fails validation (mapped to ``-32002``).
    """
    try:
        args = RewriteGateKindParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    try:
        kind = spec_writer.classify_scope(args.wave_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    if kind != "wave":
        raise ValueError(
            f"validation_failed: gate-kind rewrite targets a wave scope, got {args.wave_id!r}"
        )

    replay = idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"rewrite_gate_kind idempotent_replay wave={args.wave_id!r}")
        return replay

    state_path, event_path, wal_path = resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    if args.dry_run:
        state, _payload = read_state(state_path)
        report = _apply_kind_rewrite(state.model_copy(deep=True), args)
        return _kind_rewrite_result(args, report, before=None, after=None, envelope=None)

    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, _payload = read_state(state_path)
            before_version = state_version(state.model_dump(mode="json"))
            report = _apply_kind_rewrite(state, args)
            if not report.changed:
                result = _kind_rewrite_result(
                    args, report, before=before_version, after=before_version, envelope=None
                )
            else:
                after_version, envelope = _commit_repair(
                    ctx,
                    state=state,
                    paths=(state_path, event_path, wal_path),
                    idempotency_key=args.idempotency_key,
                    before_version=before_version,
                    build_envelope=lambda after: _build_kind_rewrite_envelope(
                        wave_id=args.wave_id,
                        report=report,
                        reason=args.reason or "",
                        before_version=before_version,
                        after_version=after,
                    ),
                )
                logger.info(
                    f"rewrite_gate_kind ok wave={args.wave_id} changed={len(report.changed)} "
                    f"before={before_version} after={after_version}"
                )
                result = _kind_rewrite_result(
                    args,
                    report,
                    before=before_version,
                    after=after_version,
                    envelope=envelope.model_dump(mode="json"),
                )
        cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


def _kind_rewrite_result(
    args: RewriteGateKindParams,
    report: GateKindRewriteReport,
    *,
    before: str | None,
    after: str | None,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the :class:`SpecRewriteGateKindResult` payload dict."""
    return SpecRewriteGateKindResult(
        operation="rewrite_gate_kind",
        wave_id=args.wave_id,
        dry_run=args.dry_run,
        changed_count=len(report.changed),
        changed=[change.model_dump(mode="json") for change in report.changed],
        unchanged_gate_ids=list(report.unchanged_gate_ids),
        reason=args.reason,
        before_version=before,
        after_version=after,
        envelope=envelope,
    ).model_dump(mode="json")


def _build_kind_rewrite_envelope(
    *,
    wave_id: str,
    report: GateKindRewriteReport,
    reason: str,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical event envelope for a gate-kind rewrite.

    The event log is the append-only record of the repair: it names every
    gate that moved, the kind it moved from (``added`` for a new gate) and
    the operator's reason, so a reader can reconstruct why a closed
    wave's verification record differs from the one it closed with.
    """
    now = datetime.now(UTC)
    moves = [
        f"{change.gate_id}:{change.before_kind or 'added'}->{change.after_kind}"
        for change in report.changed
    ]
    summary = f"spec.rewrite_gate_kind wave={wave_id} changed={len(report.changed)}"
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.spec_rewrite_gate_kind",
        actor="daemon",
        command="spec.rewrite_gate_kind",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras={
            "changed_count": len(report.changed),
            # ``extras`` is a scalar map, so the moves ride as a joined string.
            "gate_kind_moves": ",".join(moves),
            "reason": reason,
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
    "RepointScopesParams",
    "RewriteGateKindParams",
    "SpecRepointGatesResult",
    "SpecRepointScopesResult",
    "SpecRewriteGateKindResult",
    "repoint_gates",
    "repoint_scopes",
    "rewrite_gate_kind",
]
