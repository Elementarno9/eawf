"""Unit tests for :mod:`eawf.workflow.lifecycle.transitions`.

Each test starts from a small typed :class:`State` constructed via
:func:`_empty_state` and exercises one transition. The test file is the
authoritative spec for the parent guards, idempotency, and closure rules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    QualityDimension,
    ResponseClause,
    grandfather_criterion,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    DecisionStatus,
    DependencyStage,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    TrackKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    Audit,
    CriteriaFloorWaiver,
    CurrentPointers,
    Decision,
    Project,
    State,
    Wave,
    WaveDependencyBarrier,
    WaveDependencyBinding,
    wave_dependency_key,
    wave_dependency_stages,
)
from eawf.platform.profiles.models import CheckpointBlock
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    activate_iter,
    activate_phase,
    add_track,
    archive_phase,
    close_iter,
    close_phase,
    close_wave,
    edit_iter_plan,
    edit_wave_plan,
    fail_wave,
    open_iter,
    open_phase,
    plan_iter,
    plan_phase,
    remove_wave_plan,
    reopen_phase,
    set_wave_deps,
    start_wave,
    switch_track,
)
from eawf.workflow.lifecycle.transitions import (
    plan_wave as _plan_wave,
)
from tests._session_helpers import claim_wave_with_session as claim_wave
from tests.conftest import make_intent


def _claimable_criterion() -> CriterionSpec:
    """Build one real typed criterion for transition fixtures that claim."""
    return CriterionSpec(
        id="CR-CLAIM",
        text="focused lifecycle transition exits with the expected status",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["GATE-CLAIM"],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the focused lifecycle test observes the expected transition",
        response=ResponseClause(
            observe=ObserveVerb.EXITS,
            object="zero from the focused lifecycle test",
            locus=ProofLocus.PYTEST,
        ),
    )


def plan_wave(state: State, **kwargs: Any) -> Wave:
    """Plan a claimable fixture wave unless criteria are explicit."""
    kwargs.setdefault("success_criteria", [_claimable_criterion()])
    return _plan_wave(state, **kwargs)


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


def _add_dependency_metadata(
    state: State,
    *,
    wave_id: str,
    dep_wave_id: str,
) -> tuple[WaveDependencyBarrier, WaveDependencyBinding]:
    barrier = WaveDependencyBarrier(
        wave_id=wave_id,
        dep_wave_id=dep_wave_id,
        start_after="integrated",
        land_after="verified",
        reason="test edge consumes an exact upstream integration",
    )
    binding = WaveDependencyBinding(
        wave_id=wave_id,
        dep_wave_id=dep_wave_id,
        integration_id=f"WI-{dep_wave_id}",
        generation=1,
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        bound_at=datetime.now(UTC),
    )
    key = wave_dependency_key(wave_id, dep_wave_id)
    state.wave_dependency_barriers[key] = barrier
    state.wave_dependency_bindings[key] = binding
    return barrier, binding


def _add_ship_gate_audit(
    state: State,
    *,
    audit_id: str,
    scope_id: str,
    verdict: AuditVerdict | None = AuditVerdict.PASS,
    status: AuditStatus = AuditStatus.COMPLETE,
    kind: AuditKind = AuditKind.SHIP_GATE,
    check_results: list[dict[str, object]] | None = None,
) -> None:
    state.audits = dict(state.audits or {})
    state.audits[audit_id] = Audit(
        id=audit_id,
        scope_id=scope_id,
        kind=kind,
        status=status,
        check_results=list(
            check_results
            if check_results is not None
            else [{"name": "tests", "passed": True, "details": "focused tests passed"}]
        ),
        created_at=datetime.now(UTC),
        verdict=verdict,
    )


# ---- Track ------------------------------------------------------------------


def test_add_track_happy() -> None:
    state = _empty_state()
    track = add_track(state, code="COLLAR", kind=TrackKind.STRATEGY, title="Collar")
    assert track.code == "COLLAR"
    assert state.tracks is not None
    assert "COLLAR" in state.tracks
    assert state.project is not None
    assert state.project.track_ids == ["COLLAR"]


def test_add_track_duplicate_raises() -> None:
    state = _empty_state()
    add_track(state, code="COLLAR", kind=TrackKind.STRATEGY, title="Collar")
    with pytest.raises(LifecycleError, match="already exists"):
        add_track(state, code="COLLAR", kind=TrackKind.STRATEGY, title="x")


def test_add_track_no_project_raises() -> None:
    state = _empty_state()
    state.project = None
    state.scope_kind = ScopeKind.WORKSPACE
    with pytest.raises(LifecycleError, match="no project"):
        add_track(state, code="COLLAR", kind=TrackKind.STRATEGY, title="y")


def test_switch_track_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown"):
        switch_track(state, code="X")


def test_switch_track_happy() -> None:
    state = _empty_state()
    add_track(state, code="COLLAR", kind=TrackKind.STRATEGY, title="y")
    switch_track(state, code="COLLAR")
    assert state.current.track_id == "COLLAR"


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
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id=iter_id,
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    _add_ship_gate_audit(state, audit_id="AUD-1", scope_id=phase_id)


def test_close_phase_happy_clears_current() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    _seed_closed_wave(state, "P01")
    p = close_phase(state, phase_id="P01", audit_id="AUD-1", checkpoint="abc")
    assert p.status == PhaseStatus.CLOSED
    assert p.audit_id == "AUD-1"
    assert state.current.phase_id is None


def test_close_phase_rejects_stub_audit_row() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    _seed_closed_wave(state, "P01")
    _add_ship_gate_audit(
        state,
        audit_id="AUD-1",
        scope_id="P01",
        check_results=[{"name": "stub", "passed": True, "details": "Phase 2 stub"}],
    )
    with pytest.raises(LifecycleError, match="real audit evidence"):
        close_phase(state, phase_id="P01", audit_id="AUD-1")


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
        effort_bucket="M",
        intent=make_intent(),
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


def _odr_criterion(cid: str, *, tier: OracleTier | None) -> CriterionSpec:
    """Build a minimal required CriterionSpec carrying a given oracle tier."""
    return CriterionSpec(
        id=cid,
        text=f"criterion {cid} succeeds and is observable",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="a deterministic check produces a bit verdict",
        required=True,
        oracle_tier=tier,
    )


def _insert_closed_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    criteria: list[CriterionSpec],
) -> None:
    """Insert a CLOSED wave under *iter_id* carrying typed *criteria*."""
    now = datetime.now(UTC)
    state.waves[wave_id] = Wave(
        id=wave_id,
        iter_id=iter_id,
        title="w",
        status=WaveStatus.CLOSED,
        file_scopes=["src/"],
        success_criteria=criteria,
        opened_at=now,
        closed_at=now,
    )


def test_close_iter_low_determinism_criteria_emits_odr_advisory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    # ODR = 1 / 3 ~= 0.33, below the 0.80 default floor.
    _insert_closed_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        criteria=[
            _odr_criterion("CR-01", tier=OracleTier.T1_STATIC),
            _odr_criterion("CR-02", tier=OracleTier.T7_JURY),
            _odr_criterion("CR-03", tier=OracleTier.T7_JURY),
        ],
    )
    with caplog.at_level(logging.WARNING):
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    # Advisory never blocks the close.
    assert it.status == IterStatus.CLOSED
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # The odr module logs the WARNING; close_iter surfaces the finding line.
    assert any("odr_below_floor" in m and "scope='P01-I01'" in m for m in messages)
    assert any(
        "emit_iter_odr_advisory" in m and "iter=P01-I01" in m and "finding=odr_below_floor" in m
        for m in messages
    )


def test_close_iter_all_deterministic_criteria_emits_no_advisory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _insert_closed_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        criteria=[
            _odr_criterion("CR-01", tier=OracleTier.T1_STATIC),
            _odr_criterion("CR-02", tier=OracleTier.T4_CONTRACT),
        ],
    )
    with caplog.at_level(logging.WARNING):
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_close_iter_zero_criteria_takes_sentinel_path_no_advisory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    # A closed wave with no typed criteria -> sentinel (EMPTY_RATIO) path.
    _insert_closed_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        criteria=[],
    )
    with caplog.at_level(logging.WARNING):
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def _insert_closed_wave_with_plan(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    planned: int,
    delivered: int,
    effort_bucket: EffortBucket = EffortBucket.M,
) -> None:
    """Insert a CLOSED wave whose intent plans *planned* criteria and ships *delivered*.

    The plan row count comes from ``intent.planned_steps`` (the planner's
    intended steps); the delivered count is ``len(success_criteria)``. A wave
    is *thin* for the drift pulse when ``delivered < planned``.
    """
    now = datetime.now(UTC)
    state.waves[wave_id] = Wave(
        id=wave_id,
        iter_id=iter_id,
        title="w",
        status=WaveStatus.CLOSED,
        file_scopes=["src/"],
        success_criteria=[
            _odr_criterion(f"CR-{wave_id}-{i:02d}", tier=OracleTier.T1_STATIC)
            for i in range(delivered)
        ],
        effort_bucket=effort_bucket,
        intent=IntentBrief(
            problem="ship the wave deliverable per spec",
            desired_outcome="the deliverable lands with all planned criteria",
            planned_steps=[f"planned step {i:02d} the planner intended" for i in range(planned)],
        ),
        opened_at=now,
        closed_at=now,
    )


def test_close_iter_k_clean_closes_fires_pulse_no_drift(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # K=3 default budget: three clean (delivered >= planned) waves fire one
    # pulse with drift_detected=False; the close proceeds.
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    for idx in range(1, 4):
        _insert_closed_wave_with_plan(
            state,
            wave_id=f"P01-I01-W0{idx}",
            iter_id="P01-I01",
            planned=2,
            delivered=2,
        )
    with caplog.at_level(logging.INFO):
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "emit_iter_drift_pulse" in m and "iter=P01-I01" in m and "drift_detected=false" in m
        for m in messages
    )


def test_close_iter_fewer_than_k_closes_fires_no_pulse(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Two clean closes against the K=3 / D=3.5 default budget (2 M-waves = 2.0
    # EU < 3.5) -> below both arms -> no pulse line at all.
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    for idx in range(1, 3):
        _insert_closed_wave_with_plan(
            state,
            wave_id=f"P01-I01-W0{idx}",
            iter_id="P01-I01",
            planned=2,
            delivered=2,
        )
    with caplog.at_level(logging.INFO):
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED
    messages = [r.getMessage() for r in caplog.records]
    assert not any("emit_iter_drift_pulse" in m for m in messages)


def test_close_iter_thin_wave_barrier_mode_refuses_close() -> None:
    # A thin wave (delivered < planned) under barrier mode refuses the close
    # so the next dispatch cannot proceed against unreconciled drift.
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _insert_closed_wave_with_plan(
        state, wave_id="P01-I01-W01", iter_id="P01-I01", planned=2, delivered=2
    )
    _insert_closed_wave_with_plan(
        state, wave_id="P01-I01-W02", iter_id="P01-I01", planned=4, delivered=1
    )
    _insert_closed_wave_with_plan(
        state, wave_id="P01-I01-W03", iter_id="P01-I01", planned=2, delivered=2
    )
    barrier = CheckpointBlock(checkpoint_mode="barrier")
    with pytest.raises(LifecycleError, match="refuses next dispatch"):
        close_iter(state, iter_id="P01-I01", audit_id="AUD-1", checkpoint=barrier)
    # The refusal is raised BEFORE the close mutation lands -> still active.
    assert state.iters["P01-I01"].status == IterStatus.ACTIVE


def test_close_iter_thin_wave_optimistic_mode_does_not_stall() -> None:
    # The same thin wave under the optimistic default is advisory: the pulse
    # detects drift but the close proceeds (zero parallelism cost on the
    # frontier; the next claim is not blocked).
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _insert_closed_wave_with_plan(
        state, wave_id="P01-I01-W01", iter_id="P01-I01", planned=2, delivered=2
    )
    _insert_closed_wave_with_plan(
        state, wave_id="P01-I01-W02", iter_id="P01-I01", planned=4, delivered=1
    )
    _insert_closed_wave_with_plan(
        state, wave_id="P01-I01-W03", iter_id="P01-I01", planned=2, delivered=2
    )
    it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED


def test_edit_iter_plan_planned() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="orig")
    it = edit_iter_plan(state, iter_id="P01-I01", title="renamed")
    assert it.title == "renamed"
    assert state.iters["P01-I01"].title == "renamed"
    assert state.iters["P01-I01"].status == IterStatus.PLANNED


def test_edit_iter_plan_closed_iter_status_agnostic() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="orig")
    close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    it = edit_iter_plan(state, iter_id="P01-I01", title="normalised")
    assert it.title == "normalised"
    assert state.iters["P01-I01"].status == IterStatus.CLOSED


def test_edit_iter_plan_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown iter"):
        edit_iter_plan(state, iter_id="P01-I01", title="x")


def test_edit_iter_plan_over_cap_title_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="orig")
    with pytest.raises(ValidationError):
        edit_iter_plan(state, iter_id="P01-I01", title="z" * 73)
    assert state.iters["P01-I01"].title == "orig"


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
            effort_bucket="M",
            intent=make_intent(),
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
            effort_bucket="M",
            intent=make_intent(),
        )


def test_plan_wave_duplicate_raises() -> None:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="already exists"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="x",
            file_scopes=["src/"],
            effort_bucket="M",
            intent=make_intent(),
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
            effort_bucket="M",
            intent=make_intent(),
        )


def test_plan_wave_happy_appends_to_iter() -> None:
    state = _seed_wave_state()
    w = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
        effort_bucket="M",
        intent=make_intent(),
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_phase(state, phase_id="P02", title="b", depends_on=["P01"])
    plan_iter(state, iter_id="P02-I01", phase_id="P02", title="i")
    plan_wave(
        state,
        wave_id="P02-I01-W01",
        iter_id="P02-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="blocked on un-closed dep phases"):
        activate_phase(state, phase_id="P02")


def test_activate_phase_happy_sets_current() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["a"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["b"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["a"],
        effort_bucket="M",
        intent=make_intent(),
    )
    archive_phase(state, phase_id="P01")
    assert state.iters["P01-I01"].status == IterStatus.ABANDONED
    assert state.iters["P01-I01"].closed_at is not None


def test_archive_phase_leaves_no_pending_waves_validates() -> None:
    """After cascade, the candidate state passes closure-timestamp invariants."""
    from eawf.kernel.validate.strict import validate_state

    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["a"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    _add_ship_gate_audit(state, audit_id="AUD-2", scope_id="P01")
    close_phase(state, phase_id="P01", audit_id="AUD-2")
    with pytest.raises(LifecycleError, match="not open"):
        plan_iter(state, iter_id="P01-I02", phase_id="P01", title="i2")


def test_activate_iter_sets_current() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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


# ---- Iter concurrency guard + close-time repoint (LC-6) ---------------------


def test_open_iter_rejects_second_active_iter() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="first")
    with pytest.raises(ValueError, match="already has an active iter"):
        open_iter(state, iter_id="P01-I02", phase_id="P01", title="second")


def test_open_iter_allows_second_active_iter_with_flag() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="first")
    it = open_iter(state, iter_id="P01-I02", phase_id="P01", title="second", allow_concurrent=True)
    assert it.status == IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I02"


def _plan_two_iters_one_wave(state: State) -> None:
    """Seed P01 with two PLANNED iters + one wave so activate_phase can fire."""
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i1")
    plan_iter(state, iter_id="P01-I02", phase_id="P01", title="i2")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    activate_phase(state, phase_id="P01")


def test_activate_iter_rejects_second_active_iter() -> None:
    state = _empty_state()
    _plan_two_iters_one_wave(state)
    activate_iter(state, iter_id="P01-I01")
    with pytest.raises(ValueError, match="already has an active iter"):
        activate_iter(state, iter_id="P01-I02")


def test_activate_iter_allows_second_active_iter_with_flag() -> None:
    state = _empty_state()
    _plan_two_iters_one_wave(state)
    activate_iter(state, iter_id="P01-I01")
    it = activate_iter(state, iter_id="P01-I02", allow_concurrent=True)
    assert it.status == IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I02"


def test_close_iter_repoints_current_to_lowest_active_sibling() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="i1")
    open_iter(state, iter_id="P01-I02", phase_id="P01", title="i2", allow_concurrent=True)
    open_iter(state, iter_id="P01-I03", phase_id="P01", title="i3", allow_concurrent=True)
    assert state.current.iter_id == "P01-I03"
    close_iter(state, iter_id="P01-I03", audit_id="AUD-1")
    # Repoints to the lowest-numbered remaining ACTIVE sibling, not to null.
    assert state.current.iter_id == "P01-I01"
    assert state.iters["P01-I02"].status == IterStatus.ACTIVE


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
        effort_bucket="M",
        intent=make_intent(),
    )
    criterion = grandfather_criterion("legacy criterion text", index=1)
    w = edit_wave_plan(
        state,
        wave_id="P01-I01-W01",
        title="updated",
        file_scopes=["src/y/"],
        success_criteria=[criterion],
        criteria_floor_waiver=_floor_waiver(),
    )
    assert w.title == "updated"
    assert w.file_scopes == ["src/y/"]
    assert w.success_criteria == [criterion]


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
        effort_bucket="M",
        intent=make_intent(),
    )
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    with pytest.raises(LifecycleError, match="not pending"):
        edit_wave_plan(state, wave_id="P01-I01-W01", title="late")


# ---- EAWF021 measurability binding at the wave-plan transition --------------


def _measurable_criterion() -> CriterionSpec:
    """Return a well-formed measurable typed criterion (typed response clause)."""
    return CriterionSpec(
        id="CR-01",
        text="returns 200 for a valid request; pytest tests/x.py::test_ok",
        kind="functional",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="exit code 0 on a clean corpus; cli_exit",
        gate_ids=["G-01"],
        response=ResponseClause(observe=ObserveVerb.RETURNS, object="200", locus=ProofLocus.PYTEST),
    )


def _unmeasurable_criterion() -> CriterionSpec:
    """Return an authored criterion with a banned-vague token and no contract."""
    return CriterionSpec(
        id="CR-01",
        text="the widget works properly under all conditions",
        kind="functional",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the widget works properly under all load conditions",
    )


def _floor_waiver() -> CriteriaFloorWaiver:
    """A valid typed waiver for legacy-criterion authoring under the floor."""
    return CriteriaFloorWaiver(
        reason="test fixture: legacy criteria are the subject under test",
        waived_at=datetime(2026, 7, 2, tzinfo=UTC),
    )


def test_plan_wave_rejects_unmeasurable_criterion() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    with pytest.raises(LifecycleError, match="unmeasurable success criteria"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="w",
            file_scopes=["x"],
            effort_bucket="M",
            success_criteria=[_unmeasurable_criterion()],
            intent=make_intent(),
        )
    # The wave is rejected before insertion, leaving state untouched.
    assert "P01-I01-W01" not in state.waves


def test_plan_wave_inserts_measurable_criterion() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    w = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        success_criteria=[_measurable_criterion()],
        intent=make_intent(),
    )
    assert w.success_criteria[0].id == "CR-01"


def test_plan_wave_inserts_grandfathered_legacy_criterion_with_waiver() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    legacy = grandfather_criterion("ship the thing", index=1)
    # The typed-criteria floor rejects a bare legacy set at author time ...
    with pytest.raises(LifecycleError, match="typed-criteria floor"):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="w",
            file_scopes=["x"],
            effort_bucket="M",
            success_criteria=[legacy],
            intent=make_intent(),
        )
    # ... and a typed waiver lands it with the bypass visible on the row.
    w = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        success_criteria=[legacy],
        intent=make_intent(),
        criteria_floor_waiver=_floor_waiver(),
    )
    assert w.success_criteria[0].kind == "legacy"
    assert w.criteria_floor_waiver is not None


def test_edit_wave_plan_rejects_unmeasurable_criterion() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    _plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="orig",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="unmeasurable success criteria"):
        edit_wave_plan(
            state,
            wave_id="P01-I01-W01",
            success_criteria=[_unmeasurable_criterion()],
        )
    # A rejected edit leaves the wave's prior plan untouched (no criteria set).
    assert state.waves["P01-I01-W01"].success_criteria == []


def test_edit_wave_plan_accepts_grandfathered_legacy_criterion_with_waiver() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="orig",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    legacy = grandfather_criterion("ship the thing", index=1)
    with pytest.raises(LifecycleError, match="typed-criteria floor"):
        edit_wave_plan(state, wave_id="P01-I01-W01", success_criteria=[legacy])
    w = edit_wave_plan(
        state,
        wave_id="P01-I01-W01",
        success_criteria=[legacy],
        criteria_floor_waiver=_floor_waiver(),
    )
    assert w.success_criteria[0].kind == "legacy"


def test_remove_wave_plan_deletes_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    remove_wave_plan(state, wave_id="P01-I01-W01")
    assert "P01-I01-W01" not in state.waves
    assert state.iters["P01-I01"].wave_ids == []


def test_remove_wave_plan_blocked_by_blocks_list_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="blocks other waves"):
        remove_wave_plan(state, wave_id="P01-I01-W01")


def test_set_wave_deps_updates_blocks_index() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W03",
        iter_id="P01-I01",
        title="w3",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    set_wave_deps(state, wave_id="P01-I01-W03", deps=["P01-I01-W01", "P01-I01-W02"])
    assert state.waves["P01-I01-W03"].deps == ["P01-I01-W01", "P01-I01-W02"]
    assert state.waves["P01-I01-W01"].blocks == ["P01-I01-W03"]
    assert state.waves["P01-I01-W02"].blocks == ["P01-I01-W03"]
    set_wave_deps(state, wave_id="P01-I01-W03", deps=["P01-I01-W01"])
    assert state.waves["P01-I01-W02"].blocks == []


def test_set_wave_deps_prunes_removed_edge_metadata_and_readd_is_strict() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        intent=make_intent(),
    )
    edge_key = wave_dependency_key("P01-I01-W02", "P01-I01-W01")
    _add_dependency_metadata(
        state,
        wave_id="P01-I01-W02",
        dep_wave_id="P01-I01-W01",
    )

    set_wave_deps(state, wave_id="P01-I01-W02", deps=[])
    set_wave_deps(state, wave_id="P01-I01-W02", deps=["P01-I01-W01"])

    assert edge_key not in state.wave_dependency_barriers
    assert edge_key not in state.wave_dependency_bindings
    assert wave_dependency_stages(
        state.wave_dependency_barriers,
        wave_id="P01-I01-W02",
        dep_wave_id="P01-I01-W01",
    ) == (DependencyStage.CLOSED, DependencyStage.CLOSED)


def test_set_wave_deps_preserves_kept_edge_metadata() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    for wave_id in ("P01-I01-W01", "P01-I01-W02"):
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P01-I01",
            title=wave_id,
            file_scopes=["x"],
            effort_bucket="M",
            intent=make_intent(),
        )
    plan_wave(
        state,
        wave_id="P01-I01-W03",
        iter_id="P01-I01",
        title="w3",
        file_scopes=["x"],
        deps=["P01-I01-W01", "P01-I01-W02"],
        effort_bucket="M",
        intent=make_intent(),
    )
    kept_key = wave_dependency_key("P01-I01-W03", "P01-I01-W01")
    removed_key = wave_dependency_key("P01-I01-W03", "P01-I01-W02")
    kept_barrier, kept_binding = _add_dependency_metadata(
        state,
        wave_id="P01-I01-W03",
        dep_wave_id="P01-I01-W01",
    )
    _add_dependency_metadata(
        state,
        wave_id="P01-I01-W03",
        dep_wave_id="P01-I01-W02",
    )

    set_wave_deps(state, wave_id="P01-I01-W03", deps=["P01-I01-W01"])

    assert state.wave_dependency_barriers[kept_key] is kept_barrier
    assert state.wave_dependency_bindings[kept_key] is kept_binding
    assert removed_key not in state.wave_dependency_barriers
    assert removed_key not in state.wave_dependency_bindings


def test_remove_wave_plan_prunes_dependency_metadata_in_both_directions() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        intent=make_intent(),
    )
    downstream_key = wave_dependency_key("P01-I01-W02", "P01-I01-W01")
    upstream_key = wave_dependency_key("P01-I01-W01", "P01-I01-W02")
    _add_dependency_metadata(
        state,
        wave_id="P01-I01-W02",
        dep_wave_id="P01-I01-W01",
    )
    _add_dependency_metadata(
        state,
        wave_id="P01-I01-W01",
        dep_wave_id="P01-I01-W02",
    )

    remove_wave_plan(state, wave_id="P01-I01-W02")

    assert downstream_key not in state.wave_dependency_barriers
    assert upstream_key not in state.wave_dependency_barriers
    assert downstream_key not in state.wave_dependency_bindings
    assert upstream_key not in state.wave_dependency_bindings


def test_set_wave_deps_cycle_rolled_back() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="cycle"):
        set_wave_deps(state, wave_id="P01-I01-W01", deps=["P01-I01-W02"])
    assert state.waves["P01-I01-W01"].deps == []


def test_claim_wave_rejects_unmet_deps() -> None:
    """P19-W02: claim is blocked when any dep wave is not CLOSED."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(
        state,
        wave_id="P20-I01-W01",
        iter_id="P20-I01",
        title="a",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P20-I01-W02",
        iter_id="P20-I01",
        title="b",
        file_scopes=["x"],
        deps=["P20-I01-W01"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="dependency start barriers"):
        claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2")


def test_claim_wave_rejects_skipping_lower_w_sibling() -> None:
    """P19-W02: skipping a lower-W## ready sibling is rejected."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(
        state,
        wave_id="P20-I01-W01",
        iter_id="P20-I01",
        title="a",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P20-I01-W02",
        iter_id="P20-I01",
        title="b",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="lower-numbered ready siblings"):
        claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2")


def test_claim_wave_out_of_order_overrides_monotonic_gate() -> None:
    """P19-W02: --out-of-order bypasses the monotonic gate."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(
        state,
        wave_id="P20-I01-W01",
        iter_id="P20-I01",
        title="a",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P20-I01-W02",
        iter_id="P20-I01",
        title="b",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    w = claim_wave(state, wave_id="P20-I01-W02", session_id="SES-2", out_of_order=True)
    assert w.status == WaveStatus.CLAIMED


def test_claim_wave_rejected_when_dispatch_paused() -> None:
    """P29-I09-W05: a paused dispatch blocks the claim (cooperative gate)."""
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )
    state.dispatch_paused = True
    with pytest.raises(LifecycleError, match="dispatch paused: resume before claiming"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # The rejected claim leaves the wave PENDING and unbound.
    assert state.waves["P01-I01-W01"].status == WaveStatus.PENDING
    assert state.waves["P01-I01-W01"].claim_session_id is None


def test_claim_wave_succeeds_when_not_dispatch_paused() -> None:
    """The pause gate is inert when ``dispatch_paused`` is ``False`` (the default)."""
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )
    assert state.dispatch_paused is False
    w = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert w.status == WaveStatus.CLAIMED


def test_claim_wave_pause_gate_blocks_even_out_of_order() -> None:
    """The pause gate is unconditional: ``out_of_order=True`` does not bypass it.

    ``out_of_order`` opts out of the lower-W## sibling-ordering gate, not the
    deliberate operator pause, so a claim under a paused dispatch is rejected
    even with the override set.
    """
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )
    state.dispatch_paused = True
    with pytest.raises(LifecycleError, match="dispatch paused: resume before claiming"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1", out_of_order=True)
    assert state.waves["P01-I01-W01"].status == WaveStatus.PENDING


def test_claim_wave_monotonic_gate_allows_after_w01_claimed() -> None:
    """W02 may be claimed once W01 is CLAIMED/IN_PROGRESS/CLOSED."""
    state = _empty_state()
    open_phase(state, phase_id="P20", title="t")
    open_iter(state, iter_id="P20-I01", phase_id="P20", title="i")
    plan_wave(
        state,
        wave_id="P20-I01-W01",
        iter_id="P20-I01",
        title="a",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P20-I01-W02",
        iter_id="P20-I01",
        title="b",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P10-I01-W01",
        iter_id="P10-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    # iter and wave are still open — fail on open-children first
    with pytest.raises(LifecycleError, match="open iters"):
        close_phase(state, phase_id="P10", audit_id="AUD-1")
    # close the iter without closing the wave (abandon path)
    state.waves["P10-I01-W01"].status = WaveStatus.ABANDONED
    state.iters["P10-I01"].status = IterStatus.CLOSED
    state.iters["P10-I01"].closed_at = datetime.now(UTC)
    _add_ship_gate_audit(state, audit_id="AUD-1", scope_id="P10")
    with pytest.raises(LifecycleError, match="no closed waves"):
        close_phase(state, phase_id="P10", audit_id="AUD-1")


def test_close_phase_accepts_when_one_wave_closed() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P11", title="t")
    open_iter(state, iter_id="P11-I01", phase_id="P11", title="i")
    plan_wave(
        state,
        wave_id="P11-I01-W01",
        iter_id="P11-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    _add_ship_gate_audit(state, audit_id="AUD-2", scope_id="P11")
    p = close_phase(state, phase_id="P11", audit_id="AUD-2")
    assert p.status == PhaseStatus.CLOSED


def test_close_phase_rejects_missing_close_audit_evidence() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P11", title="t")
    open_iter(state, iter_id="P11-I01", phase_id="P11", title="i")
    for n in (1, 2):
        wave_id = f"P11-I01-W0{n}"
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P11-I01",
            title="w",
            file_scopes=["x"],
            effort_bucket="M",
            intent=make_intent(),
        )
        claim_wave(state, wave_id=wave_id, session_id=f"SES-{n}")
        close_wave(state, wave_id=wave_id, outcome="ok")
    state.iters["P11-I01"].status = IterStatus.CLOSED
    state.iters["P11-I01"].closed_at = datetime.now(UTC)
    state.iters["P11-I01"].audit_id = "AUD-iter"
    with pytest.raises(LifecycleError, match="close audit 'AUD-2' not found"):
        close_phase(state, phase_id="P11", audit_id="AUD-2")


def test_close_phase_rejects_closed_iter_missing_audit() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P12", title="t")
    open_iter(state, iter_id="P12-I01", phase_id="P12", title="i")
    # Two closed waves so the single-wave gate cannot mask the audit gate.
    for n in (1, 2):
        wave_id = f"P12-I01-W0{n}"
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P12-I01",
            title="w",
            file_scopes=["x"],
            effort_bucket="M",
            intent=make_intent(),
        )
        claim_wave(state, wave_id=wave_id, session_id=f"SES-{n}")
        close_wave(state, wave_id=wave_id, outcome="ok")
    # Close the iter WITHOUT an audit_id — the transition must reject.
    state.iters["P12-I01"].status = IterStatus.CLOSED
    state.iters["P12-I01"].closed_at = datetime.now(UTC)
    _add_ship_gate_audit(state, audit_id="AUD-1", scope_id="P12")
    with pytest.raises(LifecycleError, match="closed iters missing audit"):
        close_phase(state, phase_id="P12", audit_id="AUD-1")


def test_close_phase_rejects_single_wave_without_decision() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P13", title="t")
    open_iter(state, iter_id="P13-I01", phase_id="P13", title="i")
    plan_wave(
        state,
        wave_id="P13-I01-W01",
        iter_id="P13-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    claim_wave(state, wave_id="P13-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P13-I01-W01", outcome="ok")
    state.iters["P13-I01"].status = IterStatus.CLOSED
    state.iters["P13-I01"].closed_at = datetime.now(UTC)
    state.iters["P13-I01"].audit_id = "AUD-iter"
    _add_ship_gate_audit(state, audit_id="AUD-1", scope_id="P13")
    # Single closed wave, no scope-collapse decision — the transition rejects.
    with pytest.raises(LifecycleError, match="single closed wave"):
        close_phase(state, phase_id="P13", audit_id="AUD-1")


def test_close_phase_allows_single_wave_with_scope_collapse_decision() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P14", title="t")
    open_iter(state, iter_id="P14-I01", phase_id="P14", title="i")
    plan_wave(
        state,
        wave_id="P14-I01-W01",
        iter_id="P14-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    _add_ship_gate_audit(state, audit_id="AUD-1", scope_id="P14")
    p = close_phase(state, phase_id="P14", audit_id="AUD-1")
    assert p.status == PhaseStatus.CLOSED


def test_close_phase_rejects_single_wave_when_decision_superseded() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P15", title="t")
    open_iter(state, iter_id="P15-I01", phase_id="P15", title="i")
    plan_wave(
        state,
        wave_id="P15-I01-W01",
        iter_id="P15-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    _add_ship_gate_audit(state, audit_id="AUD-1", scope_id="P15")
    with pytest.raises(LifecycleError, match="single closed wave"):
        close_phase(state, phase_id="P15", audit_id="AUD-1")


def test_set_wave_deps_non_pending_rejected() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    w = edit_wave_plan(state, wave_id="P01-I01-W01", title="revised mid-flight")
    assert w.title == "revised mid-flight"
    assert state.phases["P01"].status == PhaseStatus.ACTIVE


def test_remove_wave_plan_under_active_phase_ok_when_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    remove_wave_plan(state, wave_id="P01-I01-W02")
    assert "P01-I01-W02" not in state.waves
    assert state.phases["P01"].status == PhaseStatus.ACTIVE


def test_set_wave_deps_under_active_phase_ok_when_pending() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w1",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="w2",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
    plan_wave(
        state,
        wave_id="P20-I01-W01",
        iter_id="P20-I01",
        title="a",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P20-I01-W02",
        iter_id="P20-I01",
        title="b",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
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
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(LifecycleError, match="dependency start barriers"):
        claim_wave(state, wave_id="P20-I01-W03", session_id="SES-3", out_of_order=True)
    assert state.iters["P20-I01"].status == IterStatus.PLANNED
    assert state.current.iter_id is None


def test_claim_wave_under_terminal_iter_rejected_and_not_activated() -> None:
    """A wave under a CLOSED iter cannot be claimed and the iter is untouched.

    Parent lifecycle validation precedes wave-status validation, so the
    terminal-parent guard is the stable rejection surface.
    """
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")
    close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert state.iters["P01-I01"].status == IterStatus.CLOSED
    with pytest.raises(LifecycleError, match="claim_parent_iter_terminal"):
        claim_wave(state, wave_id="P01-I01-W01", session_id="SES-2")
    assert state.iters["P01-I01"].status == IterStatus.CLOSED


# ---- Description round-trip (P28-W02) ---------------------------------------
#
# Description is an existing model field (≤500 char bound enforced in
# Pydantic) that lifecycle transitions historically did not surface. These
# tests pin the wire from transition arg -> stored field -> read back.


def test_plan_phase_persists_description() -> None:
    state = _empty_state()
    phase = plan_phase(
        state,
        phase_id="P01",
        title="t",
        description="long-form rationale for the phase",
    )
    assert phase.description == "long-form rationale for the phase"
    assert state.phases["P01"].description == "long-form rationale for the phase"


def test_plan_phase_description_none_default() -> None:
    state = _empty_state()
    phase = plan_phase(state, phase_id="P01", title="t")
    assert phase.description is None


def test_plan_phase_description_over_cap_raises() -> None:
    state = _empty_state()
    with pytest.raises(ValidationError):
        plan_phase(state, phase_id="P01", title="t", description="z" * 501)


def test_open_phase_persists_description() -> None:
    state = _empty_state()
    phase = open_phase(state, phase_id="P01", title="t", description="why this phase exists")
    assert phase.description == "why this phase exists"
    assert state.phases["P01"].description == "why this phase exists"


def test_plan_iter_persists_description() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    it = plan_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="i",
        description="iter scope summary",
    )
    assert it.description == "iter scope summary"
    assert state.iters["P01-I01"].description == "iter scope summary"


def test_plan_iter_description_over_cap_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    with pytest.raises(ValidationError):
        plan_iter(
            state,
            iter_id="P01-I01",
            phase_id="P01",
            title="i",
            description="z" * 501,
        )


def test_open_iter_persists_description() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="t")
    it = open_iter(
        state,
        iter_id="P01-I01",
        phase_id="P01",
        title="i",
        description="active iter narrative",
    )
    assert it.description == "active iter narrative"


def test_plan_wave_persists_description() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    w = plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        description="wave goal narrative",
        effort_bucket="M",
        intent=make_intent(),
    )
    assert w.description == "wave goal narrative"
    assert state.waves["P01-I01-W01"].description == "wave goal narrative"


def test_plan_wave_description_over_cap_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    with pytest.raises(ValidationError):
        plan_wave(
            state,
            wave_id="P01-I01-W01",
            iter_id="P01-I01",
            title="w",
            file_scopes=["src/"],
            description="z" * 501,
            effort_bucket="M",
            intent=make_intent(),
        )


def test_edit_iter_plan_persists_description() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    it = edit_iter_plan(state, iter_id="P01-I01", description="new scope summary")
    assert it.description == "new scope summary"
    # Title untouched when omitted.
    assert it.title == "i"


def test_edit_iter_plan_description_only_keeps_title() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="orig")
    edit_iter_plan(state, iter_id="P01-I01", description="annotated scope note")
    assert state.iters["P01-I01"].title == "orig"
    assert state.iters["P01-I01"].description == "annotated scope note"


def test_edit_iter_plan_title_only_keeps_description() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(
        state, iter_id="P01-I01", phase_id="P01", title="orig", description="kept description"
    )
    edit_iter_plan(state, iter_id="P01-I01", title="renamed")
    assert state.iters["P01-I01"].title == "renamed"
    assert state.iters["P01-I01"].description == "kept description"


def test_edit_iter_plan_description_over_cap_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    with pytest.raises(ValidationError):
        edit_iter_plan(state, iter_id="P01-I01", description="z" * 501)


def test_edit_wave_plan_persists_description() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="orig",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    w = edit_wave_plan(
        state,
        wave_id="P01-I01-W01",
        description="why this wave matters",
    )
    assert w.description == "why this wave matters"
    # Title and file_scopes untouched when omitted.
    assert w.title == "orig"
    assert w.file_scopes == ["x"]


def test_edit_wave_plan_description_over_cap_raises() -> None:
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    with pytest.raises(ValidationError):
        edit_wave_plan(state, wave_id="P01-I01-W01", description="z" * 501)


def test_edit_wave_plan_description_non_pending_rejected() -> None:
    """description edits inherit the PENDING-only guard."""
    state = _empty_state()
    plan_phase(state, phase_id="P01", title="t")
    plan_iter(state, iter_id="P01-I01", phase_id="P01", title="i")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    activate_phase(state, phase_id="P01")
    activate_iter(state, iter_id="P01-I01")
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    with pytest.raises(LifecycleError, match="not pending"):
        edit_wave_plan(state, wave_id="P01-I01-W01", description="late annotation")
