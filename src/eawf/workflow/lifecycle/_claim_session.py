"""Claim-session identity guards shared by wave lifecycle transitions."""

from __future__ import annotations

from typing import Final

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
from eawf.kernel.state.models import AgentSession, State, Wave
from eawf.workflow.lifecycle._errors import ClaimSessionGuardCode, LifecycleGuardError

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


__all__ = [
    "CLAIM_SESSION_NOT_ACTIVE",
    "CLAIM_SESSION_NOT_FOUND",
    "CLAIM_SESSION_ROLE_MISMATCH",
    "CLAIM_SESSION_SCOPE_MISMATCH",
    "validate_claim_session",
]
