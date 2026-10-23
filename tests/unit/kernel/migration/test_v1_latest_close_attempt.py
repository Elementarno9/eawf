"""``latest_close_attempt`` picks the newest close-attempt generation of a v1 wave."""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.migration.v1_state import State, latest_close_attempt
from eawf.kernel.state.enums import AuditRequirement, CloseAttemptStatus
from eawf.kernel.state.models import (
    CloseAttempt,
    resolve_close_budget,
)

_T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


_WAVE = "P01-I01-W01"


_SHA = "a" * 40


_DIGEST = "b" * 64


def _attempt(
    *,
    attempt_id: str = "CA-01",
    wave_id: str = _WAVE,
    generation: int = 1,
    repair: int = 1,
    infrastructure_retry: int = 1,
) -> CloseAttempt:
    """Build a queued close attempt carrying the given remaining budgets."""
    return CloseAttempt(
        id=attempt_id,
        wave_id=wave_id,
        outcome="close submitted",
        tokens_consumed=None,
        generation=generation,
        supersedes_id=None,
        status=CloseAttemptStatus.QUEUED,
        integration_id="WI-01",
        candidate_sha=_SHA,
        integrated_sha=_SHA,
        tree_sha=_SHA,
        wave_revision_digest=_DIGEST,
        spec_digest=_DIGEST,
        criteria_digest=_DIGEST,
        gate_manifest_digest=_DIGEST,
        policy_digest=_DIGEST,
        runner_environment_digest=_DIGEST,
        dependency_binding_digest=_DIGEST,
        audit_requirement=AuditRequirement.NONE,
        no_runtime_waiver=False,
        repair_budget_remaining=repair,
        infrastructure_retry_budget_remaining=infrastructure_retry,
        requested_at=_T0,
        updated_at=_T0,
        idempotency_key=f"close:{wave_id}:{generation}",
    )


def _state(*attempts: CloseAttempt) -> State:
    """Build a minimal repo state carrying the given close attempts."""
    return State.model_validate(
        {
            "schema_version": "1.20",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": {
                "code": "QR",
                "slug": "quant-research",
                "title": "Quant Research",
                "domains": ["quant"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:QR",
            },
            "current": {"project_code": "QR"},
            "workspace": None,
            "close_attempts": {row.id: row.model_dump(mode="json") for row in attempts},
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_latest_close_attempt_empty_state_returns_none() -> None:
    """A state with no close attempt yields ``None`` (the honest-empty path)."""
    assert latest_close_attempt(_state(), _WAVE) is None


def test_latest_close_attempt_single_row_returns_it() -> None:
    """A single attempt is trivially the newest generation."""
    row = _attempt()
    assert latest_close_attempt(_state(row), _WAVE) == row


def test_latest_close_attempt_returns_the_newest_generation() -> None:
    """Generation order wins: a repair generation supersedes its parent."""
    first = _attempt(attempt_id="CA-01", generation=1, repair=1)
    second = _attempt(attempt_id="CA-02", generation=2, repair=0)
    latest = latest_close_attempt(_state(first, second), _WAVE)
    assert latest is not None
    assert latest.id == "CA-02"
    assert resolve_close_budget(attempt=latest).repair == 0


def test_latest_close_attempt_ignores_other_waves() -> None:
    """A sibling wave's attempt never leaks into this wave's budget."""
    other = _attempt(attempt_id="CA-09", wave_id="P01-I01-W02", generation=9, repair=0)
    assert latest_close_attempt(_state(other), _WAVE) is None


def test_latest_close_attempt_unknown_wave_returns_none() -> None:
    """An unknown wave id resolves to no attempt rather than raising."""
    assert latest_close_attempt(_state(_attempt()), "P99-I99-W99") is None
