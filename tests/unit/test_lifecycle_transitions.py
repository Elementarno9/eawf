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
    activate_iter,
    activate_phase,
    add_subproject,
    archive_phase,
    claim_wave,
    close_iter,
    close_phase,
    close_wave,
    edit_wave_plan,
    fail_wave,
    open_iter,
    open_phase,
    plan_iter,
    plan_phase,
    plan_wave,
    remove_wave_plan,
    reopen_phase,
    set_wave_deps,
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


# ---- Planned-scope transitions (W01) ----------------------------------------


def test_plan_phase_creates_planned_phase() -> None:
    state = _empty_state()
    phase = plan_phase(
        state,
        phase_id="P01",
        title="planning",
        source_brief_ids=["RES-2026-05-14-001"],
    )
    assert phase.status == PhaseStatus.PLANNED
    assert phase.source_brief_ids == ["RES-2026-05-14-001"]
    assert phase.depends_on == []
    assert state.current.phase_id is None


def test_plan_phase_duplicate_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="p")
    with pytest.raises(LifecycleError, match="already exists"):
        plan_phase(state, phase_id="P01", title="dup")


def test_plan_phase_self_dep_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="cannot depend on itself"):
        plan_phase(state, phase_id="P01", title="x", depends_on=["P01"])


def test_plan_phase_unknown_dep_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase dep"):
        plan_phase(state, phase_id="P02", title="x", depends_on=["P01"])


def test_plan_phase_cycle_rejected() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="a")
    plan_phase(state, phase_id="P02", title="b", depends_on=["P01"])
    state.phases["P01"].depends_on.append("P02")
    with pytest.raises(LifecycleError, match="cycle"):
        plan_phase(state, phase_id="P03", title="c", depends_on=["P01"])


def test_activate_phase_requires_planned_status() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    with pytest.raises(LifecycleError, match="only planned phases"):
        activate_phase(state, phase_id="P01")


def test_activate_phase_requires_one_wave() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="it")
    with pytest.raises(LifecycleError, match="no planned waves"):
        activate_phase(state, phase_id="P01")


def test_activate_phase_blocks_on_unclosed_dep() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="a")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    plan_phase(state, phase_id="P02", title="b", depends_on=["P01"])
    plan_iter(state, iter_id="P02-I01", phase_id="P02", title="i")
    plan_wave(state, wave_id="P02-I01-W01", iter_id="P02-I01", title="w", file_scopes=["x"])
    with pytest.raises(LifecycleError, match="blocked on un-closed dep phases"):
        activate_phase(state, phase_id="P02")


def test_activate_phase_happy_sets_current() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    phase = activate_phase(state, phase_id="P01")
    assert phase.status == PhaseStatus.ACTIVE
    assert state.current.phase_id == "P01"


def test_archive_phase_happy() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    phase = archive_phase(state, phase_id="P01")
    assert phase.status == PhaseStatus.ARCHIVED
    assert phase.closed_at is not None


def test_archive_phase_active_rejected() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    with pytest.raises(LifecycleError, match="only planned phases"):
        archive_phase(state, phase_id="P01")


def test_plan_iter_creates_planned_status() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    it = plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    assert it.status == IterStatus.PLANNED
    assert state.current.iter_id is None


def test_plan_iter_under_active_phase_ok() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    it = plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    assert it.status == IterStatus.PLANNED


def test_plan_iter_under_closed_phase_rejected() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", commit="abc", outcome="ok")
    close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    close_phase(state, phase_id="P01", audit_id="AUD-2")
    with pytest.raises(LifecycleError, match="not open"):
        plan_iter(state, iter_id="P01-I02", phase_id="P01", title="i2")


def test_activate_iter_sets_current() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    it = activate_iter(state, iter_id="P01-I01")
    assert it.status == IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I01"


def test_activate_iter_non_planned_rejected() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    with pytest.raises(LifecycleError, match="only planned iters"):
        activate_iter(state, iter_id="P01-I01")


def test_edit_wave_plan_mutates_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="orig",
        file_scopes=["x"],
    )
    w = edit_wave_plan(
        state,
        wave_id="P01-I01-W01",
        title="updated",
        file_scopes=["src/y/"],
        success_criteria=["criterion"],
    )
    assert w.title == "updated"
    assert w.file_scopes == ["src/y/"]
    assert w.success_criteria == ["criterion"]


def test_edit_wave_plan_non_pending_rejected() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
    )
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    with pytest.raises(LifecycleError, match="not pending"):
        edit_wave_plan(state, wave_id="P01-I01-W01", title="late")


def test_remove_wave_plan_deletes_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    remove_wave_plan(state, wave_id="P01-I01-W01")
    assert "P01-I01-W01" not in state.waves
    assert state.iters["P01-I01"].wave_ids == []


def test_remove_wave_plan_blocked_by_blocks_list_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        deps=["P01-I01-W01"],
    )
    with pytest.raises(LifecycleError, match="blocks other waves"):
        remove_wave_plan(state, wave_id="P01-I01-W01")


def test_set_wave_deps_updates_blocks_index() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W03", iter_id="P01-I01", title="w3", file_scopes=["x"])
    set_wave_deps(state, wave_id="P01-I01-W03", deps=["P01-I01-W01", "P01-I01-W02"])
    assert state.waves["P01-I01-W03"].deps == ["P01-I01-W01", "P01-I01-W02"]
    assert state.waves["P01-I01-W01"].blocks == ["P01-I01-W03"]
    assert state.waves["P01-I01-W02"].blocks == ["P01-I01-W03"]
    set_wave_deps(state, wave_id="P01-I01-W03", deps=["P01-I01-W01"])
    assert state.waves["P01-I01-W02"].blocks == []


def test_set_wave_deps_cycle_rolled_back() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        deps=["P01-I01-W01"],
    )
    with pytest.raises(LifecycleError, match="cycle"):
        set_wave_deps(state, wave_id="P01-I01-W01", deps=["P01-I01-W02"])
    assert state.waves["P01-I01-W01"].deps == []


def test_set_wave_deps_non_pending_rejected() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    with pytest.raises(LifecycleError, match="not pending"):
        set_wave_deps(state, wave_id="P01-I01-W01", deps=["P01-I01-W02"])
