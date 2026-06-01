"""Lifecycle tests for ``edit_phase_plan`` and ``release_wave`` (P29-I02-W01).

``edit_phase_plan`` is the phase-level metadata editor mirroring
:func:`edit_iter_plan`; it edits a PLANNED/ACTIVE phase's title /
description / intent and rejects terminal (CLOSED / ARCHIVED) phases.
``release_wave`` is the inverse of ``claim_wave`` -- it un-claims a
claimed/in-progress wave back to PENDING. Both helpers mutate the
supplied :class:`State` in place.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    EffortBucket,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.iter_ import open_iter
from eawf.workflow.lifecycle.phase import edit_phase_plan, open_phase
from eawf.workflow.lifecycle.wave import claim_wave, plan_wave, release_wave


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


def _seed_phase_state() -> State:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="orig title")
    return state


def _seed_wave_state() -> State:
    state = _seed_phase_state()
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    return state


def _seed_claimed_wave() -> State:
    state = _seed_wave_state()
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=EffortBucket.M,
    )
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    return state


# ---- edit_phase_plan -------------------------------------------------------


def test_edit_phase_plan_unknown_raises() -> None:
    state = _empty_state()
    with pytest.raises(LifecycleError, match="unknown phase"):
        edit_phase_plan(state, phase_id="P99", title="x")


def test_edit_phase_plan_happy_rewrites_title() -> None:
    state = _seed_phase_state()
    phase = edit_phase_plan(state, phase_id="P01", title="new title")
    assert phase.title == "new title"
    assert state.phases["P01"].title == "new title"


def test_edit_phase_plan_description_only_leaves_title() -> None:
    state = _seed_phase_state()
    edit_phase_plan(state, phase_id="P01", description="long narrative")
    assert state.phases["P01"].title == "orig title"
    assert state.phases["P01"].description == "long narrative"


def test_edit_phase_plan_none_fields_are_noop() -> None:
    state = _seed_phase_state()
    edit_phase_plan(state, phase_id="P01")
    assert state.phases["P01"].title == "orig title"
    assert state.phases["P01"].description is None


def test_edit_phase_plan_active_phase_editable() -> None:
    state = _seed_phase_state()
    state.phases["P01"].status = PhaseStatus.ACTIVE
    edit_phase_plan(state, phase_id="P01", title="active retitle")
    assert state.phases["P01"].title == "active retitle"


def test_edit_phase_plan_closed_phase_rejected() -> None:
    state = _seed_phase_state()
    state.phases["P01"].status = PhaseStatus.CLOSED
    with pytest.raises(LifecycleError, match="only planned or active"):
        edit_phase_plan(state, phase_id="P01", title="nope")
    # Status must not change on rejection.
    assert state.phases["P01"].title == "orig title"


def test_edit_phase_plan_archived_phase_rejected() -> None:
    state = _seed_phase_state()
    state.phases["P01"].status = PhaseStatus.ARCHIVED
    with pytest.raises(LifecycleError, match="only planned or active"):
        edit_phase_plan(state, phase_id="P01", title="nope")


def test_edit_phase_plan_over_cap_title_raises() -> None:
    state = _seed_phase_state()
    with pytest.raises(ValidationError):
        edit_phase_plan(state, phase_id="P01", title="x" * 73)


def test_edit_phase_plan_over_cap_description_raises() -> None:
    state = _seed_phase_state()
    with pytest.raises(ValidationError):
        edit_phase_plan(state, phase_id="P01", description="d" * 501)


# ---- release_wave ----------------------------------------------------------


def test_release_wave_unknown_raises() -> None:
    state = _seed_wave_state()
    with pytest.raises(LifecycleError, match="unknown wave"):
        release_wave(state, wave_id="P01-I01-W01")


def test_release_wave_happy_returns_to_pending() -> None:
    state = _seed_claimed_wave()
    assert state.waves["P01-I01-W01"].status == WaveStatus.CLAIMED
    w = release_wave(state, wave_id="P01-I01-W01", reason="cannot finish")
    assert w.status == WaveStatus.PENDING
    assert w.claim_session_id is None
    assert w.worktree_id is None
    assert "P01-I01-W01" not in state.current.active_wave_ids


def test_release_wave_clears_in_progress() -> None:
    state = _seed_claimed_wave()
    state.waves["P01-I01-W01"].status = WaveStatus.IN_PROGRESS
    w = release_wave(state, wave_id="P01-I01-W01")
    assert w.status == WaveStatus.PENDING


def test_release_wave_idempotent_when_already_pending() -> None:
    state = _seed_claimed_wave()
    release_wave(state, wave_id="P01-I01-W01")
    # Second release: no-op, no error.
    w = release_wave(state, wave_id="P01-I01-W01")
    assert w.status == WaveStatus.PENDING


def test_release_wave_closed_rejected() -> None:
    state = _seed_claimed_wave()
    state.waves["P01-I01-W01"].status = WaveStatus.CLOSED
    with pytest.raises(LifecycleError, match="cannot release"):
        release_wave(state, wave_id="P01-I01-W01")


def test_release_wave_failed_rejected() -> None:
    state = _seed_claimed_wave()
    state.waves["P01-I01-W01"].status = WaveStatus.FAILED
    with pytest.raises(LifecycleError, match="cannot release"):
        release_wave(state, wave_id="P01-I01-W01")
