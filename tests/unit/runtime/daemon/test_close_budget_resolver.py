"""Tests: the single close-budget resolver the daemon, CLI and TUI all read (REL-004).

Boundary + error-path coverage for the public surface introduced to replace the
per-consumer budget literals:
:func:`~eawf.kernel.state.models.resolve_close_budget`,
:class:`~eawf.kernel.state.models.CloseBudget` and
:func:`~eawf.kernel.state.models.latest_close_attempt`. The wiring assertions
(daemon seeding, receipt emission, TUI display) live in
``tests/integration/runtime/daemon/test_durable_close.py`` and
``tests/tui/test_autopilot_repair_budget.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AuditRequirement, CloseAttemptStatus
from eawf.kernel.state.models import (
    CLOSE_BUDGET_AXES,
    CLOSE_BUDGET_RECEIPT_PREFIX,
    DEFAULT_CLOSE_INFRASTRUCTURE_RETRY_BUDGET,
    DEFAULT_CLOSE_REPAIR_BUDGET,
    CloseAttempt,
    CloseBudget,
    State,
    latest_close_attempt,
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


# --------------------------------------------------------------------------
# resolve_close_budget
# --------------------------------------------------------------------------


def test_resolve_close_budget_without_attempt_returns_the_seed_budget() -> None:
    """No attempt yields the seed budget the daemon stamps on a fresh attempt."""
    budget = resolve_close_budget()
    assert budget.repair == DEFAULT_CLOSE_REPAIR_BUDGET
    assert budget.infrastructure_retry == DEFAULT_CLOSE_INFRASTRUCTURE_RETRY_BUDGET


def test_resolve_close_budget_reads_a_live_attempt_remaining_budget() -> None:
    """A live attempt yields its own remaining counters, not the seed."""
    budget = resolve_close_budget(attempt=_attempt(repair=0, infrastructure_retry=5))
    assert budget.repair == 0
    assert budget.infrastructure_retry == 5


def test_resolve_close_budget_rejects_a_non_attempt() -> None:
    """A wrong-typed argument fails fast instead of silently seeding a default."""
    with pytest.raises(TypeError, match="attempt must be a CloseAttempt or None"):
        resolve_close_budget(attempt="CA-01")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# CloseBudget
# --------------------------------------------------------------------------


def test_close_budget_rejects_a_negative_axis() -> None:
    """A negative budget is not representable (the counters are ``ge=0``)."""
    with pytest.raises(ValidationError):
        CloseBudget(repair=-1, infrastructure_retry=1)


def test_close_budget_forbids_unknown_keys() -> None:
    """The budget model forbids extras, so a typo can never silently pass."""
    with pytest.raises(ValidationError):
        CloseBudget.model_validate({"repair": 1, "infrastructure_retry": 1, "repare": 1})


def test_close_budget_total_repair_attempts_counts_the_first_dispatch() -> None:
    """The display denominator is the first dispatch plus each funded repair."""
    assert CloseBudget(repair=0, infrastructure_retry=0).total_repair_attempts == 1
    assert CloseBudget(repair=1, infrastructure_retry=0).total_repair_attempts == 2
    assert CloseBudget(repair=7, infrastructure_retry=0).total_repair_attempts == 8


def test_close_budget_remaining_reads_each_axis() -> None:
    """Each axis reads back the counter it bounds."""
    budget = CloseBudget(repair=2, infrastructure_retry=3)
    assert budget.remaining("repair") == 2
    assert budget.remaining("infrastructure_retry") == 3


def test_close_budget_remaining_rejects_an_unknown_axis() -> None:
    """An unknown axis raises rather than defaulting to a funded number."""
    with pytest.raises(KeyError, match="unknown close budget axis"):
        CloseBudget(repair=1, infrastructure_retry=1).remaining("repare")  # type: ignore[arg-type]


def test_close_budget_exhausted_axes_orders_by_the_axis_contract() -> None:
    """Exhausted axes come back in :data:`CLOSE_BUDGET_AXES` order."""
    assert CloseBudget(repair=1, infrastructure_retry=1).exhausted_axes() == ()
    assert CloseBudget(repair=0, infrastructure_retry=1).exhausted_axes() == ("repair",)
    assert CloseBudget(repair=1, infrastructure_retry=0).exhausted_axes() == (
        "infrastructure_retry",
    )
    assert CloseBudget(repair=0, infrastructure_retry=0).exhausted_axes() == CLOSE_BUDGET_AXES


def test_close_budget_receipt_ref_names_the_axis_and_the_funded_value() -> None:
    """The receipt ref carries the attempt, the exhausted axis and the funded value."""
    ref = CloseBudget(repair=2, infrastructure_retry=1).receipt_ref(
        attempt_id="CA-01",
        axis="repair",
    )
    assert ref == f"{CLOSE_BUDGET_RECEIPT_PREFIX}:CA-01:repair:2"


def test_close_budget_receipt_ref_rejects_an_empty_attempt_id() -> None:
    """An anonymous receipt would be unattributable, so it is refused."""
    with pytest.raises(ValueError, match="non-empty close attempt id"):
        CloseBudget(repair=1, infrastructure_retry=1).receipt_ref(attempt_id="", axis="repair")


def test_close_budget_receipt_ref_rejects_an_unknown_axis() -> None:
    """An unknown axis cannot be named in a receipt."""
    with pytest.raises(KeyError, match="unknown close budget axis"):
        CloseBudget(repair=1, infrastructure_retry=1).receipt_ref(
            attempt_id="CA-01",
            axis="repare",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# latest_close_attempt
# --------------------------------------------------------------------------


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
