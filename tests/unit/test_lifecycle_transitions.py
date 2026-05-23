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
    start_wave,
    switch_subproject,
)
from eawf.state.enums import (
    DecisionStatus,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.state.models import (
    CurrentPointers,
    Decision,
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


def _seed_closed_wave(state: State, phase_id: str, iter_id: str | None = None) -> None:
    """Add an iter + closed wave under *phase_id* so close_phase can succeed.

    ``close_phase`` requires at least one CLOSED wave (P19-W03), every CLOSED
    child iter to carry its ``audit_id``, and — for a single-wave phase — an
    ACTIVE scope-collapse decision (P27-I02-W36). This helper seeds all three
    so tests exercising close_phase / reopen_phase paths satisfy the gates
    without inflating each test body.
    """
    iter_id = iter_id or f"{phase_id}-I01"
    wave_id = f"{phase_id}-I01-W01"
    open_iter(state, iter_id=iter_id, phase_id=phase_id, title="i")
    plan_wave(state, wave_id=wave_id, iter_id=iter_id, title="w", file_scopes=["x"])
    claim_wave(state, wave_id=wave_id, session_id=f"SES-seed-{phase_id}")
    close_wave(state, wave_id=wave_id, outcome="ok")
    state.iters[iter_id].status = IterStatus.CLOSED
    state.iters[iter_id].closed_at = datetime.now(UTC)
    state.iters[iter_id].audit_id = f"AUD-iter-{phase_id}"
    state.decisions[f"D-COLLAPSE-{phase_id}"] = Decision(
        id=f"D-COLLAPSE-{phase_id}",
        scope_id=phase_id,
        title=f"{phase_id} scope collapse: ship as single-wave phase",
        rationale="scope collapse accepted; follow-up moved to the next phase",
        alternatives=["open another wave", "leave the phase open"],
        status=DecisionStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )


def test_close_phase_happy_clears_current() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    _seed_closed_wave(state, "P01")
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
    _seed_closed_wave(state, "P01")
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
    _seed_closed_wave(state, "P01")
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
    _seed_closed_wave(state, "P01")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    open_phase(state, phase_id="P02", title="y")
    reopen_phase(state, phase_id="P01")
    assert state.current.phase_id == "P02"


def test_reopen_phase_then_open_iter_succeeds() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    _seed_closed_wave(state, "P01")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    reopen_phase(state, phase_id="P01")
    it = open_iter(state, iter_id="P01-I02", phase_id="P01", title="follow-up")
    assert it.status == IterStatus.ACTIVE


# ---- Iter -------------------------------------------------------------------


def test_open_iter_unknown_phase_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase"):
        open_iter(state, iter_id="P01-I01", phase_id="P01", title="x")


def test_open_iter_closed_phase_raises() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    _seed_closed_wave(state, "P01")
    close_phase(state, phase_id="P01", audit_id="AUD-1")
    with pytest.raises(LifecycleError, match="not open"):
        open_iter(state, iter_id="P01-I02", phase_id="P01", title="y")


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
        close_wave(state, wave_id="P01-I01-W01", outcome="ok")


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
    w = close_wave(state, wave_id="P01-I01-W01", outcome="ok")
    assert w.status == WaveStatus.CLOSED
    assert w.outcome == "ok"
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
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")
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


def test_start_wave_unknown_raises() -> None:
    state = _seed_wave_state()
    with pytest.raises(LifecycleError, match="unknown wave"):
        start_wave(state, wave_id="P01-I01-W01")


def test_start_wave_pending_rejected() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    with pytest.raises(LifecycleError, match="not claimed"):
        start_wave(state, wave_id="P01-I01-W01")


def test_start_wave_terminal_rejected() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")
    with pytest.raises(LifecycleError, match="not claimed"):
        start_wave(state, wave_id="P01-I01-W01")


def test_start_wave_happy_claimed_to_in_progress() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    w = start_wave(state, wave_id="P01-I01-W01")
    assert w.status == WaveStatus.IN_PROGRESS
    # The claim binding + active pointer are preserved across the flip.
    assert w.claim_session_id == "SES-1"
    assert "P01-I01-W01" in state.current.active_wave_ids


def test_start_wave_idempotent_when_already_in_progress() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    start_wave(state, wave_id="P01-I01-W01")
    # Second call is a no-op, no error, status unchanged.
    w = start_wave(state, wave_id="P01-I01-W01")
    assert w.status == WaveStatus.IN_PROGRESS


def test_start_wave_then_close_succeeds() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    start_wave(state, wave_id="P01-I01-W01")
    w = close_wave(state, wave_id="P01-I01-W01", outcome="done")
    assert w.status == WaveStatus.CLOSED
    assert "P01-I01-W01" not in state.current.active_wave_ids


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


def test_archive_phase_clears_current_pointer() -> None:
    """Archiving the phase under the current pointer nulls the pointer.

    Mirrors :func:`close_phase`: a stale pointer at an archived phase would
    otherwise mis-scope downstream readers (e.g. the status pane counters).
    """
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    state.current.phase_id = "P01"
    state.current.iter_id = "P01-I01"
    state.current.active_wave_ids = ["P01-I01-W01"]
    archive_phase(state, phase_id="P01")
    assert state.current.phase_id is None
    assert state.current.iter_id is None
    assert state.current.active_wave_ids == []


def test_archive_phase_leaves_other_pointer_untouched() -> None:
    """Archiving a non-pointer phase leaves ``current.phase_id`` alone."""
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    open_phase(state, phase_id="P02", title="active")
    assert state.current.phase_id == "P02"
    archive_phase(state, phase_id="P01")
    assert state.current.phase_id == "P02"


def test_archive_phase_cascades_pending_waves_to_abandoned() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["a"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["b"])
    archive_phase(state, phase_id="P01")
    assert state.waves["P01-I01-W01"].status == WaveStatus.ABANDONED
    assert state.waves["P01-I01-W02"].status == WaveStatus.ABANDONED
    # Closure timestamp is stamped so the closure-timestamp invariant holds.
    assert state.waves["P01-I01-W01"].closed_at is not None
    assert state.waves["P01-I01-W02"].closed_at is not None


def test_archive_phase_cascades_planned_iter_to_abandoned() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["a"])
    archive_phase(state, phase_id="P01")
    assert state.iters["P01-I01"].status == IterStatus.ABANDONED
    assert state.iters["P01-I01"].closed_at is not None


def test_archive_phase_leaves_no_pending_waves_validates() -> None:
    """After cascade, the candidate state passes closure-timestamp invariants."""
    from eawf.validate.strict import validate_state

    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["a"])
    archive_phase(state, phase_id="P01")
    report = validate_state(state.model_dump(mode="json"))
    assert report.state is not None
    assert report.violations == []


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
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")
    close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    state.decisions["D-COLLAPSE-P01"] = Decision(
        id="D-COLLAPSE-P01",
        scope_id="P01",
        title="P01 scope collapse: single-wave phase",
        rationale="scope collapse accepted; follow-up deferred",
        status=DecisionStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
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


def test_claim_wave_rejects_unmet_deps() -> None:
    """P19-W02: claim is blocked when any dep wave is not CLOSED."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(state, wave_id="P20-I01-W01", iter_id="P20-I01", title="a", file_scopes=["x"])
    plan_wave(
        state,
        wave_id="P20-I01-W02",
        iter_id="P20-I01",
        title="b",
        file_scopes=["x"],
        deps=["P20-I01-W01"],
    )
    with pytest.raises(LifecycleError, match="un-closed dep waves"):
        claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2")


def test_claim_wave_rejects_skipping_lower_w_sibling() -> None:
    """P19-W02: skipping a lower-W## ready sibling is rejected."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(state, wave_id="P20-I01-W01", iter_id="P20-I01", title="a", file_scopes=["x"])
    plan_wave(state, wave_id="P20-I01-W02", iter_id="P20-I01", title="b", file_scopes=["x"])
    with pytest.raises(LifecycleError, match="lower-numbered ready siblings"):
        claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2")


def test_claim_wave_out_of_order_overrides_monotonic_gate() -> None:
    """P19-W02: --out-of-order bypasses the monotonic gate."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(state, wave_id="P20-I01-W01", iter_id="P20-I01", title="a", file_scopes=["x"])
    plan_wave(state, wave_id="P20-I01-W02", iter_id="P20-I01", title="b", file_scopes=["x"])
    w = claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2", out_of_order=True)
    assert w.status == WaveStatus.CLAIMED


def test_claim_wave_monotonic_gate_allows_after_w01_claimed() -> None:
    """W02 may be claimed once W01 is CLAIMED/IN_PROGRESS/CLOSED."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(state, wave_id="P20-I01-W01", iter_id="P20-I01", title="a", file_scopes=["x"])
    plan_wave(state, wave_id="P20-I01-W02", iter_id="P20-I01", title="b", file_scopes=["x"])
    claim_wave(state, wave_id="P20-I01-W01", session_id="SES-1")
    # W01 is CLAIMED so it is no longer PENDING; W02 may now claim
    # even though W01 hasn't closed yet (only closed-dep waves block).
    w = claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2")
    assert w.status == WaveStatus.CLAIMED


def test_close_phase_rejects_when_no_closed_wave() -> None:
    """P19-W03: phase close demands at least one CLOSED wave."""
    state = _empty_state()
    open_phase(state, phase_id="P10", title="t")
    open_iter(state, iter_id="P10-I01", phase_id="P10", title="i")
    plan_wave(state, wave_id="P10-I01-W01", iter_id="P10-I01", title="w", file_scopes=["x"])
    # iter and wave are still open — fail on open-children first
    with pytest.raises(LifecycleError, match="open iters"):
        close_phase(state, phase_id="P10", audit_id="AUD-1")
    # close the iter without closing the wave (abandon path)
    state.waves["P10-I01-W01"].status = WaveStatus.ABANDONED
    state.iters["P10-I01"].status = IterStatus.CLOSED
    state.iters["P10-I01"].closed_at = datetime.now(UTC)
    with pytest.raises(LifecycleError, match="no closed waves"):
        close_phase(state, phase_id="P10", audit_id="AUD-1")


def test_close_phase_accepts_when_one_wave_closed() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P11", title="t")
    open_iter(state, iter_id="P11-I01", phase_id="P11", title="i")
    plan_wave(state, wave_id="P11-I01-W01", iter_id="P11-I01", title="w", file_scopes=["x"])
    claim_wave(state, wave_id="P11-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P11-I01-W01", outcome="ok")
    state.iters["P11-I01"].status = IterStatus.CLOSED
    state.iters["P11-I01"].closed_at = datetime.now(UTC)
    state.iters["P11-I01"].audit_id = "AUD-iter"
    state.decisions["D-COLLAPSE-P11"] = Decision(
        id="D-COLLAPSE-P11",
        scope_id="P11",
        title="P11 scope collapse: single-wave phase",
        rationale="scope collapse accepted; follow-up deferred",
        status=DecisionStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
    p = close_phase(state, phase_id="P11", audit_id="AUD-2")
    assert p.status == PhaseStatus.CLOSED


def test_close_phase_rejects_closed_iter_missing_audit() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P12", title="t")
    open_iter(state, iter_id="P12-I01", phase_id="P12", title="i")
    # Two closed waves so the single-wave gate cannot mask the audit gate.
    for n in (1, 2):
        wave_id = f"P12-I01-W0{n}"
        plan_wave(state, wave_id=wave_id, iter_id="P12-I01", title="w", file_scopes=["x"])
        claim_wave(state, wave_id=wave_id, session_id=f"SES-{n}")
        close_wave(state, wave_id=wave_id, outcome="ok")
    # Close the iter WITHOUT an audit_id — the transition must reject.
    state.iters["P12-I01"].status = IterStatus.CLOSED
    state.iters["P12-I01"].closed_at = datetime.now(UTC)
    with pytest.raises(LifecycleError, match="closed iters missing audit"):
        close_phase(state, phase_id="P12", audit_id="AUD-1")


def test_close_phase_rejects_single_wave_without_decision() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P13", title="t")
    open_iter(state, iter_id="P13-I01", phase_id="P13", title="i")
    plan_wave(state, wave_id="P13-I01-W01", iter_id="P13-I01", title="w", file_scopes=["x"])
    claim_wave(state, wave_id="P13-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P13-I01-W01", outcome="ok")
    state.iters["P13-I01"].status = IterStatus.CLOSED
    state.iters["P13-I01"].closed_at = datetime.now(UTC)
    state.iters["P13-I01"].audit_id = "AUD-iter"
    # Single closed wave, no scope-collapse decision — the transition rejects.
    with pytest.raises(LifecycleError, match="single closed wave"):
        close_phase(state, phase_id="P13", audit_id="AUD-1")


def test_close_phase_allows_single_wave_with_scope_collapse_decision() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P14", title="t")
    open_iter(state, iter_id="P14-I01", phase_id="P14", title="i")
    plan_wave(state, wave_id="P14-I01-W01", iter_id="P14-I01", title="w", file_scopes=["x"])
    claim_wave(state, wave_id="P14-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P14-I01-W01", outcome="ok")
    state.iters["P14-I01"].status = IterStatus.CLOSED
    state.iters["P14-I01"].closed_at = datetime.now(UTC)
    state.iters["P14-I01"].audit_id = "AUD-iter"
    state.decisions["D-COLLAPSE-P14"] = Decision(
        id="D-COLLAPSE-P14",
        scope_id="P14",
        title="P14 scope collapse: ship single-wave phase",
        rationale="scope collapse accepted; remaining work moved to next phase",
        status=DecisionStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
    p = close_phase(state, phase_id="P14", audit_id="AUD-1")
    assert p.status == PhaseStatus.CLOSED


def test_close_phase_rejects_single_wave_when_decision_superseded() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P15", title="t")
    open_iter(state, iter_id="P15-I01", phase_id="P15", title="i")
    plan_wave(state, wave_id="P15-I01-W01", iter_id="P15-I01", title="w", file_scopes=["x"])
    claim_wave(state, wave_id="P15-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P15-I01-W01", outcome="ok")
    state.iters["P15-I01"].status = IterStatus.CLOSED
    state.iters["P15-I01"].closed_at = datetime.now(UTC)
    state.iters["P15-I01"].audit_id = "AUD-iter"
    # A scope-collapse decision exists but is no longer ACTIVE — must not count.
    state.decisions["D-COLLAPSE-P15"] = Decision(
        id="D-COLLAPSE-P15",
        scope_id="P15",
        title="P15 scope collapse: single-wave phase",
        rationale="scope collapse",
        status=DecisionStatus.SUPERSEDED,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(LifecycleError, match="single closed wave"):
        close_phase(state, phase_id="P15", audit_id="AUD-1")


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


# ---- P19-W12: ACTIVE-phase revise on PENDING waves --------------------------


def test_edit_wave_plan_under_active_phase_ok_when_pending() -> None:
    """P19-W12: PENDING waves remain editable after the parent phase
    flips ACTIVE — the load-bearing invariant is the wave-level PENDING
    check, not the phase status."""
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    w = edit_wave_plan(state, wave_id="P01-I01-W01", title="revised mid-flight")
    assert w.title == "revised mid-flight"
    assert state.phases["P01"].status == PhaseStatus.ACTIVE


def test_remove_wave_plan_under_active_phase_ok_when_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    remove_wave_plan(state, wave_id="P01-I01-W02")
    assert "P01-I01-W02" not in state.waves
    assert state.phases["P01"].status == PhaseStatus.ACTIVE


def test_set_wave_deps_under_active_phase_ok_when_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    set_wave_deps(state, wave_id="P01-I01-W02", deps=["P01-I01-W01"])
    assert state.waves["P01-I01-W02"].deps == ["P01-I01-W01"]
    assert state.waves["P01-I01-W01"].blocks == ["P01-I01-W02"]


def test_edit_wave_plan_under_active_phase_rejects_closed_wave() -> None:
    """Even with an ACTIVE parent phase the wave-level PENDING guard
    keeps CLOSED waves frozen."""
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", outcome="done")
    with pytest.raises(LifecycleError, match="not pending"):
        edit_wave_plan(state, wave_id="P01-I01-W01", title="late edit")


# ---- P26-W40: claim auto-activates a PLANNED parent iter --------------------


def test_claim_wave_activates_planned_iter() -> None:
    """First claim under a PLANNED iter flips it ACTIVE + sets the pointer.

    Guards against the lifecycle gap where waves ran/closed while the
    parent iter never left PLANNED (no ACTIVE->PLANNED reversion exists).
    """
    state = _empty_state()
    # Reproduce the bug shape: ACTIVE phase, PLANNED child iter (activate_phase
    # does not cascade-activate the iter), current.iter_id cleared.
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    activate_phase(state, phase_id="P01")
    assert state.phases["P01"].status == PhaseStatus.ACTIVE
    assert state.iters["P01-I01"].status == IterStatus.PLANNED
    assert state.current.iter_id is None
    w = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert w.status == WaveStatus.CLAIMED
    assert state.iters["P01-I01"].status == IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I01"
    assert "P01-I01-W01" in state.current.active_wave_ids


def test_claim_wave_active_iter_unchanged() -> None:
    """Claiming under an already-ACTIVE iter does not churn iter state.

    The iter stays ACTIVE, ``current.iter_id`` keeps pointing at it, and
    a previously-active sibling wave on the pointer is preserved (the
    activation path must not reset ``active_wave_ids``).
    """
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="i")  # ACTIVE iter
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w1", file_scopes=["x"])
    plan_wave(state, wave_id="P01-I01-W02", iter_id="P01-I01", title="w2", file_scopes=["x"])
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    claim_wave(state, wave_id="P01-I01-W02", session_id="SES-2")
    assert state.iters["P01-I01"].status == IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I01"
    assert state.current.active_wave_ids == ["P01-I01-W01", "P01-I01-W02"]


def test_claim_wave_rejected_claim_does_not_activate_iter() -> None:
    """A claim rejected by the monotonic/dep gates leaves the iter PLANNED.

    Auto-activation is a side-effect of the *successful*-claim path only,
    so a gate rejection must not flip the parent iter.
    """
    state = _empty_state()
    plan_phase(state, phase_id="P20", title="t")
    plan_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(state, wave_id="P20-I01-W01", iter_id="P20-I01", title="a", file_scopes=["x"])
    plan_wave(state, wave_id="P20-I01-W02", iter_id="P20-I01", title="b", file_scopes=["x"])
    activate_phase(state, phase_id="P20")
    assert state.iters["P20-I01"].status == IterStatus.PLANNED
    # W02 skips the lower-numbered ready sibling W01 -> monotonic gate rejects.
    with pytest.raises(LifecycleError, match="lower-numbered ready siblings"):
        claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2")
    assert state.iters["P20-I01"].status == IterStatus.PLANNED
    assert state.current.iter_id is None
    # A dep-gate rejection likewise leaves the iter PLANNED.
    plan_wave(
        state,
        wave_id="P20-I01-W03",
        iter_id="P20-I01",
        title="c",
        file_scopes=["x"],
        deps=["P20-I01-W01"],
    )
    with pytest.raises(LifecycleError, match="un-closed dep waves"):
        claim_wave(state, wave_id="P20-I01-W03", session_id="SES-3", out_of_order=True)
    assert state.iters["P20-I01"].status == IterStatus.PLANNED
    assert state.current.iter_id is None


def test_claim_wave_under_terminal_iter_rejected_and_not_activated() -> None:
    """A wave under a CLOSED iter cannot be claimed and the iter is untouched.

    The wave's PENDING gate is the load-bearing reject (a CLOSED iter only
    holds non-PENDING waves); the activation guard additionally only fires
    for PLANNED iters, so a terminal iter is never resurrected.
    """
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", title="w", file_scopes=["x"])
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")
    close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert state.iters["P01-I01"].status == IterStatus.CLOSED
    with pytest.raises(LifecycleError, match="cannot be claimed"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-2")
    assert state.iters["P01-I01"].status == IterStatus.CLOSED
