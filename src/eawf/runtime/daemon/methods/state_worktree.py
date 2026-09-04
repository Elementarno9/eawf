"""Daemon-owned worktree landing and ``track.*`` mutators.

``wave land`` / ``wave autoland`` / ``track sync`` mutate ``state.json``
through a generic commit wrapper rather than the ``state.mutate``
transaction: each runs an arbitrary in-process mutator under the same
portalock + WAL + event-append discipline the canonical mutator uses.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import orjson

from eawf.kernel.state.enums import (
    StoreKind,
)
from eawf.kernel.state.models import (
    State,
    Track,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.validate.strict import validate_state
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    register,
)
from eawf.runtime.daemon.wal import WalRecord

if TYPE_CHECKING:
    pass
from eawf.runtime.daemon.methods.state_context import (
    bus_for_root,
    read_state,
    resolve_mutator_paths,
    state_version,
)
from eawf.runtime.daemon.methods.state_models import (
    TrackSyncParams,
    TrackSyncRpcResult,
    WaveAutolandParams,
    WaveAutolandRpcResult,
    WaveLandBatchParams,
    WaveLandBatchRpcResult,
    WaveLandParams,
    WaveLandRpcResult,
)

logger = logging.getLogger(__name__)


def _wave_land_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one wave-land result."""
    return WaveLandRpcResult(
        wave=result.wave_id,
        commits=list(result.commits),
        outcome=result.outcome,
        closed=result.closed,
        worktree_cleaned=result.worktree_cleaned,
        merged_commit=result.merged_commit,
        integration_id=result.integration_id,
        close_attempt=None,
        close_backgrounded=False,
    ).model_dump(mode="json")


def _wave_land_batch_payload(
    result: Any,
    *,
    close_mode: Literal["durable_async", "daemonless_synchronous"],
) -> dict[str, Any]:
    """Return the JSON-mode result shape for a wave-land-batch result."""
    return WaveLandBatchRpcResult(
        landed=[WaveLandRpcResult.model_validate(_wave_land_payload(row)) for row in result.landed],
        failed_wave=result.failed_wave,
        error=result.error,
        skipped=list(result.skipped),
        barrier_requirements={
            wave_id: list(stages) for wave_id, stages in result.barrier_requirements.items()
        },
        close_mode=close_mode,
    ).model_dump(mode="json")


def _wave_autoland_row_payload(row: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one autoland row."""
    return {
        "wave": row.wave_id,
        "commits": list(row.commits),
        "merged_commit": row.merged_commit,
        "worktree_cleaned": row.worktree_cleaned,
    }


def _wave_autoland_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for a wave-autoland result."""
    return WaveAutolandRpcResult(
        order=list(result.order),
        landed=[_wave_autoland_row_payload(row) for row in result.landed],
        failed_wave=result.failed_wave,
        error=result.error,
        remaining=list(result.remaining),
        dry_run=result.dry_run,
    ).model_dump(mode="json")


def _build_worktree_event_envelope(
    *,
    command: str,
    scope_id: str | None,
    params: dict[str, Any],
    result: dict[str, Any],
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical event row for daemon-owned worktree mutations."""
    now = datetime.now(UTC)
    summary = f"{command} scope={scope_id}"
    args_raw = orjson.dumps(params, option=orjson.OPT_SORT_KEYS)
    extras: dict[str, str | int | float | bool] = {}
    if command == "state.wave_land":
        extras = {
            "closed": bool(result.get("closed", False)),
            "worktree_cleaned": bool(result.get("worktree_cleaned", False)),
            "commit_count": len(result.get("commits", [])),
        }
    elif command == "state.wave_land_batch":
        extras = {
            "landed_count": len(result.get("landed", [])),
            "failed": result.get("failed_wave") is not None,
            "skipped_count": len(result.get("skipped", [])),
        }
    elif command == "state.wave_autoland":
        extras = {
            "landed_count": len(result.get("landed", [])),
            "failed": result.get("failed_wave") is not None,
            "remaining_count": len(result.get("remaining", [])),
            "dry_run": bool(result.get("dry_run", False)),
        }
    payload = EventPayload(
        timestamp=now,
        event_type=command,
        event_kind=None,
        actor="daemon",
        command=command,
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status="warn" if result.get("failed_wave") is not None else "ok",
        message=summary,
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def commit_worktree_state(
    *,
    ctx: MethodContext,
    repo_root: Path | None,
    params: dict[str, Any],
    command: str,
    scope_id: str | None,
    apply_func: Callable[[State], dict[str, Any]],
) -> dict[str, Any]:
    """Run a daemon-owned mutator under canonical state persistence.

    When *repo_root* is ``None`` the mutator paths resolve via the
    boot-time ``ctx.state_path`` anchor (the legacy / in-process test
    fallback). A real *repo_root* routes the state + event writes to that
    repo, matching the per-request anchoring the worktree-land handlers use.
    """
    from eawf.runtime.lock import portalock
    from eawf.surfaces.cli import errors as cli_errors

    state_path, event_path, wal_path = resolve_mutator_paths(
        repo_root=str(repo_root) if repo_root is not None else None,
        ctx=ctx,
    )
    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, payload = read_state(state_path)
            before_version = state_version(payload)
            try:
                result = apply_func(state)
            except cli_errors.ValidationError as exc:
                raise DaemonValidationError(f"validation_failed: {exc}") from exc
            except cli_errors.CliError as exc:
                raise ValueError(str(exc)) from exc

            state.updated_at = datetime.now(UTC)
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise DaemonValidationError(
                    "validation_failed: post-mutation schema invalid: "
                    + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise DaemonValidationError(
                    f"validation_failed: post-mutation invariants violated: {violation_codes}"
                )
            after_version = state_version(new_payload)
            envelope = _build_worktree_event_envelope(
                command=command,
                scope_id=scope_id,
                params=params,
                result=result,
                before_version=before_version,
                after_version=after_version,
            )
            record_id = uuid.uuid4().hex
            record = WalRecord(
                record_id=record_id,
                envelope=envelope,
                idempotency_key=None,
                written_at=datetime.now(UTC),
                before_state_version=before_version,
                after_state_version=after_version,
                state_path=str(state_path),
            )
            wal.write_pending(wal_path, record)
            atomic_write_json_locked(state_path, new_payload)
            wal.mark_applied(wal_path, record_id)
            append_envelope(event_path, envelope)
            wal.mark_fsynced(wal_path, record_id)
            bus = bus_for_root(ctx, state_path)
            if bus is not None:
                bus.publish(envelope)
            ctx.last_event_id = envelope.id
            logger.info(
                f"commit_worktree_state command={command!r} scope_id={scope_id!r} "
                f"before={before_version} "
                f"after={after_version} envelope_id={envelope.id!r}"
            )
            return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


@register("state.wave_land")
async def wave_land_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned implementation of ``eawf wave land``."""
    args = WaveLandParams.model_validate(params)
    repo_root = Path(args.repo_root)

    from eawf.runtime.worktree import wave_land, worktree_registry_lock

    with worktree_registry_lock(repo_root, timeout=5.0):
        landed = commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params=params,
            command="state.wave_land",
            scope_id=args.wave_id,
            apply_func=lambda state: _wave_land_payload(
                wave_land(
                    state,
                    repo_root=repo_root,
                    wave_id=args.wave_id,
                    outcome=args.outcome,
                    keep_worktree=args.keep_worktree,
                    defer_close=True,
                )
            ),
        )
    from eawf.runtime.daemon.methods.close import submit as submit_close

    submitted = await submit_close(
        ctx,
        {
            "wave_id": args.wave_id,
            "outcome": landed["outcome"],
            "commit": landed["merged_commit"],
            "repo_root": str(repo_root),
        },
    )
    landed["close_attempt"] = submitted["attempt"]
    landed["close_backgrounded"] = submitted["backgrounded"]
    return WaveLandRpcResult.model_validate(landed).model_dump(mode="json")


@register("state.wave_land_batch")
async def wave_land_batch_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Integrate a batch, then submit each durable close outside the state lock."""
    args = WaveLandBatchParams.model_validate(params)
    repo_root = Path(args.repo_root)

    from eawf.runtime.worktree import wave_land_batch, worktree_registry_lock

    with worktree_registry_lock(repo_root, timeout=5.0):
        landed = commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params=params,
            command="state.wave_land_batch",
            scope_id=args.iter_id,
            apply_func=lambda state: _wave_land_batch_payload(
                wave_land_batch(
                    state,
                    repo_root=repo_root,
                    iter_id=args.iter_id,
                    ready_only=args.ready_only,
                    keep_worktree=args.keep_worktree,
                    defer_close=True,
                ),
                close_mode="durable_async",
            ),
        )
    from eawf.runtime.daemon.methods.close import submit as submit_close

    for row in landed["landed"]:
        submitted = await submit_close(
            ctx,
            {
                "wave_id": row["wave"],
                "outcome": row["outcome"],
                "commit": row["merged_commit"],
                "repo_root": str(repo_root),
            },
        )
        row["close_attempt"] = submitted["attempt"]
        row["close_backgrounded"] = submitted["backgrounded"]
    return WaveLandBatchRpcResult.model_validate(landed).model_dump(mode="json")


@register("state.wave_autoland")
async def wave_autoland_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned implementation of ``eawf wave autoland``."""
    args = WaveAutolandParams.model_validate(params)
    repo_root = Path(args.repo_root)

    from eawf.runtime.worktree import wave_autoland, worktree_registry_lock

    with worktree_registry_lock(repo_root, timeout=5.0):
        return commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params=params,
            command="state.wave_autoland",
            scope_id=args.iter_id,
            apply_func=lambda state: _wave_autoland_payload(
                wave_autoland(
                    state,
                    repo_root=repo_root,
                    iter_id=args.iter_id,
                    keep_worktree=args.keep_worktree,
                    dry_run=args.dry_run,
                )
            ),
        )


# ---- track.* mutators --------------------------------------------------------


def _tracks(state: State) -> dict[str, Track]:
    """Return ``state.tracks`` as a non-``None`` dict in place.

    Creates a fresh empty dict on the state when the field is currently
    ``None`` so a first ``track.add`` has somewhere to land.
    """
    if state.tracks is None:
        state.tracks = {}
    return state.tracks


def _apply_track_sync(state: State, args: TrackSyncParams) -> dict[str, Any]:
    """Recompute a Track's measured outcome statuses from their samples.

    Resolves the target Track (the explicit ``track_id`` param, else the
    :attr:`CurrentPointers.track_id` cursor) and runs the
    :func:`eawf.workflow.evidence.outcome.sync_track_outcomes` reducer -- the
    same reducer the wave-close hook fires -- so an operator can re-derive the
    standings on demand. An absent target Track (no id and no cursor) yields a
    typed no-op result with an empty change list rather than raising, so
    ``track sync`` on a repo with no Track in focus is harmless.

    Args:
        state: Loaded :class:`State`. Mutated in place by the reducer.
        args: Validated :class:`TrackSyncParams`.

    Returns:
        Result dict matching :class:`TrackSyncRpcResult`.
    """
    from eawf.workflow.evidence.outcome import sync_track_outcomes

    track_id = args.track_id if args.track_id else state.current.track_id
    changed = sync_track_outcomes(state, track_id=track_id) if track_id else []
    logger.info(f"_apply_track_sync track={track_id!r} changed={len(changed)}")
    return TrackSyncRpcResult(
        track_id=track_id,
        changed_outcome_ids=changed,
        changed=len(changed),
    ).model_dump(mode="json")


@register("track.sync")
async def track_sync_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned ``track.sync`` mutator.

    Recomputes a Track's measured outcome statuses from their samples via the
    same reducer the wave-close hook fires. The daemon is the sole canonical
    mutator (AGENTS rule 4); the CLI ``track sync`` shim routes here over
    JSON-RPC.
    """
    args = TrackSyncParams.model_validate(params)
    repo_root = Path(args.repo_root) if args.repo_root else None
    return commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=params,
        command="track.sync",
        scope_id=args.track_id,
        apply_func=lambda state: _apply_track_sync(state, args),
    )
