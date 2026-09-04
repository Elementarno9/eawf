"""``runtime.codex_lifecycle`` -- provider-native Codex session correlation.

Codex discloses its own session and subagent lifecycle rather than eawf's,
so the daemon correlates each provider event onto the AgentSession it
already owns and onto that session's active wave. This module owns the
binding rules, the per-event apply, and the RPC that persists the result.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    MeasurementQuality,
    MeasurementStatus,
    StoreKind,
)
from eawf.kernel.state.models import (
    AgentSession,
    SessionAttempt,
    State,
    Wave,
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
from eawf.runtime.daemon.methods.state_models import CodexLifecycleParams, CodexLifecycleResult

logger = logging.getLogger(__name__)


def _build_codex_lifecycle_event_envelope(
    *,
    args: CodexLifecycleParams,
    result: CodexLifecycleResult,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the event row emitted for Codex lifecycle correlation."""
    now = datetime.now(UTC)
    safe_params = args.model_dump(
        mode="json",
        exclude={"repo_root", "agent_transcript_path"},
    )
    args_raw = orjson.dumps(safe_params, option=orjson.OPT_SORT_KEYS)
    scope_id = result.wave_id or result.agent_session_id or ""
    extras: dict[str, str | int | float | bool] = {
        "provider_event": args.event_type,
        "correlated": result.correlated,
    }
    if result.reason is not None:
        extras["reason"] = result.reason
    if result.wave_id is not None:
        extras["wave"] = result.wave_id
    if result.attempt is not None:
        extras["attempt"] = result.attempt
    payload = EventPayload(
        timestamp=now,
        event_type="runtime.codex_lifecycle",
        event_kind=None,
        actor="daemon",
        command="runtime.codex_lifecycle",
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok" if result.correlated else "warn",
        message=(f"runtime.codex_lifecycle event={args.event_type} correlated={result.correlated}"),
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=(f"runtime.codex_lifecycle event={args.event_type} correlated={result.correlated}"),
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _active_codex_sessions(state: State) -> list[AgentSession]:
    return [
        session
        for session in state.agent_sessions.values()
        if session.runtime == "codex" and session.status == "active"
    ]


def _bind_codex_provider_session(
    state: State,
    provider_session_id: str,
) -> tuple[AgentSession | None, str | None]:
    sessions = _active_codex_sessions(state)
    bound = [session for session in sessions if session.runtime_session_id == provider_session_id]
    if len(bound) == 1:
        return bound[0], None
    if len(bound) > 1:
        return None, "provider_session_binding_ambiguous"
    unbound = [session for session in sessions if session.runtime_session_id is None]
    if len(unbound) != 1:
        reason = (
            "provider_session_target_missing"
            if not unbound
            else "provider_session_target_ambiguous"
        )
        return None, reason
    unbound[0].runtime_session_id = provider_session_id
    return unbound[0], None


def _bound_codex_session(
    state: State,
    provider_session_id: str,
) -> tuple[AgentSession | None, str | None]:
    bound = [
        session
        for session in _active_codex_sessions(state)
        if session.runtime_session_id == provider_session_id
    ]
    if len(bound) == 1:
        return bound[0], None
    reason = (
        "provider_session_binding_missing" if not bound else "provider_session_binding_ambiguous"
    )
    return None, reason


def _codex_session_wave(
    state: State,
    session: AgentSession,
) -> tuple[Wave | None, str | None]:
    active_wave_ids = set(state.current.active_wave_ids)
    candidates = {wave_id for wave_id in session.claimed_wave_ids if wave_id in active_wave_ids}
    if session.scope_id in active_wave_ids:
        candidates.add(session.scope_id)
    if len(candidates) != 1:
        reason = "wave_correlation_missing" if not candidates else "wave_correlation_ambiguous"
        return None, reason
    wave_id = next(iter(candidates))
    wave = state.waves.get(wave_id)
    if wave is None:
        return None, "wave_record_missing"
    return wave, None


def _apply_codex_lifecycle(
    state: State,
    args: CodexLifecycleParams,
) -> CodexLifecycleResult:
    before_version = ""
    after_version = ""
    if args.event_type == "session_start":
        session, reason = _bind_codex_provider_session(state, args.provider_session_id)
        return CodexLifecycleResult(
            correlated=session is not None,
            reason=reason,
            agent_session_id=session.id if session is not None else None,
            before_version=before_version,
            after_version=after_version,
        )

    session, reason = _bound_codex_session(state, args.provider_session_id)
    if session is None:
        return CodexLifecycleResult(
            correlated=False,
            reason=reason,
            before_version=before_version,
            after_version=after_version,
        )
    if args.event_type == "session_end":
        wave, reason = _codex_session_wave(state, session)
        return CodexLifecycleResult(
            correlated=wave is not None,
            reason=reason,
            agent_session_id=session.id,
            wave_id=wave.id if wave is not None else None,
            before_version=before_version,
            after_version=after_version,
        )

    if args.agent_id is None:
        return CodexLifecycleResult(
            correlated=False,
            reason="agent_id_missing",
            agent_session_id=session.id,
            before_version=before_version,
            after_version=after_version,
        )
    wave, reason = _codex_session_wave(state, session)
    if wave is None:
        return CodexLifecycleResult(
            correlated=False,
            reason=reason,
            agent_session_id=session.id,
            before_version=before_version,
            after_version=after_version,
        )

    existing_no = next(
        (
            attempt_no
            for attempt_no, attempt in wave.sessions.items()
            if attempt.runtime == "codex" and attempt.session_id == args.agent_id
        ),
        None,
    )
    if args.event_type == "subagent_start":
        if existing_no is None:
            attempt_no = (max(wave.sessions) if wave.sessions else 0) + 1
            wave.sessions[attempt_no] = SessionAttempt(
                attempt=attempt_no,
                runtime="codex",
                session_id=args.agent_id,
                session_log_handle=(f"urn:eawf:v1:session-log:codex:{uuid.uuid4().hex}"),
                started_at=args.occurred_at,
                measurement_quality=MeasurementQuality.UNAVAILABLE,
                measurement_status=MeasurementStatus.USAGE_UNAVAILABLE,
                measurement_reason="awaiting_subagent_stop",
            )
        else:
            attempt_no = existing_no
        return CodexLifecycleResult(
            correlated=True,
            agent_session_id=session.id,
            wave_id=wave.id,
            attempt=attempt_no,
            before_version=before_version,
            after_version=after_version,
        )

    if existing_no is None:
        return CodexLifecycleResult(
            correlated=False,
            reason="subagent_start_missing",
            agent_session_id=session.id,
            wave_id=wave.id,
            before_version=before_version,
            after_version=after_version,
        )
    attempt = wave.sessions[existing_no]
    counters = args.counters
    session_log_handle = attempt.session_log_handle
    if args.agent_transcript_path is not None:
        from eawf.runtime.daemon.session import register_session_log

        session_log_handle = register_session_log(
            "codex",
            Path(args.agent_transcript_path),
            wave_id=wave.id,
        )
    wave.sessions[existing_no] = attempt.model_copy(
        update={
            "session_log_handle": session_log_handle,
            "ended_at": args.occurred_at,
            "cache_creation_input_tokens": (
                counters.cache_creation_input_tokens if counters is not None else None
            ),
            "cache_read_input_tokens": (
                counters.cache_read_input_tokens if counters is not None else None
            ),
            "input_tokens": counters.input_tokens if counters is not None else None,
            "output_tokens": counters.output_tokens if counters is not None else None,
            "cost_usd": (
                float(counters.cost_usd)
                if counters is not None and counters.cost_usd is not None
                else None
            ),
            "measurement_quality": args.measurement_quality,
            "measurement_status": args.measurement_status,
            "measurement_reason": args.measurement_reason,
        }
    )
    return CodexLifecycleResult(
        correlated=True,
        agent_session_id=session.id,
        wave_id=wave.id,
        attempt=existing_no,
        before_version=before_version,
        after_version=after_version,
    )


@register("runtime.codex_lifecycle")
async def codex_lifecycle(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Correlate provider-native Codex lifecycle events under daemon ownership."""
    try:
        args = CodexLifecycleParams.model_validate(params)
    except ValidationError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc

    state_path, event_path, wal_path = resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )
    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, payload = read_state(state_path)
            before_version = state_version(payload)
            result = _apply_codex_lifecycle(state, args)
            if result.correlated:
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
            result = result.model_copy(
                update={
                    "before_version": before_version,
                    "after_version": after_version,
                }
            )
            envelope = _build_codex_lifecycle_event_envelope(
                args=args,
                result=result,
                before_version=before_version,
                after_version=after_version,
            )
            result = result.model_copy(update={"event": envelope.model_dump(mode="json")})
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
                f"codex_lifecycle event={args.event_type} "
                f"correlated={result.correlated} wave={result.wave_id!r}"
            )
            return result.model_dump(mode="json")
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)
