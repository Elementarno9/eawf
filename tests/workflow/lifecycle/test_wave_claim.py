"""Tests for the ``effort_bucket`` claim-time gate on :func:`claim_wave`.

The wave-lifecycle gate (P28-I02-W16) rejects a claim when a legacy wave
record has no ``effort_bucket``: estimates, variance, and the EU-projection
table all key off that field, so a bucketless wave silently degrades the
dispatch + reporting story. The error message points the operator at the
one-line fix (``eawf roadmap revise --set-bucket``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import (
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.iter_ import open_iter
from eawf.workflow.lifecycle.phase import open_phase
from eawf.workflow.lifecycle.wave import claim_wave, plan_wave


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
    return state


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
    )
    w = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    assert w.status == WaveStatus.CLAIMED
    assert w.claim_session_id == "SES-1"
    assert w.effort_bucket == EffortBucket.XS
    assert "P01-I01-W01" in state.current.active_wave_ids
