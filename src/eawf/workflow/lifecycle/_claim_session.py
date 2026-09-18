"""Claim-session identity guards and the claim-time runtime baseline.

The baseline lives beside the guards because it is the same question asked
twice: which session is claiming, and what had that session already spent
when it did. Both read the :class:`~eawf.kernel.state.models.AgentSession`
row, and neither means anything without it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, WaveStatus
from eawf.kernel.state.models import AgentSession, RuntimeBaseline, State, Wave
from eawf.workflow.lifecycle._errors import ClaimSessionGuardCode, LifecycleGuardError

if TYPE_CHECKING:
    from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

logger = logging.getLogger(__name__)

#: The wave statuses whose work is still burning the session it claimed in.
_SHARING_WAVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)

#: Stable claim-session guard codes. Callers and negative probes key off these,
#: so the strings are API surface; message wording behind each code may change.
CLAIM_SESSION_NOT_FOUND: Final[ClaimSessionGuardCode] = "claim_session_not_found"
CLAIM_SESSION_NOT_ACTIVE: Final[ClaimSessionGuardCode] = "claim_session_not_active"
CLAIM_SESSION_SCOPE_MISMATCH: Final[ClaimSessionGuardCode] = "claim_session_scope_mismatch"
CLAIM_SESSION_ROLE_MISMATCH: Final[ClaimSessionGuardCode] = "claim_session_role_mismatch"


def _allowed_claim_scopes(wave: Wave, state: State) -> list[str]:
    """Return the scope ids a session may carry to claim *wave*.

    A claim session must be anchored at or above the wave it claims: the wave
    itself, its parent iter, its parent phase, or the project. Anything else --
    a sibling wave, an unrelated phase, a free-form string -- is out of scope.

    Args:
        wave: The wave under claim.
        state: State supplying the project code (when a project row exists).

    Returns:
        The allowed scope ids, most specific first.
    """
    scopes = [wave.id, wave.iter_id]
    parent_iter = state.iters.get(wave.iter_id)
    if parent_iter is not None:
        scopes.append(parent_iter.phase_id)
    if state.project is not None:
        scopes.append(state.project.code)
    return list(dict.fromkeys(scopes))


def _validate_existing_claims(state: State, session: AgentSession, wave: Wave) -> None:
    """Reject reuse when the session's prior wave bindings conflict.

    Iter-, phase-, and project-scoped sessions may claim several compatible
    waves. A stale/corrupt prior binding must not broaden that permission: every
    extant wave already indexed on the session must accept the same scope and
    role. Missing historical rows stay readable and do not block new work.

    Args:
        state: State supplying prior claimed-wave rows.
        session: Session proposed for reuse.
        wave: New wave the rejection anchors to.

    Raises:
        LifecycleGuardError: When a prior binding conflicts by scope or role.
    """
    # ``claimed_wave_ids`` was added after ``Wave.claim_session_id`` and
    # historical rows may therefore carry only the reverse binding. Treat both
    # directions as authority: an empty session index must never erase an
    # incompatible prior role/scope fact and make cross-role reuse appear safe.
    claimed_wave_ids = list(session.claimed_wave_ids)
    claimed_wave_ids.extend(
        prior_wave.id
        for prior_wave in state.waves.values()
        if prior_wave.claim_session_id == session.id and prior_wave.id not in claimed_wave_ids
    )
    for claimed_wave_id in claimed_wave_ids:
        claimed_wave = state.waves.get(claimed_wave_id)
        if claimed_wave is None:
            continue
        if session.scope_id not in _allowed_claim_scopes(claimed_wave, state):
            raise LifecycleGuardError(
                CLAIM_SESSION_SCOPE_MISMATCH,
                wave.id,
                f"cannot claim wave {wave.id!r}: session {session.id!r} has "
                f"incompatible prior binding {claimed_wave_id!r}",
            )
        required_role = claimed_wave.agent_role or AgentSessionRole.EXECUTOR
        if session.role not in {required_role, AgentSessionRole.OPERATOR}:
            raise LifecycleGuardError(
                CLAIM_SESSION_ROLE_MISMATCH,
                wave.id,
                f"cannot claim wave {wave.id!r}: session {session.id!r} has "
                f"incompatible prior role binding {claimed_wave_id!r}",
            )


def validate_claim_session(state: State, wave: Wave, session_id: str) -> AgentSession:
    """Return the live session that may claim *wave*, or reject with a guard code.

    Every guard runs before the first mutation, so a rejected claim leaves the
    state byte-identical. The session must exist, be ACTIVE, match the wave's
    role (or be OPERATOR), and carry wave/iter/phase/project scope. Compatible
    parent-scoped sessions may accumulate several claimed waves.

    Args:
        state: The state the session is read from.
        wave: The wave being claimed.
        session_id: The claiming session id.

    Returns:
        The validated :class:`~eawf.kernel.state.models.AgentSession`.

    Raises:
        LifecycleGuardError: With code ``claim_session_not_found`` /
            ``claim_session_not_active`` / ``claim_session_role_mismatch`` /
            ``claim_session_scope_mismatch``.
    """
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise LifecycleGuardError(
            CLAIM_SESSION_NOT_FOUND,
            wave.id,
            f"cannot claim wave {wave.id!r}: session {session_id!r} does not exist; "
            f"start one with `eawf session start --role <role> --scope {wave.id} "
            f"--runtime <runtime>`",
        )
    if session.status is not AgentSessionStatus.ACTIVE:
        raise LifecycleGuardError(
            CLAIM_SESSION_NOT_ACTIVE,
            wave.id,
            f"cannot claim wave {wave.id!r}: session {session_id!r} is "
            f"{session.status.value!r}, not active; start a fresh session",
        )
    required_role = wave.agent_role or AgentSessionRole.EXECUTOR
    if session.role is not required_role and session.role is not AgentSessionRole.OPERATOR:
        raise LifecycleGuardError(
            CLAIM_SESSION_ROLE_MISMATCH,
            wave.id,
            f"cannot claim wave {wave.id!r}: wave expects role "
            f"{required_role.value!r} but session {session_id!r} is "
            f"{session.role.value!r} (only an operator session may claim on behalf "
            f"of a specialised wave)",
        )
    allowed = _allowed_claim_scopes(wave, state)
    if session.scope_id not in allowed:
        raise LifecycleGuardError(
            CLAIM_SESSION_SCOPE_MISMATCH,
            wave.id,
            f"cannot claim wave {wave.id!r}: session {session_id!r} is scoped to "
            f"{session.scope_id!r}; allowed scopes are {allowed}",
        )
    _validate_existing_claims(state, session, wave)
    return session


def _claim_session_counters(runtime_session_id: str) -> RuntimeCounters | None:
    """Return a vendor runtime session's cumulative counters, when readable.

    The transcript is the primary source. The statusline runtime-counter
    sidecar stays a fallback for an operator whose statusline is
    ``eawf statusline`` but whose transcript does not resolve. Callers must
    supply a vendor runtime session id explicitly; an EAWF
    :class:`~eawf.kernel.state.models.AgentSession` id never enters this
    lookup.

    Args:
        runtime_session_id: Vendor session id used to resolve both the runtime
            transcript and its session-keyed statusline cache.

    Returns:
        The session's cumulative counters, or ``None`` when neither the
        transcript nor the sidecar yields any.
    """
    from eawf.runtime.runtime_counter_sidecar import (
        RuntimeCounterSidecar,
        sidecar_path_for_statusline_cache,
    )
    from eawf.runtime.runtimes.claude.statusline import cache_path_for
    from eawf.runtime.runtimes.claude.transcript_counters import (
        aggregate_transcript_counters,
        transcript_path_for_session,
    )

    transcript = transcript_path_for_session(runtime_session_id, cwd=Path.cwd())
    counters = aggregate_transcript_counters(transcript)
    if counters is not None:
        return counters
    sidecar = RuntimeCounterSidecar(
        sidecar_path_for_statusline_cache(cache_path_for(runtime_session_id))
    )
    return sidecar.read()


def count_runtime_session_sharers(state: State, *, runtime_session_id: str) -> int:
    """Return how many waves are burning *runtime_session_id* right now.

    Runtime counters are cumulative per vendor session, so every wave
    claimed into one session differences the same numbers. Counting the
    sharers at claim time is what lets the close-time delta hand each wave
    a share instead of handing every one of them the whole session.

    Args:
        state: The state the wave and session rows are read from.
        runtime_session_id: The vendor session the claim is anchored to.

    Returns:
        How many CLAIMED or IN_PROGRESS waves resolve to a session
        disclosing this runtime session id. The wave whose claim is being
        stamped is counted among them, so a sole claimant answers one.
    """
    sharing = {
        session.id
        for session in state.agent_sessions.values()
        if session.runtime_session_id == runtime_session_id
    }
    return sum(
        1
        for wave in state.waves.values()
        if wave.status in _SHARING_WAVE_STATUSES and wave.claim_session_id in sharing
    )


def capture_claim_baseline(state: State, session: AgentSession) -> RuntimeBaseline | None:
    """Return the claim-time snapshot of *session*, or ``None`` when it has none.

    An :class:`~eawf.kernel.state.models.AgentSession` id is an EAWF id, not
    a vendor one, so the lookup is anchored on the session's disclosed
    ``runtime_session_id`` and nothing else: resolving a transcript by the
    EAWF id read a foreign namespace and stamped whatever happened to
    collide as this wave's origin.

    Args:
        state: The state the concurrent-wave count is read from.
        session: The validated claiming session, already bound to the wave
            so the wave being claimed is counted among the sharers.

    Returns:
        The baseline, stamped with the sharer count the claim saw, or
        ``None`` when the session discloses no runtime session or that
        session exposes no counters at all -- so the close-time delta
        degrades to "no captured runtime" rather than subtracting against a
        phantom zero baseline.
    """
    runtime_session_id = session.runtime_session_id
    if runtime_session_id is None:
        return None
    counters = _claim_session_counters(runtime_session_id)
    if counters is None:
        return None
    # The count is of BOUND waves, and a capture taken before the binding
    # lands sees none of them. A divisor of zero is not a thing the delta can
    # apply, and the session being captured for is itself in flight, so one is
    # the floor rather than a rounding-up of nothing.
    shared = max(1, count_runtime_session_sharers(state, runtime_session_id=runtime_session_id))
    logger.info(
        f"capture_claim_baseline session={session.id!r} shared_wave_count={shared} "
        f"measure_version={counters.measure_version}"
    )
    return RuntimeBaseline(
        api_duration_ms=counters.api_duration_ms,
        total_duration_ms=counters.total_duration_ms,
        cost_usd=float(counters.cost_usd) if counters.cost_usd is not None else None,
        input_tokens=counters.input_tokens,
        output_tokens=counters.output_tokens,
        cache_creation_input_tokens=counters.cache_creation_input_tokens,
        cache_read_input_tokens=counters.cache_read_input_tokens,
        harness=counters.harness,
        model=counters.model,
        session_id=runtime_session_id,
        measure_version=counters.measure_version,
        shared_wave_count=shared,
        captured_at=datetime.now(UTC),
    )


__all__ = [
    "CLAIM_SESSION_NOT_ACTIVE",
    "CLAIM_SESSION_NOT_FOUND",
    "CLAIM_SESSION_ROLE_MISMATCH",
    "CLAIM_SESSION_SCOPE_MISMATCH",
    "capture_claim_baseline",
    "count_runtime_session_sharers",
    "validate_claim_session",
]
