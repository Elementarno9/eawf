"""Lifecycle tests for the silent ``current.track_id`` stamp on phase open.

``open_phase`` (and ``plan_phase``) copy the active
:attr:`CurrentPointers.track_id` onto the new :attr:`Phase.track_id` at
open time. The behaviour is silent: when no Track is focused
(``current.track_id is None``) the field stays ``None`` and nothing else
about the open path changes. These tests pin the binary criterion --
phase open with a Track active tags the phase, with none active the field
stays ``None`` -- and confirm a pre-existing state.json that predates the
field (the key simply absent) still loads under the live model, which the
1.9 -> 1.10 migration already exercises in ``tests/unit/test_migrate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import (
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    TrackKind,
)
from eawf.kernel.state.models import CurrentPointers, Phase, Project, State
from eawf.workflow.lifecycle.phase import open_phase, plan_phase
from eawf.workflow.lifecycle.project import add_track, switch_track


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


def _state_with_active_track() -> State:
    state = _empty_state()
    add_track(state, code="VOL", kind=TrackKind.STRATEGY, title="Volatility modelling")
    switch_track(state, code="VOL")
    return state


# ---- open_phase stamps the active track ------------------------------------


def test_open_phase_stamps_active_track_id() -> None:
    state = _state_with_active_track()
    assert state.current.track_id == "VOL"
    phase = open_phase(state, phase_id="P01", title="phase one")
    assert phase.track_id == "VOL"
    assert state.phases["P01"].track_id == "VOL"


def test_open_phase_leaves_track_id_none_when_none_active() -> None:
    state = _empty_state()
    assert state.current.track_id is None
    phase = open_phase(state, phase_id="P01", title="phase one")
    assert phase.track_id is None
    assert state.phases["P01"].track_id is None


def test_open_phase_stamp_is_silent_otherwise() -> None:
    # The stamp does not perturb the rest of the open path: status, cursor,
    # and the new phase id all land exactly as the untracked open would.
    state = _state_with_active_track()
    phase = open_phase(state, phase_id="P02", title="phase two")
    assert phase.status == PhaseStatus.ACTIVE
    assert state.current.phase_id == "P02"
    assert phase.track_id == "VOL"


# ---- plan_phase shares the same stamp --------------------------------------


def test_plan_phase_stamps_active_track_id() -> None:
    state = _state_with_active_track()
    phase = plan_phase(state, phase_id="P03", title="planned phase")
    assert phase.status == PhaseStatus.PLANNED
    assert phase.track_id == "VOL"


def test_plan_phase_leaves_track_id_none_when_none_active() -> None:
    state = _empty_state()
    phase = plan_phase(state, phase_id="P03", title="planned phase")
    assert phase.track_id is None


# ---- pre-existing state loads (migration-covered; asserted here) ------------


def test_phase_track_id_defaults_to_none_when_key_absent() -> None:
    # A pre-existing state.json that predates the field carries no ``track_id``
    # key; under ``extra="forbid"`` the missing key takes the ``None`` default
    # rather than failing to load. The full subproject_id -> track_id rename is
    # exercised by the 1.9 -> 1.10 migration in tests/unit/test_migrate.py.
    payload: dict[str, object] = {
        "id": "P00",
        "scope_id": "QR",
        "title": "legacy phase",
        "status": PhaseStatus.ACTIVE.value,
        "opened_at": "2026-05-08T00:00:00Z",
    }
    phase = Phase.model_validate(payload)
    assert phase.track_id is None
    # The round-trip stays stable so the field is durable once stamped.
    reloaded = Phase.model_validate(phase.model_dump(mode="json"))
    assert reloaded.track_id is None
