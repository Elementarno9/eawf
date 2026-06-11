"""Tests for the ``PauseResolution`` model + the ``UserDecisionKind`` enum.

``PauseResolution`` is the shared typed record both operator-decision surfaces
(the ``needs_user`` pause + the fleet-fork) project onto, kind-tagged by
``UserDecisionKind``. The validator enforces the decision-kind / fork-resolution
coupling so a malformed cross-surface row fails at construction.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import UserDecisionKind
from eawf.kernel.state.models import FleetForkResolution, PauseResolution

_NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


def test_user_decision_kind_closed_set() -> None:
    """The enum carries exactly the two operator-decision families."""
    assert {k.value for k in UserDecisionKind} == {"pause", "fleet_fork"}


def test_pause_resolution_pause_round_trips() -> None:
    """A PAUSE resolution validates and serialises with no fork_resolution."""
    row = PauseResolution(
        decision_kind=UserDecisionKind.PAUSE,
        scope_id="P30-I16-W21",
        ref="urn:eawf:v1:event:P30-I16-W21/needs-user-abc123",
        choice="re-dispatch",
        resolved_at=_NOW,
    )
    assert row.fork_resolution is None
    assert row.choice == "re-dispatch"
    dumped = row.model_dump(mode="json")
    assert dumped["decision_kind"] == "pause"
    assert dumped["fork_resolution"] is None


def test_pause_resolution_fleet_fork_round_trips() -> None:
    """A FLEET_FORK resolution carries a fork_resolution whose value is the choice."""
    row = PauseResolution(
        decision_kind=UserDecisionKind.FLEET_FORK,
        scope_id="P30-I16-W21",
        ref="P30-I16-W21#1",
        choice=FleetForkResolution.APPROVE_CLOSE.value,
        fork_resolution=FleetForkResolution.APPROVE_CLOSE,
        resolved_at=_NOW,
    )
    assert row.fork_resolution is FleetForkResolution.APPROVE_CLOSE
    assert row.choice == "approve_close"


def test_pause_resolution_defaults_urgency_normal() -> None:
    """Urgency defaults to NORMAL so the field is additive for non-ranking callers."""
    row = PauseResolution(
        decision_kind=UserDecisionKind.PAUSE,
        scope_id="QR",
        ref="urn:eawf:v1:event:QR/needs-user-xyz",
        choice="approve",
        resolved_at=_NOW,
    )
    assert row.urgency.value == "normal"


def test_pause_resolution_pause_rejects_fork_resolution() -> None:
    """Error path: a PAUSE decision must not carry a fork_resolution."""
    with pytest.raises(ValidationError, match="must not carry a fork_resolution"):
        PauseResolution(
            decision_kind=UserDecisionKind.PAUSE,
            scope_id="QR",
            ref="urn:eawf:v1:event:QR/needs-user-1",
            choice="skip",
            fork_resolution=FleetForkResolution.SKIP,
            resolved_at=_NOW,
        )


def test_pause_resolution_fleet_fork_requires_fork_resolution() -> None:
    """Error path: a FLEET_FORK decision must carry a fork_resolution."""
    with pytest.raises(ValidationError, match="requires a fork_resolution"):
        PauseResolution(
            decision_kind=UserDecisionKind.FLEET_FORK,
            scope_id="QR",
            ref="P30-I16-W21#1",
            choice="approve_close",
            resolved_at=_NOW,
        )


def test_pause_resolution_fleet_fork_choice_must_match_resolution() -> None:
    """Error path: a FLEET_FORK choice disagreeing with its fork_resolution rejects."""
    with pytest.raises(ValidationError, match="must equal"):
        PauseResolution(
            decision_kind=UserDecisionKind.FLEET_FORK,
            scope_id="QR",
            ref="P30-I16-W21#1",
            choice="approve_close",
            fork_resolution=FleetForkResolution.SKIP,
            resolved_at=_NOW,
        )


def test_pause_resolution_rejects_empty_choice() -> None:
    """Boundary: an empty choice fails the min_length floor."""
    with pytest.raises(ValidationError):
        PauseResolution(
            decision_kind=UserDecisionKind.PAUSE,
            scope_id="QR",
            ref="urn:eawf:v1:event:QR/needs-user-2",
            choice="",
            resolved_at=_NOW,
        )


def test_pause_resolution_forbids_extra_keys() -> None:
    """Schema mismatch: an unknown key is rejected by ``extra="forbid"``."""
    with pytest.raises(ValidationError):
        PauseResolution.model_validate(
            {
                "decision_kind": "pause",
                "scope_id": "QR",
                "ref": "urn:eawf:v1:event:QR/needs-user-3",
                "choice": "approve",
                "resolved_at": _NOW.isoformat(),
                "surprise": "unexpected",
            }
        )
