"""Tests for the ``effort_bucket`` claim-time gate on :func:`claim_wave`.

The wave-lifecycle gate (P28-I02-W16) rejects a claim when a legacy wave
record has no ``effort_bucket``: estimates, variance, and the EU-projection
table all key off that field, so a bucketless wave silently degrades the
dispatch + reporting story. The error message points the operator at the
one-line fix (``eawf roadmap revise --set-bucket``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, ResponseClause, grandfather_criterion
from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    AgentSession,
    CriteriaFloorWaiver,
    CurrentPointers,
    Project,
    State,
)
from eawf.workflow.lifecycle._errors import LifecycleError, LifecycleGuardError
from eawf.workflow.lifecycle.iter_ import open_iter, plan_iter
from eawf.workflow.lifecycle.phase import open_phase
from eawf.workflow.lifecycle.wave import claim_wave
from eawf.workflow.lifecycle.wave import plan_wave as _plan_wave
from tests.conftest import make_intent


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_wave_state() -> State:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _add_session(state)
    return state


def _add_session(
    state: State,
    *,
    session_id: str = "SES-1",
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    scope_id: str = "QR",
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
) -> AgentSession:
    session = AgentSession(
        id=session_id,
        role=role,
        runtime="claude",
        scope_id=scope_id,
        status=status,
        started_at=datetime.now(UTC),
    )
    state.agent_sessions[session_id] = session
    if status is AgentSessionStatus.ACTIVE:
        state.current.active_session_ids.append(session_id)
    return session


def _claim_criterion() -> CriterionSpec:
    """Build one real typed criterion for claim-path fixtures."""
    return CriterionSpec(
        id="CR-01",
        text="focused pytest exits zero for the claimed lifecycle behavior",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["GATE-1"],
        quality_dimension="functional_suitability",
        measurable_signal="the focused pytest process exits with status zero",
        response=ResponseClause(
            observe="exits",
            object="zero from the focused lifecycle test",
            locus="pytest",
        ),
    )


def plan_wave(state: State, **kwargs: Any) -> Any:
    """Plan a claimable wave unless a test explicitly supplies criteria."""
    kwargs.setdefault("success_criteria", [_claim_criterion()])
    return _plan_wave(state, **kwargs)


def test_claim_wave_rejects_when_effort_bucket_none() -> None:
    """A wave record without an ``effort_bucket`` cannot be claimed.

    The gate surfaces the canonical one-line fix
    (``eawf roadmap revise --set-bucket``) in the error message so the
    operator does not have to read the source to recover.
    """
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    state.waves["P01-I01-W01"].effort_bucket = None
    with pytest.raises(LifecycleError, match="effort_bucket"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # Also assert the operator-facing fix is named in the message so the
    # contract is part of the API surface, not a private string.
    with pytest.raises(LifecycleError, match="eawf roadmap revise --set-bucket"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # Status must not flip when the gate rejects.
    assert state.waves["P01-I01-W01"].status == WaveStatus.PENDING
    assert "P01-I01-W01" not in state.current.active_wave_ids


def test_claim_wave_succeeds_when_effort_bucket_set() -> None:
    """A PENDING wave with a non-None ``effort_bucket`` keeps claiming cleanly."""
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.XS,
        intent=make_intent(),
    )
    w = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert w.status == WaveStatus.CLAIMED
    assert w.claim_session_id == "SES-1"
    assert w.effort_bucket == EffortBucket.XS
    assert "P01-I01-W01" in state.current.active_wave_ids
    assert state.agent_sessions["SES-1"].claimed_wave_ids == ["P01-I01-W01"]


def test_claim_wave_disabled_rejects_historical_floor_waiver_without_mutation() -> None:
    """A pending historical floor waiver cannot enter execution in disabled mode."""
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        success_criteria=[grandfather_criterion("historical criterion", index=1)],
        criteria_floor_waiver=CriteriaFloorWaiver(
            reason="historical repair waiver with sufficient detail",
            waived_at=datetime.now(UTC),
        ),
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as raised:
        claim_wave(
            state,
            wave_id="P01-I01-W01",
            session_id="SES-1",
            waiver_mode="disabled",
        )

    assert raised.value.code == "waiver_mode_disabled"
    assert state.model_dump_json() == before


def test_claim_wave_disabled_rejects_historical_raw_reason_without_mutation() -> None:
    """Raw criterion waivers reject at claim even when rows predate the policy."""
    state = _seed_wave_state()
    criterion = grandfather_criterion("historical criterion", index=1).model_copy(
        update={"waiver_reason": "old raw criterion waiver"}
    )
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        success_criteria=[criterion],
        criteria_floor_waiver=CriteriaFloorWaiver(
            reason="historical repair waiver with sufficient detail",
            waived_at=datetime.now(UTC),
        ),
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    # Isolate the raw-reason leg from the separate floor-waiver leg.
    state.waves["P01-I01-W01"].criteria_floor_waiver = None
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as raised:
        claim_wave(
            state,
            wave_id="P01-I01-W01",
            session_id="SES-1",
            waiver_mode="disabled",
        )

    assert raised.value.code == "waiver_mode_disabled"
    assert state.model_dump_json() == before


@pytest.mark.parametrize(
    "scope_id",
    ["P01-I01-W01", "P01-I01", "P01", "QR"],
)
def test_claim_wave_accepts_every_scope_in_parent_chain(scope_id: str) -> None:
    """Wave, iter, phase, and project anchors all authorize one claim."""
    state = _seed_wave_state()
    state.agent_sessions["SES-1"].scope_id = scope_id
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )

    wave = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert wave.claim_session_id == "SES-1"


def test_claim_wave_stamps_claimed_at_on_first_claim() -> None:
    """The first claim stamps the wave's ``claimed_at`` work-start fact.

    ``claimed_at`` is unset at plan time (only ``opened_at`` is stamped) and
    becomes a UTC timestamp on the claim transition, so the elapsed-clock
    consumers anchor on work-start instead of plan/creation time.
    """
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    assert state.waves["P01-I01-W01"].claimed_at is None
    before = datetime.now(UTC)
    w = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    after = datetime.now(UTC)
    assert w.claimed_at is not None
    assert before <= w.claimed_at <= after


def test_claim_wave_preserves_claimed_at_on_idempotent_reclaim() -> None:
    """A same-session re-claim is a no-op and never re-bases ``claimed_at``."""
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    first = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    original = first.claimed_at
    assert original is not None
    again = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert again.claimed_at == original


def test_claim_wave_rejects_unknown_session_without_mutation() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-MISSING")

    assert exc_info.value.code == "claim_session_not_found"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_inactive_session_without_mutation() -> None:
    state = _seed_wave_state()
    state.agent_sessions["SES-1"].status = AgentSessionStatus.STALE
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert exc_info.value.code == "claim_session_not_active"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_wrong_scope_without_mutation() -> None:
    state = _seed_wave_state()
    state.agent_sessions["SES-1"].scope_id = "P99"
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert exc_info.value.code == "claim_session_scope_mismatch"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_wrong_role_but_allows_operator_override() -> None:
    state = _seed_wave_state()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        agent_role=AgentSessionRole.REVIEWER,
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()
    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")
    assert exc_info.value.code == "claim_session_role_mismatch"
    assert state.model_dump_json() == before

    operator = _add_session(
        state,
        session_id="SES-OP",
        role=AgentSessionRole.OPERATOR,
        scope_id="P01",
    )
    claimed = claim_wave(state, wave_id=wave.id, session_id=operator.id)
    assert claimed.claim_session_id == operator.id
    assert operator.claimed_wave_ids == [wave.id]


def test_claim_wave_project_session_reuses_across_compatible_waves() -> None:
    state = _seed_wave_state()
    for wave_id in ("P01-I01-W01", "P01-I01-W02"):
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P01-I01",
            title=wave_id,
            file_scopes=["src/"],
            effort_bucket=EffortBucket.M,
            intent=make_intent(),
        )

    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    claim_wave(state, wave_id="P01-I01-W02", session_id="SES-1")

    assert state.agent_sessions["SES-1"].claimed_wave_ids == [
        "P01-I01-W01",
        "P01-I01-W02",
    ]


def test_claim_wave_scoped_session_cannot_reuse_on_sibling() -> None:
    state = _seed_wave_state()
    state.agent_sessions["SES-1"].scope_id = "P01-I01-W01"
    for wave_id in ("P01-I01-W01", "P01-I01-W02"):
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P01-I01",
            title=wave_id,
            file_scopes=["src/"],
            effort_bucket=EffortBucket.M,
            intent=make_intent(),
        )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id="P01-I01-W02", session_id="SES-1")

    assert exc_info.value.code == "claim_session_scope_mismatch"
    assert state.model_dump_json() == before


def test_claim_wave_reverse_binding_blocks_cross_role_reuse_without_session_index() -> None:
    """Historical wave binding remains authoritative when session index is empty."""
    state = _seed_wave_state()
    state.agent_sessions["SES-1"].role = AgentSessionRole.AUDITOR
    first = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="review",
        file_scopes=["src/"],
        agent_role=AgentSessionRole.REVIEWER,
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    second = plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="audit",
        file_scopes=["src/"],
        agent_role=AgentSessionRole.AUDITOR,
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    first.claim_session_id = "SES-1"
    assert state.agent_sessions["SES-1"].claimed_wave_ids == []
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=second.id, session_id="SES-1", out_of_order=True)

    assert exc_info.value.code == "claim_session_role_mismatch"
    assert state.model_dump_json() == before


def test_claim_wave_idempotent_reuse_requires_active_bound_session() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.agent_sessions["SES-1"].status = AgentSessionStatus.STALE
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert exc_info.value.code == "claim_session_not_active"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_missing_parent_iter_without_mutation() -> None:
    state = _seed_wave_state()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    del state.iters[wave.iter_id]
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert exc_info.value.code == "claim_parent_iter_missing"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_missing_parent_phase_without_mutation() -> None:
    state = _seed_wave_state()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    del state.phases["P01"]
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert exc_info.value.code == "claim_parent_phase_missing"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_nonactive_parent_phase_without_mutation() -> None:
    state = _seed_wave_state()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    state.phases["P01"].status = PhaseStatus.PLANNED
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert exc_info.value.code == "claim_parent_phase_not_active"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_terminal_parent_iter_without_mutation() -> None:
    state = _seed_wave_state()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    state.iters[wave.iter_id].status = IterStatus.CLOSED
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert exc_info.value.code == "claim_parent_iter_terminal"
    assert state.model_dump_json() == before


def test_claim_wave_rejects_planned_iter_with_active_sibling_without_mutation() -> None:
    state = _seed_wave_state()
    plan_iter(state, iter_id="P01-I02", phase_id="P01", title="second")
    wave = plan_wave(
        state,
        wave_id="P01-I02-W01",
        iter_id="P01-I02",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert exc_info.value.code == "claim_active_iter_conflict"
    assert state.model_dump_json() == before


def test_claim_wave_autoactivates_planned_iter_without_clobbering_active_waves() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="first")
    _add_session(state)
    first = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="first wave",
        file_scopes=["src/one"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    second = plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="second wave",
        file_scopes=["src/two"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )

    claim_wave(state, wave_id=first.id, session_id="SES-1")
    claim_wave(state, wave_id=second.id, session_id="SES-1")

    assert state.iters["P01-I01"].status is IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I01"
    assert state.current.active_wave_ids == [first.id, second.id]


def test_claim_wave_rejects_empty_criteria_without_mutation() -> None:
    state = _seed_wave_state()
    wave = _plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert exc_info.value.code == "claim_criteria_empty"
    assert state.model_dump_json() == before


def test_claim_wave_counts_statuses_when_active_pointer_is_stale() -> None:
    state = _seed_wave_state()
    for wave_id in ("P01-I01-W01", "P01-I01-W02"):
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P01-I01",
            title=wave_id,
            file_scopes=["src/"],
            effort_bucket=EffortBucket.M,
            intent=make_intent(),
        )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1", max_parallel_waves=1)
    state.current.active_wave_ids = []
    before = state.model_dump_json()

    with pytest.raises(LifecycleGuardError) as exc_info:
        claim_wave(
            state,
            wave_id="P01-I01-W02",
            session_id="SES-1",
            out_of_order=True,
            max_parallel_waves=1,
        )

    assert exc_info.value.code == "claim_parallel_limit_reached"
    assert state.model_dump_json() == before


def test_claim_wave_uses_parent_rows_when_current_pointers_are_stale() -> None:
    state = _seed_wave_state()
    wave = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    state.current.phase_id = None
    state.current.iter_id = None

    claimed = claim_wave(state, wave_id=wave.id, session_id="SES-1")

    assert claimed.status is WaveStatus.CLAIMED
    assert state.current.active_wave_ids == [wave.id]
