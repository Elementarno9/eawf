"""Lifecycle tests for ``edit_phase_plan`` and ``release_wave``.

``edit_phase_plan`` is the phase-level metadata editor mirroring
:func:`edit_iter_plan`; it edits a PLANNED/ACTIVE phase's title /
description / release / intent and rejects terminal (CLOSED / ARCHIVED)
phases. The ``release`` band (W02) accepts a ``vMAJOR.MINOR.PATCH``
version and routes it through the model assignment validator.
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
from eawf.kernel.state.models import CurrentPointers, Phase, Project, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.iter_ import open_iter
from eawf.workflow.lifecycle.phase import edit_phase_plan, open_phase
from eawf.workflow.lifecycle.wave import plan_wave, release_wave
from tests._session_helpers import claim_wave_with_session as claim_wave
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
        intent=make_intent(),
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


def test_edit_phase_plan_sets_release_version() -> None:
    state = _seed_phase_state()
    phase = edit_phase_plan(state, phase_id="P01", release="v0.5.0")
    assert phase.release == "v0.5.0"
    assert state.phases["P01"].release == "v0.5.0"
    # release-only edit leaves the title untouched.
    assert state.phases["P01"].title == "orig title"


def test_edit_phase_plan_release_accepts_prerelease() -> None:
    state = _seed_phase_state()
    phase = edit_phase_plan(state, phase_id="P01", release="v0.5.0rc1")
    assert phase.release == "v0.5.0rc1"


def test_edit_phase_plan_release_invalid_pattern_raises() -> None:
    state = _seed_phase_state()
    with pytest.raises(ValidationError):
        edit_phase_plan(state, phase_id="P01", release="0.5.0")
    # The rejected assignment must not mutate the phase.
    assert state.phases["P01"].release is None


def test_edit_phase_plan_release_garbage_raises() -> None:
    state = _seed_phase_state()
    with pytest.raises(ValidationError):
        edit_phase_plan(state, phase_id="P01", release="v1.2")


def test_edit_phase_plan_release_none_is_noop() -> None:
    state = _seed_phase_state()
    edit_phase_plan(state, phase_id="P01", release="v0.5.0")
    # Passing release=None leaves the prior value untouched (no-clear).
    edit_phase_plan(state, phase_id="P01", title="retitled")
    assert state.phases["P01"].release == "v0.5.0"


def test_edit_phase_plan_release_on_closed_phase_rejected() -> None:
    state = _seed_phase_state()
    state.phases["P01"].status = PhaseStatus.CLOSED
    with pytest.raises(LifecycleError, match="only planned or active"):
        edit_phase_plan(state, phase_id="P01", release="v0.5.0")


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


# ---- Phase.release field contract (W02) ------------------------------------


def _phase_payload(**overrides: object) -> dict[str, object]:
    """Return a minimal valid Phase payload dict for round-trip tests."""
    base: dict[str, object] = {
        "id": "P01",
        "scope_id": "QR",
        "title": "orig title",
        "status": PhaseStatus.PLANNED.value,
        "opened_at": "2026-05-08T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_phase_release_defaults_to_none() -> None:
    phase = Phase.model_validate(_phase_payload())
    assert phase.release is None


def test_phase_release_accepts_valid_version() -> None:
    phase = Phase.model_validate(_phase_payload(release="v0.5.0"))
    assert phase.release == "v0.5.0"


def test_phase_release_accepts_prerelease_segment() -> None:
    phase = Phase.model_validate(_phase_payload(release="v1.2.3rc4"))
    assert phase.release == "v1.2.3rc4"


def test_phase_release_rejects_missing_v_prefix() -> None:
    with pytest.raises(ValidationError):
        Phase.model_validate(_phase_payload(release="0.5.0"))


def test_phase_release_rejects_two_segment_version() -> None:
    with pytest.raises(ValidationError):
        Phase.model_validate(_phase_payload(release="v1.2"))


def test_phase_release_none_round_trips() -> None:
    phase = Phase.model_validate(_phase_payload())
    reloaded = Phase.model_validate(phase.model_dump(mode="json"))
    assert reloaded.release is None


def test_phase_release_version_round_trips() -> None:
    phase = Phase.model_validate(_phase_payload(release="v0.5.0"))
    reloaded = Phase.model_validate(phase.model_dump(mode="json"))
    assert reloaded.release == "v0.5.0"


def test_phase_old_shape_without_release_loads() -> None:
    """A pre-W02 phase payload (no ``release`` key) stays valid under extra=forbid."""
    payload = _phase_payload()
    assert "release" not in payload
    phase = Phase.model_validate(payload)
    assert phase.release is None
