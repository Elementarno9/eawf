"""Unit tests for ``eawf phase prepare-close`` checklist computation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.cli.commands.lifecycle import _phase_prepare_close_checklist
from eawf.lifecycle.transitions import (
    LifecycleError,
    open_iter,
    open_phase,
    plan_wave,
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


def test_prepare_close_unknown_phase_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase"):
        _phase_prepare_close_checklist(state, phase_id="P99")


def test_prepare_close_empty_phase_ok() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    out = _phase_prepare_close_checklist(state, phase_id="P03")
    assert out["ok"] is True
    assert out["open_iters"] == []
    assert out["open_waves"] == []
    assert out["closed_waves_missing_commit"] == []
    assert out["iters_without_audit"] == []
    assert out["phase_status"] == PhaseStatus.ACTIVE.value


def test_prepare_close_flags_open_iter() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    out = _phase_prepare_close_checklist(state, phase_id="P03")
    assert out["ok"] is False
    assert out["open_iters"] == ["P03-I01"]


def test_prepare_close_flags_open_wave() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(state, wave_id="P03-I01-W01", iter_id="P03-I01", title="w", file_scopes=["x"])
    out = _phase_prepare_close_checklist(state, phase_id="P03")
    assert out["ok"] is False
    assert out["open_waves"] == ["P03-I01-W01"]


def test_prepare_close_flags_closed_iter_missing_audit() -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    # Cheat: close the iter manually (transition path requires audit_id).
    it = state.iters["P03-I01"]
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    out = _phase_prepare_close_checklist(state, phase_id="P03")
    assert out["iters_without_audit"] == ["P03-I01"]
    assert out["ok"] is False


def test_prepare_close_flags_closed_wave_missing_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eawf.lifecycle.wave_sha.derive_wave_sha",
        lambda _wid: None,
    )
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(state, wave_id="P03-I01-W01", iter_id="P03-I01", title="w", file_scopes=["x"])
    w = state.waves["P03-I01-W01"]
    w.status = WaveStatus.CLOSED
    w.closed_at = datetime.now(UTC)
    out = _phase_prepare_close_checklist(state, phase_id="P03")
    assert out["closed_waves_missing_commit"] == ["P03-I01-W01"]
    assert out["ok"] is False


def test_prepare_close_flags_single_wave_without_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eawf.lifecycle.wave_sha.derive_wave_sha",
        lambda _wid: "abc123",
    )
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(state, wave_id="P03-I01-W01", iter_id="P03-I01", title="w", file_scopes=["x"])
    w = state.waves["P03-I01-W01"]
    w.status = WaveStatus.CLOSED
    w.closed_at = datetime.now(UTC)
    it = state.iters["P03-I01"]
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    it.audit_id = "AUD-1"

    out = _phase_prepare_close_checklist(state, phase_id="P03")

    assert out["closed_wave_count"] == 1
    assert out["unique_closed_wave_commit_count"] == 1
    assert out["single_wave_without_decision"] is True
    assert out["scope_collapse_decision"] is False
    assert out["ok"] is False
    assert "single closed wave" in "; ".join(out["blockers"])


def test_prepare_close_allows_single_wave_with_scope_collapse_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eawf.lifecycle.wave_sha.derive_wave_sha",
        lambda _wid: "abc123",
    )
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(state, wave_id="P03-I01-W01", iter_id="P03-I01", title="w", file_scopes=["x"])
    w = state.waves["P03-I01-W01"]
    w.status = WaveStatus.CLOSED
    w.closed_at = datetime.now(UTC)
    it = state.iters["P03-I01"]
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    it.audit_id = "AUD-1"
    state.decisions["D-SINGLE"] = Decision(
        id="D-SINGLE",
        scope_id="P03",
        summary="P03 scope collapse: finish as single-wave phase",
        rationale="scope collapse accepted because follow-up work moved to next phase",
        alternatives=["open another wave", "leave phase open"],
        status=DecisionStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )

    out = _phase_prepare_close_checklist(state, phase_id="P03")

    assert out["single_wave_without_decision"] is False
    assert out["scope_collapse_decision"] is True
    assert out["blockers"] == []
    assert out["ok"] is True
