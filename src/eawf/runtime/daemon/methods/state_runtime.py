"""``runtime.capture`` -- cumulative runtime counters onto one active wave.

Runtime counters are cumulative per SESSION, not per wave, so a capture
has to be correlated to exactly one wave, rebased when the capturing
session changes, re-originated when the counter source resets, and merged
without null-clobbering fields a prior snapshot populated. This module
owns those rules and the RPC that persists their result.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    StoreKind,
)
from eawf.kernel.state.models import (
    RuntimeBaseline,
    RuntimeCarry,
    RuntimeLatest,
    SessionAttempt,
    State,
    Wave,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.validate.strict import validate_state
from eawf.observability.telemetry.join import (
    DEFAULT_EU_MINUTES,
)
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    register,
)
from eawf.runtime.daemon.wal import WalRecord
from eawf.workflow.lifecycle.wave import compute_runtime_delta

if TYPE_CHECKING:
    pass
from eawf.runtime.daemon.methods.state_context import (
    bus_for_root,
    read_state,
    resolve_mutator_paths,
    state_version,
)
from eawf.runtime.daemon.methods.state_models import RuntimeCaptureParams, RuntimeCaptureResult

logger = logging.getLogger(__name__)


def _build_runtime_capture_event_envelope(
    *,
    active_wave_ids: list[str],
    params: dict[str, Any],
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the event row emitted after a runtime.capture write."""
    now = datetime.now(UTC)
    scope_id = ",".join(active_wave_ids)
    args_raw = orjson.dumps(params, option=orjson.OPT_SORT_KEYS)
    extras: dict[str, str | int | float | bool] = {
        "active_count": len(active_wave_ids),
        "active_wave_ids": scope_id,
    }
    session_id = params.get("session_id")
    if isinstance(session_id, str) and session_id:
        extras["session_id"] = session_id
    payload = EventPayload(
        timestamp=now,
        event_type="runtime.capture",
        event_kind=None,
        actor="daemon",
        command="runtime.capture",
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=f"runtime.capture active_count={len(active_wave_ids)}",
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=f"runtime.capture active_count={len(active_wave_ids)}",
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _runtime_latest_from_params(
    params: RuntimeCaptureParams,
    *,
    shared_wave_count: int,
) -> RuntimeLatest:
    """Convert capture params into the state-model runtime snapshot.

    Threads the parser-stamped ``harness`` + ``model`` attribution off the
    capture params (W19 added them to :class:`RuntimeCounters`, which
    :class:`RuntimeCaptureParams` extends) onto the persisted
    :class:`RuntimeLatest` so a recorded actual derived from this snapshot
    carries non-null attribution and becomes calibratable by harness+model.
    Both stay nullable -- a payload with no recognised model still persists.

    ``shared_wave_count`` is the daemon's own count of the waves this one capture
    is about to be written to. The counters are the SESSION's, not any one wave's,
    so the count is what lets the close-time delta hand each wave a share instead
    of handing every wave the whole session.

    Args:
        params: The validated capture payload.
        shared_wave_count: How many active waves this capture is written to.
    """
    captured_at = params.captured_at or datetime.now(UTC)
    cost_usd = float(params.cost_usd) if params.cost_usd is not None else None
    return RuntimeLatest(
        api_duration_ms=params.api_duration_ms,
        total_duration_ms=params.total_duration_ms,
        cost_usd=cost_usd,
        input_tokens=params.input_tokens,
        output_tokens=params.output_tokens,
        cache_creation_input_tokens=params.cache_creation_input_tokens,
        cache_read_input_tokens=params.cache_read_input_tokens,
        harness=params.harness,
        model=params.model,
        session_id=params.session_id,
        measure_version=params.measure_version,
        shared_wave_count=shared_wave_count,
        captured_at=captured_at,
    )


def _resolve_runtime_capture_wave_ids(  # noqa: C901
    state: State,
    params: RuntimeCaptureParams,
) -> list[str]:
    """Resolve one exact wave for a runtime capture.

    Runtime counters are session-scoped. Copying one snapshot onto every active
    wave fabricates attribution, so ambiguous correlation is rejected. Callers
    may name the wave directly; Codex session-end captures may instead resolve
    through the daemon-owned :class:`AgentSession` provider-session binding.
    The sole-active-wave fallback preserves the unambiguous legacy path.
    """
    active_wave_ids = list(state.current.active_wave_ids)
    if not active_wave_ids:
        raise DaemonValidationError("validation_failed: runtime.capture requires active waves")

    if params.wave_id is not None:
        if params.wave_id not in active_wave_ids:
            raise DaemonValidationError(
                f"validation_failed: runtime.capture wave is not active: {params.wave_id!r}"
            )
        return [params.wave_id]

    if params.harness == "codex" and params.session_id is not None:
        candidates: set[str] = set()
        for session in state.agent_sessions.values():
            if (
                session.runtime != "codex"
                or session.runtime_session_id != params.session_id
                or session.status != "active"
            ):
                continue
            candidates.update(
                wave_id for wave_id in session.claimed_wave_ids if wave_id in active_wave_ids
            )
            if session.scope_id in active_wave_ids:
                candidates.add(session.scope_id)
        if len(candidates) == 1:
            return sorted(candidates)
        if candidates:
            raise DaemonValidationError(
                "validation_failed: runtime.capture correlation ambiguous: "
                f"candidate_count={len(candidates)}"
            )

    if len(active_wave_ids) == 1:
        return active_wave_ids
    raise DaemonValidationError(
        "validation_failed: runtime.capture correlation ambiguous: "
        f"active_count={len(active_wave_ids)}"
    )


#: Every counter a runtime snapshot carries and a carry accumulates.
_RUNTIME_COUNTER_FIELDS: Final[tuple[str, ...]] = (
    "api_duration_ms",
    "total_duration_ms",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _fold_finished_session(
    carry: RuntimeCarry | None,
    baseline: RuntimeBaseline,
    latest: RuntimeLatest | None,
) -> RuntimeCarry:
    """Return *carry* with the finished session's total (``latest - baseline``) added.

    Counter deltas are clamped at zero: a session whose snapshots regressed (a
    reset counter source) contributes nothing rather than a negative total.

    The folded total is the wave's SHARE of that session -- divided by however many
    waves were active when its counters were captured
    (:func:`~eawf.workflow.lifecycle.wave.shared_wave_divisor`). Dividing here, at
    the point the session's total is finalised, is what lets the close-time delta
    add the carry verbatim: each session is split by ITS OWN concurrency rather
    than by whatever concurrency the wave happens to end under.
    """
    from eawf.workflow.lifecycle.wave import shared_wave_divisor

    base = carry or RuntimeCarry()
    if latest is None:
        # The session ended without ever capturing, so there is nothing measured
        # to fold. Counting it as "folded" would claim a session's runtime was
        # accounted for when in truth it was never seen -- but dropping it in
        # silence is how a dead capture path passes for an idle one. Say what is
        # being lost, so the missing runtime has a recorded reason like every
        # other way this wave can under-report.
        logger.warning(
            f"fold_finished_session session={baseline.session_id!r} status='never-captured'; "
            "the session ended with nothing captured against its baseline -- "
            "whatever runtime it spent on this wave is dropped, not carried"
        )
        return base
    divisor = shared_wave_divisor(baseline, latest)
    folded: dict[str, float | int] = {}
    for field in _RUNTIME_COUNTER_FIELDS:
        latest_value = getattr(latest, field)
        if latest_value is None:
            continue
        baseline_value = getattr(baseline, field) or 0
        session_total = max(0, latest_value - baseline_value) / divisor
        existing = getattr(base, field)
        folded[field] = (
            existing + session_total
            if isinstance(existing, float)
            else existing + int(session_total)
        )
    folded["sessions_folded"] = base.sessions_folded + 1
    return base.model_copy(update=folded)


def counters_incomparable(baseline: RuntimeBaseline, incoming: RuntimeLatest) -> bool:
    """Return whether *incoming* cannot be differenced against *baseline*.

    Two ways that happens, and the second is the one that bites quietly:

    * **The counters went backwards.** Cumulative counters only grow, so a drop
      means the source changed under the wave -- a truncated transcript, a reset.
    * **The measure changed.** When the definition of the counter changes, the
      difference between two snapshots is not work, it is the redefinition. This
      is NOT detectable from the direction the number moved: a redefinition that
      lowers the figure looks like a regression and gets caught, but one that
      RAISES it looks exactly like a productive week and gets banked as runtime.
      Both happened inside P30-I25 -- the first redefinition stranded two claimed
      waves, the very next inflated three of them by thirteen hours apiece -- which
      is why the snapshots carry ``measure_version`` and this check reads it rather
      than inferring from the numbers.
    """
    base_version = baseline.measure_version
    new_version = incoming.measure_version
    if base_version != new_version and not (base_version is None and new_version is None):
        # An UNVERSIONED baseline is not a matching one -- it was produced by some
        # earlier definition of the counters, and which one is exactly the fact
        # nobody recorded. Treating unknown as "same" is what let the gap-heuristic
        # baselines survive the turn-span change and bank 13 hours apiece. Two
        # unversioned snapshots (the statusline path, which declares no measure at
        # all) still fall through to the direction check below.
        return True
    for field in _RUNTIME_COUNTER_FIELDS:
        base_value = getattr(baseline, field)
        new_value = getattr(incoming, field)
        if base_value is not None and new_value is not None and new_value < base_value:
            return True
    return False


def reorigin_on_reset(wave: Wave, incoming: RuntimeLatest) -> None:
    """Re-origin the baseline on incomparable counters so the wave stays measurable.

    The alternative is what the close path used to do: raise on the backwards
    counter, which strands the wave FOREVER -- no retry can help, because the
    baseline is on disk and every future capture compares against it. The runtime
    the old basis measured cannot be recovered, so it is dropped (loudly); what
    matters is that the wave stays closable and keeps measuring forward.
    """
    baseline = wave.runtime_baseline
    if baseline is None:
        return
    logger.warning(
        f"reorigin_on_counter_reset wave={wave.id} session={incoming.session_id!r} "
        f"baseline_api_duration_ms={baseline.api_duration_ms!r} "
        f"incoming_api_duration_ms={incoming.api_duration_ms!r}; "
        "counters regressed (source reset or basis change) -- re-originating"
    )
    wave.runtime_baseline = baseline.model_copy(
        update={field: getattr(incoming, field) or 0 for field in _RUNTIME_COUNTER_FIELDS}
        | {
            "captured_at": datetime.now(UTC),
            "measure_version": incoming.measure_version,
            "shared_wave_count": incoming.shared_wave_count,
        }
    )
    wave.runtime_latest = None
    # Record WHY this wave's runtime is short. The measurement taken before the
    # reset cannot be re-derived, so the wave may close with less runtime than it
    # really spent -- or with none. Without the count, that close is
    # indistinguishable from a capture path that silently did nothing, and the
    # zero-runtime gate must then either refuse every reset or trust every zero.
    carry = wave.runtime_carry or RuntimeCarry()
    wave.runtime_carry = carry.model_copy(update={"counter_resets": carry.counter_resets + 1})


def rebase_for_session(wave: Wave, incoming: RuntimeLatest, session_id: str | None) -> None:
    """Rebase the wave's runtime snapshots onto *session_id*'s counter origin.

    Runtime counters are cumulative *within* a session: session B's transcript
    starts from zero regardless of what session A already spent on the wave.
    Differencing B's counters against A's baseline is therefore meaningless --
    the delta goes backwards, and the close path clamps a backwards counter to
    zero, so the wave would close reporting no runtime at all. So on the first
    capture from a session other than the baseline's, the finished session's
    total is folded into ``wave.runtime_carry`` and the baseline is re-originated
    on the new session -- the close-time delta then sums every session's runtime.

    **The new origin is the capturing session's counters right now, not zero.**
    A zero origin is wrong in two ways, and both bite:

    * *Returning to a session double-counts it.* Sessions interleave (A -> B ->
      A). On the return to A, a zero origin makes the next delta A's ENTIRE
      cumulative -- including the work already folded into the carry when A was
      first left. The wave is then charged twice for it, without bound, once per
      alternation.
    * *It absorbs work the wave did not do.* Session B's counters cover
      everything the operator did in B, so a zero origin charges the wave for any
      unrelated work B did before the wave was resumed.

    Originating on the incoming counters costs at most the turn that just ended
    (its work lands before the first capture in the new session establishes the
    origin). That is a bounded under-count of one turn, against an unbounded
    over-count -- the safer error, and the honest one.

    A capture with no session id, or one matching the baseline's session, leaves
    the snapshots alone. A baseline predating the session stamp (schema < 1.15)
    adopts the capturing session when nothing has been captured against it yet.
    """
    baseline = wave.runtime_baseline
    if baseline is None or session_id is None:
        return
    if baseline.session_id == session_id:
        return
    if baseline.session_id is None and wave.runtime_latest is None:
        # A baseline predating the session stamp with nothing captured against it
        # yet: adopt the capturing session rather than treating the wave as
        # multi-session and folding a zero total.
        wave.runtime_baseline = baseline.model_copy(update={"session_id": session_id})
        return

    wave.runtime_carry = _fold_finished_session(
        wave.runtime_carry, baseline=baseline, latest=wave.runtime_latest
    )
    wave.runtime_baseline = RuntimeBaseline(
        api_duration_ms=incoming.api_duration_ms or 0,
        total_duration_ms=incoming.total_duration_ms or 0,
        cost_usd=incoming.cost_usd or 0.0,
        input_tokens=incoming.input_tokens or 0,
        output_tokens=incoming.output_tokens or 0,
        cache_creation_input_tokens=incoming.cache_creation_input_tokens or 0,
        cache_read_input_tokens=incoming.cache_read_input_tokens or 0,
        harness=incoming.harness or baseline.harness,
        model=incoming.model or baseline.model,
        session_id=session_id,
        measure_version=incoming.measure_version,
        shared_wave_count=incoming.shared_wave_count,
        captured_at=datetime.now(UTC),
    )
    wave.runtime_latest = None
    logger.info(
        f"rebase_runtime_counters wave={wave.id} session={session_id!r} "
        f"sessions_folded={wave.runtime_carry.sessions_folded}"
    )


#: Per-class token fields a runtime.capture merge must never null-clobber.
_RUNTIME_TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def merge_runtime_latest(existing: RuntimeLatest | None, incoming: RuntimeLatest) -> RuntimeLatest:
    """Merge a fresh capture over the existing snapshot without null-clobbering tokens.

    A ``runtime.capture`` payload can carry a priced cost + duration while
    omitting the per-class token counts (the ``context_window.current_usage``
    block is absent for some payloads). A blind overwrite would wipe token
    fields a prior headless snapshot populated, collapsing the close-time
    runtime-delta token tally to zero. Merge so a ``None`` incoming token field
    preserves the existing populated value; every other field takes the fresh
    capture's value.

    ``shared_wave_count`` takes the LARGEST concurrency either snapshot saw, not
    the freshest. A wave that shared its session with three others and then ran on
    alone still accrued that shared runtime, so letting the final solo capture
    (count 1) overwrite the count would hand it the whole session back.

    Args:
        existing: The wave's current ``runtime_latest`` snapshot, or ``None``.
        incoming: The freshly-parsed capture snapshot to fold in.

    Returns:
        ``incoming`` unchanged when there is no existing snapshot; otherwise a
        copy of ``incoming`` whose per-class token fields fall back to the
        existing value wherever ``incoming`` left them ``None``, and whose
        shared-wave count is the max of the two.
    """
    if existing is None:
        return incoming
    updates: dict[str, Any] = {
        field: getattr(existing, field)
        for field in _RUNTIME_TOKEN_FIELDS
        if getattr(incoming, field) is None and getattr(existing, field) is not None
    }
    shared_counts = [
        count
        for count in (existing.shared_wave_count, incoming.shared_wave_count)
        if count is not None
    ]
    if shared_counts and max(shared_counts) != incoming.shared_wave_count:
        updates["shared_wave_count"] = max(shared_counts)
    if not updates:
        return incoming
    return incoming.model_copy(update=updates)


def upsert_interactive_session_attempt(
    wave: Wave,
    *,
    latest: RuntimeLatest,
    session_id: str | None,
) -> None:
    """Mint (or update) the interactive-Claude ``SessionAttempt`` from a capture.

    The interactive-Claude lifecycle (claude CLI claim/close + the Stop hook)
    fires ``runtime.capture``, which stamps ``wave.runtime_latest`` -- but unlike
    a headless spawn (which stamps :attr:`SessionAttempt.cost_usd` in
    :func:`~eawf.runtime.daemon.methods.agent._persist_live_session_attempt`) it
    minted NO attempt, so an interactive wave carried cost only on the wave-level
    snapshot and never surfaced a per-attempt cost row like a headless wave does.
    This upsert records that attempt, restoring per-attempt-cost parity across
    the headless/interactive axis.

    The attempt carries the wave's **delta**, not the capture snapshot. The
    snapshot is cumulative for the whole session, so stamping it verbatim charged
    every active wave with the entire session's cost and token volume, and left
    the attempt with ``started_at == ended_at`` -- a zero-length span, which the
    wave-detail metrics tab (which derives EU from attempt spans) renders as
    ``0.00 EU`` even though the recorded actual carries real EU. The attempt
    therefore spans claim (the baseline capture) to this capture, and its cost and
    per-class tokens come from :func:`compute_runtime_delta`.

    Idempotency mirrors the headless attempt-counter handling: a repeated
    Stop-hook capture for the SAME interactive session UPDATES the existing
    attempt in place (preserving its ``attempt`` number + ``started_at``) rather
    than appending a duplicate. The dedup key is the capture ``session_id``,
    synthesised per-wave when the hook omits it so a session-less capture still
    dedupes onto a single attempt. A capture carrying no priced cost, or one with
    no baseline to difference against, is a no-op: there is nothing wave-scoped to
    surface, so the wave-level snapshot stays the only record.

    Args:
        wave: The active wave whose ``runtime_latest`` this capture stamped.
        latest: The runtime snapshot the same capture produced; the wave's delta
            against it feeds the attempt.
        session_id: The interactive Claude Code session id off the capture,
            or ``None`` when the Stop hook omitted it.
    """
    if latest.cost_usd is None:
        return
    # The snapshot is CUMULATIVE for the whole session, so stamping it verbatim
    # put the entire session's spend on every wave and left the attempt with a
    # zero-length span (started_at == ended_at), which the wave-detail metrics tab
    # renders as 0.00 EU. The wave's own delta is what belongs on its attempt row.
    delta = compute_runtime_delta(
        wave.runtime_baseline,
        latest,
        carry=wave.runtime_carry,
        eu_minutes=DEFAULT_EU_MINUTES,
    )
    if delta is None:
        return
    handle_id = session_id or f"interactive:{wave.id}"
    runtime = latest.harness or "claude-code"
    existing_no = next(
        (no for no, sess in wave.sessions.items() if sess.session_id == handle_id),
        None,
    )
    if existing_no is not None:
        attempt_no = existing_no
        started_at = wave.sessions[existing_no].started_at
        outcome = "update"
    else:
        attempt_no = (max(wave.sessions) if wave.sessions else 0) + 1
        # The attempt starts when the wave was baselined (its claim), not when the
        # capture fired, so the span is the wave's working window.
        started_at = (
            wave.runtime_baseline.captured_at
            if wave.runtime_baseline is not None
            else latest.captured_at
        )
        outcome = "mint"
    wave.sessions[attempt_no] = SessionAttempt(
        attempt=attempt_no,
        runtime=runtime,
        session_id=handle_id,
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:{handle_id}",
        started_at=started_at,
        ended_at=latest.captured_at,
        exit_status=0,
        input_tokens=delta.input_tokens,
        output_tokens=delta.output_tokens,
        cache_creation_input_tokens=delta.cache_creation_input_tokens,
        cache_read_input_tokens=delta.cache_read_input_tokens,
        cost_usd=delta.actual_cost_usd,
    )
    logger.info(
        f"upsert_interactive_session_attempt wave={wave.id} attempt={attempt_no} "
        f"session={handle_id!r} cost_usd={delta.actual_cost_usd} "
        f"tokens={delta.actual_tokens} outcome={outcome}"
    )


@register("runtime.capture")
async def runtime_capture(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Persist latest runtime counters onto one exactly correlated active wave.

    Args:
        ctx: Server context; state, event, and WAL paths are resolved the same
            way as ``state.mutate``.
        params: Strict :class:`RuntimeCaptureParams` payload.

    Returns:
        Dict matching :class:`RuntimeCaptureResult`.

    Raises:
        DaemonValidationError: When params fail validation, no active waves are
            registered, an active wave id is missing, or post-write state
            validation rejects the candidate payload.
    """
    try:
        args = RuntimeCaptureParams.model_validate(params)
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
            active_wave_ids = _resolve_runtime_capture_wave_ids(state, args)

            latest = _runtime_latest_from_params(args, shared_wave_count=len(active_wave_ids))
            for wave_id in active_wave_ids:
                wave = state.waves.get(wave_id)
                if wave is None:
                    raise DaemonValidationError(
                        f"validation_failed: active wave missing: {wave_id!r}"
                    )
                # A capture from a session other than the baseline's measures a
                # fresh counter origin, so rebase (folding the finished session's
                # total into runtime_carry, and re-originating on THIS session's
                # counters) before merging this session's snapshot in.
                rebase_for_session(wave, incoming=latest, session_id=args.session_id)
                # Same session, but the snapshot is not comparable to the baseline:
                # the counters went backwards, or the measure itself changed. Either
                # way the difference is not work, so re-origin rather than record it.
                if wave.runtime_baseline is not None and counters_incomparable(
                    wave.runtime_baseline, latest
                ):
                    reorigin_on_reset(wave, latest)
                wave.runtime_latest = merge_runtime_latest(wave.runtime_latest, latest)
                # The interactive-Claude lifecycle mints no SessionAttempt on
                # its own (only the headless spawn does); record one here off the
                # same priced capture so an interactive wave surfaces per-attempt
                # cost the way a headless wave does. Idempotent per session id.
                upsert_interactive_session_attempt(wave, latest=latest, session_id=args.session_id)
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
            event_params = args.model_dump(mode="json", exclude={"repo_root"})
            envelope = _build_runtime_capture_event_envelope(
                active_wave_ids=active_wave_ids,
                params=event_params,
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
                f"runtime_capture active_count={len(active_wave_ids)} "
                f"before={before_version} after={after_version} envelope_id={envelope.id!r}"
            )
            return RuntimeCaptureResult(
                active_wave_ids=active_wave_ids,
                active_count=len(active_wave_ids),
                before_version=before_version,
                after_version=after_version,
                event=envelope.model_dump(mode="json"),
            ).model_dump(mode="json")
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)
