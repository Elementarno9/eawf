"""Tests for the Track promotion gate + out-of-scope containment.

Covers the guardian a Track passes before its lifecycle advances and the
containment backstop that flags an out-of-scope wave:

* ``promote_track`` REFUSES a promotion unless every reachable Outcome holds
  ``MET`` over ``MIN_PROMOTION_PERIOD`` (C1 refuse path), and an attested
  ``force_reason`` overrides the refuse with the reason recorded on the gate
  (C1 forced-override-with-reason path).
* ``wave_scope_violations`` flags a wave whose ``file_scopes`` fall outside the
  Track's declared ``scope_globs`` so Track containment is CHECKED, not assumed
  (C2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eawf.kernel.state.enums import OutcomeDirection, OutcomeStatus, TrackStatus
from eawf.kernel.state.models import Goal, Outcome, State, Track, Wave
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.track import (
    MIN_PROMOTION_PERIOD,
    evaluate_track_promotion,
    promote_track,
    wave_scope_violations,
)


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


# --- shared builders ---------------------------------------------------------


def _outcome(
    *,
    outcome_id: str = "OUT-1",
    status: OutcomeStatus = OutcomeStatus.MET,
    updated_at: datetime | None = None,
) -> Outcome:
    """Build a measured Outcome at a chosen ``updated_at``.

    The model invariant forbids a measured outcome (terminal status + sample)
    with no evidence ref, so a measured one always carries an evidence ref.
    """
    measured = status is not OutcomeStatus.PENDING
    return Outcome(
        id=outcome_id,
        scope_id="QR-X",
        metric="sharpe",
        threshold=1.0,
        direction=OutcomeDirection.MAX,
        value=1.5 if measured else None,
        sample=1.5 if measured else None,
        best_value=1.5 if measured else None,
        status=status,
        audit_id="AUD-1" if measured else None,
        evidence_refs=["repo:.ea/x.md"] if measured else [],
        updated_at=updated_at if updated_at is not None else _now(),
    )


def _state_with_outcomes(
    outcomes: list[Outcome],
    *,
    track_id: str = "QR-X",
    scope_globs: list[str] | None = None,
) -> State:
    """Build a valid State carrying a Track -> Goal -> Outcome chain."""
    goal = Goal(
        id="GOAL-1",
        scope_id=track_id,
        title="Beat the sharpe floor",
        summary="The strategy must clear its sharpe threshold on the eval set.",
        status="open",
        outcome_ids=[outcome.id for outcome in outcomes],
        created_at=_now(),
    )
    track = Track(
        id=track_id,
        code="QR",
        slug="x-track",
        title="X Strategy",
        kind="strategy",
        domains=["quant"],
        status="active",
        goal_ids=[goal.id],
        scope_globs=list(scope_globs or []),
    )
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": None,
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
            "track_ids": [track_id],
        },
        "current": {
            "project_code": "QR",
            "track_id": track_id,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "tracks": {track_id: track.model_dump(mode="json")},
        "goals": {goal.id: goal.model_dump(mode="json")},
        "outcomes": {out.id: out.model_dump(mode="json") for out in outcomes},
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _wave(*, file_scopes: list[str]) -> Wave:
    """Build a minimal in-progress Wave with the given file scopes."""
    return Wave(
        id="P01-I01-W01",
        iter_id="P01-I01",
        title="Move the sharpe metric",
        status="in_progress",
        file_scopes=list(file_scopes),
        opened_at=_now(),
    )


# --- C1: promotion gate refuse + forced-override-with-reason ------------------


def test_promote_refused_when_outcomes_not_held_over_period() -> None:
    """A Track whose MET outcome has not held the period is REFUSED.

    The outcome is MET but its ``updated_at`` is now (zero hold), so the
    period gate fails and ``promote_track`` raises rather than advancing the
    Track. The Track status is left unchanged.
    """
    state = _state_with_outcomes([_outcome(status=OutcomeStatus.MET, updated_at=_now())])
    with pytest.raises(LifecycleError, match="not promotable"):
        promote_track(state, track_id="QR-X", new_status=TrackStatus.RETIRED, now=_now())
    assert state.tracks["QR-X"].status.value == "active"


def test_promote_refused_when_outcome_missed() -> None:
    """A Track with a MISSED outcome is REFUSED even when the period has elapsed."""
    aged = _now() - MIN_PROMOTION_PERIOD - timedelta(days=1)
    state = _state_with_outcomes([_outcome(status=OutcomeStatus.MISSED, updated_at=aged)])
    gate = evaluate_track_promotion(state, track_id="QR-X", now=_now())
    assert gate.promotable is False
    assert gate.outcomes_met is False
    assert gate.blocking_outcome_ids == ["OUT-1"]
    with pytest.raises(LifecycleError, match="not promotable"):
        promote_track(state, track_id="QR-X", new_status=TrackStatus.RETIRED, now=_now())


def test_promote_passes_when_outcomes_met_over_period() -> None:
    """A Track whose MET outcome held the full period promotes on the merits."""
    aged = _now() - MIN_PROMOTION_PERIOD - timedelta(seconds=1)
    state = _state_with_outcomes([_outcome(status=OutcomeStatus.MET, updated_at=aged)])
    gate = promote_track(state, track_id="QR-X", new_status=TrackStatus.RETIRED, now=_now())
    assert gate.promotable is True
    assert gate.outcomes_met is True
    assert gate.period_held is True
    assert gate.forced is False
    assert state.tracks["QR-X"].status.value == "retired"


def test_force_overrides_refuse_with_recorded_reason() -> None:
    """An attested force overrides the refuse and records the reason on the gate.

    The outcome has NOT held the period (would refuse on the merits), but a
    forced override promotes the Track anyway -- and the gate carries both the
    forced flag, the reason, and the still-visible merits verdict.
    """
    state = _state_with_outcomes([_outcome(status=OutcomeStatus.MET, updated_at=_now())])
    gate = promote_track(
        state,
        track_id="QR-X",
        new_status=TrackStatus.RETIRED,
        force_reason="operator sign-off: ship ahead of the hold window",
        now=_now(),
    )
    assert gate.promotable is True
    assert gate.forced is True
    assert gate.force_reason == "operator sign-off: ship ahead of the hold window"
    # The merits verdict stays visible on the forced record for audit.
    assert gate.period_held is False
    assert gate.blocking_outcome_ids == ["OUT-1"]
    assert state.tracks["QR-X"].status.value == "retired"


def test_force_with_blank_reason_is_rejected() -> None:
    """An attested force must record a real reason -- a blank one is rejected."""
    state = _state_with_outcomes([_outcome(status=OutcomeStatus.MET, updated_at=_now())])
    with pytest.raises(LifecycleError, match="attested reason"):
        promote_track(
            state,
            track_id="QR-X",
            new_status=TrackStatus.RETIRED,
            force_reason="   ",
            now=_now(),
        )


def test_promote_refused_when_no_outcomes_defined() -> None:
    """A Track with no reachable outcome is not promotable on the merits.

    Boundary: an empty outcome set carries no evidence the workstream
    delivered, so it blocks promotion just like an unmet one.
    """
    state = _state_with_outcomes([])
    gate = evaluate_track_promotion(state, track_id="QR-X", now=_now())
    assert gate.promotable is False
    assert gate.outcomes_met is False
    with pytest.raises(LifecycleError, match="no outcomes defined"):
        promote_track(state, track_id="QR-X", new_status=TrackStatus.RETIRED, now=_now())


def test_evaluate_unknown_track_raises() -> None:
    """An unknown Track id raises rather than silently passing the gate."""
    state = _state_with_outcomes([_outcome()])
    with pytest.raises(LifecycleError, match="unknown track"):
        evaluate_track_promotion(state, track_id="NOPE-Z", now=_now())


# --- C2: out-of-scope wave containment ---------------------------------------


def test_wave_outside_declared_scope_is_flagged() -> None:
    """A wave touching files outside the Track's declared scope is flagged.

    The Track declares ``src/strategies/collar/**``; the wave touches one
    in-scope file and one out-of-scope file, so only the out-of-scope file is
    returned -- containment is CHECKED, not assumed.
    """
    track = _state_with_outcomes([_outcome()], scope_globs=["src/strategies/collar/**"]).tracks[
        "QR-X"
    ]
    wave = _wave(
        file_scopes=[
            "src/strategies/collar/signal.py",
            "src/strategies/momentum/signal.py",
        ]
    )
    violations = wave_scope_violations(track, wave)
    assert violations == ["src/strategies/momentum/signal.py"]


def test_wave_fully_inside_declared_scope_has_no_violations() -> None:
    """A wave whose every file is covered by the declared scope is contained."""
    track = _state_with_outcomes(
        [_outcome()], scope_globs=["src/strategies/collar/**", "tests/collar/**"]
    ).tracks["QR-X"]
    wave = _wave(file_scopes=["src/strategies/collar/signal.py", "tests/collar/test_signal.py"])
    assert wave_scope_violations(track, wave) == []


def test_track_with_no_declared_scope_cannot_enforce_containment() -> None:
    """A Track that declares no scope flags nothing -- there is nothing to check.

    Boundary: an empty ``scope_globs`` means containment cannot be enforced, so
    every wave file is treated as in-scope rather than crashing or flagging all.
    """
    track = _state_with_outcomes([_outcome()], scope_globs=[]).tracks["QR-X"]
    wave = _wave(file_scopes=["anywhere/at/all.py"])
    assert wave_scope_violations(track, wave) == []
