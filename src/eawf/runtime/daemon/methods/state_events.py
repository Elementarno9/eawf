"""Canonical event-envelope construction for the state-method family.

Every state mutation, elapsed tick, and drift alarm converges on the same
on-disk ``StoreKind.EVENT`` row shape, so a subscriber cannot tell whether
an envelope came from the daemon or the in-process fallback. This module
owns those builders plus the advisory extras they carry.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from eawf.kernel.state.enums import (
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    State,
)
from eawf.kernel.state.mutations import (
    Mutation,
    MutationKind,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventKind, EventPayload
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
)

if TYPE_CHECKING:
    pass
from eawf.runtime.daemon.methods.state_context import args_hash, bus_for_root
from eawf.workflow.estimation.thresholds import classify_band, wave_budget_minutes

logger = logging.getLogger(__name__)


#: Active-wave elapsed updates are coarse-grained to one event per wave per
#: elapsed minute. The in-memory cache suppresses repeated digest polls inside
#: the same minute; a daemon restart may re-emit the current minute, which is
#: acceptable for a live advisory stream.
_WAVE_ELAPSED_ACTIVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)
_WAVE_ELAPSED_LAST_MINUTE: dict[int, dict[str, int]] = {}
#: The cache is read-then-written by every digest poll, and digest polls now
#: hand their parse to a worker thread, so two polls over the same wave can
#: reach the check-and-set concurrently. A process-wide lock keeps the pair
#: atomic: without it both callers observe the stale minute and each appends
#: an update for the same minute boundary.
_WAVE_ELAPSED_CACHE_LOCK: Final[threading.Lock] = threading.Lock()

# ---- Envelope construction --------------------------------------------------


#: Map :class:`MutationKind` -> closed :data:`EventKind` literal so the
#: post-mutation envelope carries a typed ``event_kind`` discriminator
#: (P28-I02-W03). Kinds not in this table land with ``event_kind=None``
#: during the v0.3-v0.5 migration window — the field is optional on
#: :class:`EventPayload` until every emitter is migrated, at which point
#: v0.5+ governance flips it to non-optional. Wave claim/close are wired
#: first because runtime subscribers use them to track active work and
#: close-time actuals.
MUTATION_EVENT_KIND: Final[dict[MutationKind, EventKind]] = {
    MutationKind.WAVE_CLAIM: "wave_claimed",
    MutationKind.WAVE_CLOSE: "wave_closed",
    MutationKind.PHASE_ACTIVATE: "phase_activated",
    MutationKind.ITER_CLOSE: "iter_closed",
    MutationKind.PHASE_CLOSE: "phase_closed",
}


def bucket_drift_extras(state: State) -> dict[str, str | int | float | bool]:
    """Return bucket calibration drift extras, or empty when no drift fires."""
    from eawf.workflow.estimation.buckets import calibrate_buckets

    report = calibrate_buckets(state)
    nudged = [row for row in report.buckets if row.nudge]
    if not nudged:
        return {}
    max_drift = max(row.drift_pct or 0.0 for row in nudged)
    sample_count = sum(row.sample_count for row in report.buckets)
    return {
        "bucket_drift": True,
        "bucket_drift_count": len(nudged),
        "bucket_drift_max_pct": max_drift,
        "bucket_drift_samples": sample_count,
        "bucket_drift_buckets": ",".join(row.bucket.value for row in nudged),
    }


def _wave_elapsed_cache(ctx: MethodContext) -> dict[str, int]:
    """Return the daemon-local ``wave_id -> elapsed_minute`` publish cache."""
    return _WAVE_ELAPSED_LAST_MINUTE.setdefault(id(ctx), {})


def claim_elapsed_minute(*, ctx: MethodContext, cache_key: str, elapsed_minute: int) -> bool:
    """Claim *elapsed_minute* for *cache_key*, returning True on first claim.

    The whole check-and-set runs under :data:`_WAVE_ELAPSED_CACHE_LOCK`, so
    exactly one of any number of concurrent callers wins a given minute
    boundary and the losers skip their append.

    Args:
        ctx: Server context; keys the daemon-local publish cache.
        cache_key: Per-root wave key, since wave ids repeat across repos.
        elapsed_minute: Whole elapsed minutes since the wave was claimed.

    Returns:
        True when this caller claimed the minute and must publish,
        False when another caller already published it.
    """
    with _WAVE_ELAPSED_CACHE_LOCK:
        cache = _wave_elapsed_cache(ctx)
        if cache.get(cache_key) == elapsed_minute:
            return False
        cache[cache_key] = elapsed_minute
        return True


def _wave_elapsed_band(elapsed_minutes: float, budget_minutes: float | None) -> str:
    """Classify elapsed time against the shared budget bands.

    Banding is delegated so this publisher and the stale-wave advisory agree at the
    boundaries: a wave sitting exactly on a band edge must not read one way here and
    another way there.
    """
    if budget_minutes is None or budget_minutes <= 0:
        return "ok"
    return classify_band(elapsed_minutes / budget_minutes)


def _build_wave_elapsed_envelope(
    *,
    wave_id: str,
    elapsed_minute: int,
    elapsed_minutes: float,
    budget_minutes: float | None,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build one ``wave_elapsed_update`` event envelope."""
    now = datetime.now(UTC)
    band = _wave_elapsed_band(elapsed_minutes, budget_minutes)
    status = "error" if band == "err" else band
    ratio = elapsed_minutes / budget_minutes if budget_minutes else 0.0
    args_raw = f"{wave_id}:{elapsed_minute}".encode()
    extras: dict[str, str | int | float | bool] = {
        "wave_id": wave_id,
        "elapsed_minute": elapsed_minute,
        "elapsed_minutes": round(elapsed_minutes, 4),
        "elapsed_band": band,
    }
    if budget_minutes is not None:
        extras["elapsed_budget_minutes"] = round(budget_minutes, 4)
        extras["elapsed_ratio"] = round(ratio, 4)
        extras["elapsed_percent"] = round(ratio * 100.0, 2)
    summary = f"wave_elapsed_update wave={wave_id} minute={elapsed_minute}"
    payload = EventPayload(
        timestamp=now,
        event_type="wave_elapsed_update",
        event_kind="wave_elapsed_update",
        actor="daemon",
        command="state.digest.wave_elapsed_update",
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status=status,
        message=summary,
        extras=extras,
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


def publish_wave_elapsed_updates(
    *,
    ctx: MethodContext,
    state: State,
    state_path: Path,
    event_path: Path,
    version: str,
    now: datetime,
) -> None:
    """Append + publish at most one elapsed update per active wave minute.

    Anchors on ``claimed_at`` (work-start), not ``opened_at``
    (plan/creation): a wave planned long before it is claimed must not
    publish an inflated elapsed clock. A wave without a ``claimed_at``
    (no work-start fact) is skipped, so no elapsed update fires for it.
    """
    bus = bus_for_root(ctx, state_path)
    for wave in state.waves.values():
        if wave.status not in _WAVE_ELAPSED_ACTIVE_STATUSES or wave.claimed_at is None:
            continue
        elapsed_seconds = (now - wave.claimed_at).total_seconds()
        if elapsed_seconds < 60.0:
            continue
        elapsed_minute = int(elapsed_seconds // 60)
        # Wave ids repeat across repos (every repo has a P01-W01), so the
        # dedup cache is keyed per root as well.
        cache_key = f"{state_path}:{wave.id}"
        if not claim_elapsed_minute(ctx=ctx, cache_key=cache_key, elapsed_minute=elapsed_minute):
            continue
        elapsed_minutes = elapsed_seconds / 60.0
        envelope = _build_wave_elapsed_envelope(
            wave_id=wave.id,
            elapsed_minute=elapsed_minute,
            elapsed_minutes=elapsed_minutes,
            budget_minutes=wave_budget_minutes(state, wave.id),
            before_version=version,
            after_version=version,
        )
        append_envelope(event_path, envelope)
        if bus is not None:
            bus.publish(envelope)
        ctx.last_event_id = envelope.id
        logger.info(f"wave_elapsed_update wave={wave.id!r} minute={elapsed_minute}")


def build_event_envelope(
    *,
    mutation: Mutation,
    before_version: str,
    after_version: str,
    extras: dict[str, str | int | float | bool] | None = None,
) -> Envelope:
    """Build the canonical ``StoreKind.EVENT`` envelope for *mutation*.

    The envelope shape mirrors :func:`eawf.surfaces.cli.commands.lifecycle._append_event`
    so subscribers cannot tell whether the envelope was produced via
    the daemon or the daemonless fallback — both paths converge on
    the same on-disk row.

    Args:
        mutation: The mutation just applied; supplies ``kind`` /
            ``scope_id`` / params hash.
        before_version: State digest before the apply.
        after_version: State digest after the apply.
        extras: Optional rolled-up advisory metrics to surface on the
            envelope's :attr:`EventPayload.extras` map. Today the
            ``WAVE_CLOSE`` path populates this (W06
            ``readiness_warnings_count`` + the P28-I02-W03
            ``actual_tokens`` / ``actual_cost_usd`` rollup from the
            close-time ActualSummary); future verify-spine waves may
            extend the set (compile-gate fail count, waiver count,
            etc.). Additive — existing subscribers ignore unknown
            extras.
    """
    now = datetime.now(UTC)
    summary = f"state.mutate {mutation.kind.value} scope={mutation.scope_id}"
    payload = EventPayload(
        timestamp=now,
        event_type=f"state.mutate.{mutation.kind.value}",
        event_kind=MUTATION_EVENT_KIND.get(mutation.kind),
        actor="daemon",
        command=f"state.mutate.{mutation.kind.value}",
        args_hash=args_hash(mutation),
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras=dict(extras) if extras else {},
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=mutation.scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def build_bucket_drift_envelope(
    *,
    mutation: Mutation,
    before_version: str,
    after_version: str,
    extras: dict[str, str | int | float | bool],
) -> Envelope:
    """Build the ``bucket_drift_detected`` event envelope."""
    now = datetime.now(UTC)
    summary = f"bucket_drift_detected scope={mutation.scope_id}"
    payload = EventPayload(
        timestamp=now,
        event_type="bucket_drift_detected",
        event_kind="bucket_drift_detected",
        actor="daemon",
        command=f"state.mutate.{mutation.kind.value}",
        args_hash=args_hash(mutation),
        before_state_version=before_version,
        after_state_version=after_version,
        status="warn",
        message=summary,
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=mutation.scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def mutation_event_extras(state: State, mutation: Mutation) -> dict[str, str | int | float | bool]:
    """Return the per-kind ``EventPayload.extras`` map for a committed mutation.

    Computed AFTER the apply succeeds so the rolled-up view reflects the state
    the event describes. A kind with nothing to surface gets an empty dict,
    which keeps the envelope shape uniform across every mutation kind.
    """
    extras: dict[str, str | int | float | bool] = {}
    if mutation.kind is MutationKind.WAVE_CLAIM:
        claimed_wave_id = str(mutation.params["wave_id"])
        claim_session_id = state.waves[claimed_wave_id].claim_session_id
        if claim_session_id is None:  # pragma: no cover - claim transition guarantees it
            raise DaemonValidationError(
                f"validation_failed: claimed wave has no session: {claimed_wave_id!r}"
            )
        # The daemon executed the mutation, so actor remains ``daemon``.
        # This additive reference records which already-validated live
        # session gained the wave without claiming that session
        # authenticated the state.mutate request.
        extras["claim_session_id"] = claim_session_id
    elif mutation.kind is MutationKind.ITER_CLOSE:
        from eawf.kernel.state.enums import AuditVerdict
        from eawf.workflow.lifecycle._audit_acceptance import (
            AUDIT_MINOR_BACKLOG_TRIAGE,
        )

        audit_id = str(mutation.params["audit_id"])
        audit = (state.audits or {}).get(audit_id)
        extras["audit_id"] = audit_id
        extras["require_audit_accepted"] = bool(
            mutation.params.get("require_audit_accepted", False)
        )
        if (
            bool(mutation.params.get("require_audit_accepted", False))
            and audit is not None
            and audit.verdict is AuditVerdict.MINOR
        ):
            extras["warning"] = AUDIT_MINOR_BACKLOG_TRIAGE
    return extras
