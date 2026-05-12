"""Unit tests for :mod:`eawf.lifecycle.transitions`.

Each test starts from a small typed :class:`State` constructed via
:func:`_empty_state` and exercises one transition. The test file is the
authoritative spec for the parent guards, idempotency, and closure rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.lifecycle.transitions import (
    LifecycleError,
    add_subproject,
    claim_wave,
    close_iter,
    close_phase,
    close_wave,
    fail_wave,
    open_iter,
    open_phase,
    plan_wave,
    reopen_phase,
    switch_subproject,
)
from eawf.state.enums import (
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.state.models import (
    CurrentPointers,
    Project,
    State,
)


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


# ---- Subproject -------------------------------------------------------------


def test_add_subproject_happy() -> None:
    state = _empty_state()
    sub = add_subproject(state, code="COLLAR", kind="strategy", title="Collar")
    assert sub.code == "COLLAR"
    assert state.subprojects is not None
    assert "COLLAR" in state.subprojects


def test_add_subproject_duplicate_raises() -> None:
    state = _empty_state()
    add_subproject(state, code="COLLAR", kind="strategy", title="Collar")
    with pytest.raises(LifecycleError, match="already exists"):
        add_subproject(state, code="COLLAR", kind="strategy", title="x")


def test_add_subproject_no_project_raises() -> None:
    state = _empty_state()
    state.project = None
    state.scope_kind = ScopeKind.WORKSPACE
    with pytest.raises(LifecycleError, match="no project"):
        add_subproject(state, code="COLLAR", kind="x", title="y")


def test_switch_subproject_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown"):
        switch_subproject(state, code="X")


def test_switch_subproject_happy() -> None:
    state = _empty_state()
    add_subproject(state, code="COLLAR", kind="x", title="y")
    switch_subproject(state, code="COLLAR")
    assert state.current.subproject_id == "COLLAR"


# ---- Phase ------------------------------------------------------------------


def test_open_phase_sets_active_and_current() -> None:
    state = _empty_state()
    phase = open_phase(state, phase_id="P01", title="bootstrap")
    assert phase.status == PhaseStatus.ACTIVE
    assert state.current.phase_id == "P01"


def test_open_phase_duplicate_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    with pytest.raises(LifecycleError, match="already exists"):
        open_phase(state, phase_id="P01", title="y")


def test_close_phase_with_open_iter_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    with pytest.raises(LifecycleError, match="open iters"):
        close_phase(state, phase_id="P01", audit_id="AUD-1")


def test_close_phase_happy_clears_current() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    p = close_phase(state, phase_id="P01", audit_id="AUD-1", checkpoint="abc")
    assert p.status == PhaseStatus.CLOSED
    assert p.audit_id == "AUD-1"
    assert state.current.phase_id is None


def test_close_phase_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase"):
        close_phase(state, phase_id="P03", audit_id="X")


def test_close_phase_already_closed_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    with pytest.raises(LifecycleError, match="cannot close"):
        close_phase(state, phase_id="P01", audit_id="AUD-2")


def test_reopen_phase_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase"):
        reopen_phase(state, phase_id="P99")


def test_reopen_phase_active_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    with pytest.raises(LifecycleError, match="only closed phases can reopen"):
        reopen_phase(state, phase_id="P01")


def test_reopen_phase_happy_restores_active_and_current() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    assert state.current.phase_id is None
    p = reopen_phase(state, phase_id="P01")
    assert p.status == PhaseStatus.ACTIVE
    assert p.closed_at is None
    assert p.audit_id == "AUD-1"  # preserved for traceability
    assert state.current.phase_id == "P01"


def test_reopen_phase_does_not_steal_current_when_other_phase_active() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    open_phase(state, phase_id="P02", title="y")
    reopen_phase(state, phase_id="P01")
    assert state.current.phase_id == "P02"


def test_reopen_phase_then_open_iter_succeeds() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    reopen_phase(state, phase_id="P01")
    it = open_iter(state, iter_id="P01-I01", phase_id="P01", title="follow-up")
    assert it.status == IterStatus.ACTIVE


# ---- Iter -------------------------------------------------------------------


def test_open_iter_unknown_phase_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase"):
        open_iter(state, iter_id="P01-I01", phase_id="P01", title="x")


def test_open_iter_closed_phase_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    with pytest.raises(LifecycleError, match="not open"):
        open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")


def test_open_iter_happy() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    it = open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    assert it.status == IterStatus.ACTIVE
    assert state.iters["P01-I01"].phase_id == "P01"
    assert state.phases["P01"].iter_ids == ["P01-I01"]
    assert state.current.iter_id == "P01-I01"


def test_close_iter_with_open_wave_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    with pytest.raises(LifecycleError, match="open waves"):
        close_iter(state, iter_id="P01-I01", audit_id="AUD-1")


def test_close_iter_happy() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED
    assert state.current.iter_id is None


# ---- Wave -------------------------------------------------------------------


def _seed_wave_state() -> State:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    return state


def test_plan_wave_unknown_iter_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown iter"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="w",
            file_scopes=["src/"],
        )


def test_plan_wave_closed_iter_raises() -> None:
    state = _seed_wave_state()
    close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    with pytest.raises(LifecycleError, match="not open"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="w",
            file_scopes=["src/"],
        )


def test_plan_wave_duplicate_raises() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    with pytest.raises(LifecycleError, match="already exists"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="x",
            file_scopes=["src/"],
        )


def test_plan_wave_unknown_dep_raises() -> None:
    state = _seed_wave_state()
    with pytest.raises(LifecycleError, match="unknown dep"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="w",
            file_scopes=["src/"],
            deps=["P01-I01-W99"],
        )


def test_plan_wave_happy_appends_to_iter() -> None:
    state = _seed_wave_state()
    w = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    assert w.status == WaveStatus.PENDING
    assert state.iters["P01-I01"].wave_ids == ["P01-I01-W01"]


def test_claim_wave_unknown_raises() -> None:
    state = _seed_wave_state()
    with pytest.raises(LifecycleError, match="unknown wave"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="S")


def test_claim_wave_happy_inserts_active() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    w = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert w.status == WaveStatus.CLAIMED
    assert w.claim_session_id == "SES-1"
    assert "P01-I01-W01" in state.current.active_wave_ids


def test_claim_wave_idempotent_same_session() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # Second call with same session: no-op, no error.
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert state.waves["P01-I01-W01"].claim_session_id == "SES-1"


def test_claim_wave_other_session_rejected() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    with pytest.raises(LifecycleError, match="cannot be claimed"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-2")


def test_close_wave_pending_rejected() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    with pytest.raises(LifecycleError, match="not claimed"):
        close_wave(state, wave_id="P01-I01-W01", commit="abc", outcome="ok")


def test_close_wave_happy_clears_active() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    w = close_wave(state, wave_id="P01-I01-W01", commit="abc", outcome="ok")
    assert w.status == WaveStatus.CLOSED
    assert w.commit == "abc"
    assert "P01-I01-W01" not in state.current.active_wave_ids


def test_fail_wave_terminal_rejects() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", commit="abc", outcome="ok")
    with pytest.raises(LifecycleError, match="terminal"):
        fail_wave(state, wave_id="P01-I01-W01", reason="x")


def test_fail_wave_happy() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    w = fail_wave(state, wave_id="P01-I01-W01", reason="tests broke")
    assert w.status == WaveStatus.FAILED
    assert w.outcome == "tests broke"
