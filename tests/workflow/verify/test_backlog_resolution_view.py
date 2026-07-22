"""Tests for the backlog-resolution close-gate wiring in readiness (P30-I10-W02).

Pins two W02 contracts on the readiness layer:

* ``compute`` surfaces a ``backlog_resolution`` close-gate view for a wave that
  links backlog items, and the view flips to ``fail`` (driving ``ready=False``)
  when a linked item is left dangling -- so ``verify.enforce`` refuses the
  close;
* :func:`wired_audit_dsl_kinds` covers every registered audit-DSL kind, so the
  BIND-1 wired-on sweep exits 0 over the post-W02 tree (no kind ships
  registered-but-idle), including the previously-idle ``tui_flow`` and the new
  ``backlog_resolution``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    BacklogPriority,
    BacklogStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import BacklogItem, CurrentPointers, Project, State
from eawf.platform.profiles.models import VerifyBlock
from eawf.workflow.audit_dsl.registry import registered_audit_dsl_kinds
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.transitions import open_iter, open_phase
from eawf.workflow.lifecycle.wave import plan_wave
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify.readiness import wired_audit_dsl_kinds
from tests._criteria_helpers import legacy_criteria
from tests._session_helpers import claim_wave_with_session as claim_wave
from tests.conftest import make_floor_waiver, make_intent

WAVE_ID = "P30-I10-W02"


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:VFY",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="VFY",
                slug="vfy",
                title="VFY",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:VFY",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="VFY").model_dump(mode="json"),
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


def _seed_wave(state: State) -> None:
    open_phase(state, phase_id="P30", title="phase")
    open_iter(state, iter_id="P30-I10", phase_id="P30", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P30-I10",
        title="wave",
        file_scopes=["src/"],
        success_criteria=legacy_criteria("legacy one"),
        criteria_floor_waiver=make_floor_waiver(),
        effort_bucket="M",
        intent=make_intent(),
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-1")


def _backlog_item(item_id: str, *, status: BacklogStatus, resolution: str | None) -> BacklogItem:
    return BacklogItem(
        id=item_id,
        scope_id=WAVE_ID,
        title=f"item {item_id}",
        priority=BacklogPriority.P3,
        status=status,
        created_at=datetime.now(UTC),
        resolution=resolution,
    )


# ---- compute integration ----------------------------------------------------


def test_compute_surfaces_backlog_view_on_resolved_link(tmp_path: Path) -> None:
    # A wave linked to a closed-with-resolution item surfaces a passing
    # backlog_resolution view and stays ready.
    state = _empty_state()
    _seed_wave(state)
    state.backlog = {"B093": _backlog_item("B093", status=BacklogStatus.CLOSED, resolution="done")}
    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=tmp_path / "store",
        repo_root=tmp_path,
        load_profile_verify=False,
    )
    views = [v for v in result.criteria if v.id == "backlog_resolution"]
    assert len(views) == 1
    assert views[0].source == "floor"
    assert views[0].status == "pass"
    assert result.ready is True


def test_compute_backlog_view_fails_on_dangling_link(tmp_path: Path) -> None:
    # A wave that leaves a linked item dangling surfaces a fail view and flips
    # ready=False.
    state = _empty_state()
    _seed_wave(state)
    state.backlog = {"B093": _backlog_item("B093", status=BacklogStatus.OPEN, resolution=None)}
    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=tmp_path / "store",
        repo_root=tmp_path,
        load_profile_verify=False,
    )
    views = [v for v in result.criteria if v.id == "backlog_resolution"]
    assert len(views) == 1
    assert views[0].status == "fail"
    assert result.ready is False


def test_compute_no_backlog_view_when_wave_links_nothing(tmp_path: Path) -> None:
    # A wave linking no backlog items surfaces no backlog_resolution view, so
    # its readiness is byte-unchanged (only the legacy advisory view remains).
    state = _empty_state()
    _seed_wave(state)
    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=tmp_path / "store",
        repo_root=tmp_path,
        load_profile_verify=False,
    )
    assert [v for v in result.criteria if v.id == "backlog_resolution"] == []
    assert result.ready is True


def test_enforce_refuses_close_on_dangling_backlog(tmp_path: Path, monkeypatch) -> None:
    # verify.enforce drives the backlog gate: a dangling link makes compute
    # raise LifecycleError naming backlog_resolution at the enforcing close seam.
    state = _empty_state()
    _seed_wave(state)
    state.backlog = {"B093": _backlog_item("B093", status=BacklogStatus.OPEN, resolution=None)}
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda *a, **k: VerifyBlock(enforce=True),
    )
    with pytest.raises(LifecycleError) as exc:
        readiness_mod.compute(
            WAVE_ID,
            state=state,
            store_dir=tmp_path / "store",
            repo_root=tmp_path,
        )
    assert "backlog_resolution" in str(exc.value)


# ---- wired-on sweep coverage ------------------------------------------------


def test_wired_kinds_cover_every_registered_kind() -> None:
    # The post-W02 tree: every registered kind is wired (the wired-on sweep is
    # clean). No kind ships registered-but-idle.
    registered = registered_audit_dsl_kinds()
    wired = wired_audit_dsl_kinds()
    assert registered - wired == frozenset()


def test_previously_idle_tui_flow_is_now_wired() -> None:
    # tui_flow was registered-but-idle (no _GATE_KIND_TIER entry); the
    # supplemental tier binding un-idles it.
    assert "tui_flow" in wired_audit_dsl_kinds()


def test_backlog_resolution_is_wired() -> None:
    # The new close-gate kind is wired via CLOSE_GATE_KINDS, not a tier map.
    assert "backlog_resolution" in wired_audit_dsl_kinds()


def test_wired_kinds_is_frozen() -> None:
    assert isinstance(wired_audit_dsl_kinds(), frozenset)
