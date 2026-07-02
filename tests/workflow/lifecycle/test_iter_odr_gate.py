"""Gate tests for the profile-configurable ODR blocking flag at iter close.

:func:`eawf.workflow.lifecycle.transitions.close_iter` scores the closing
iter's CLOSED-wave criteria against the Oracle-Determinism-Ratio floor. The
finding is advisory (log-only) by default; when a profile's
:attr:`~eawf.platform.profiles.models.VerifyBlock.odr_blocking` flag is set the
same sub-floor verdict is a HARD gate that refuses the close before any state
mutation. These tests pin both halves of that contract plus the boundary
(ratio exactly at floor) and sentinel (no required criteria) paths.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from eawf.kernel.spec.common import CriterionSpec, OracleTier, QualityDimension
from eawf.kernel.state.enums import (
    IterStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.platform.profiles.models import VerifyBlock
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    close_iter,
    open_iter,
    open_phase,
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


def _open_iter_with_low_odr(state: State) -> None:
    """Seed P01-I01 with a closed wave whose ODR is 1/3 (below the 0.80 floor)."""
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
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


def test_close_iter_below_floor_blocking_raises(caplog: pytest.LogCaptureFixture) -> None:
    state = _empty_state()
    _open_iter_with_low_odr(state)
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(LifecycleError, match="odr_blocking"),
    ):
        close_iter(
            state,
            iter_id="P01-I01",
            audit_id="AUD-1",
            odr_blocking=VerifyBlock(odr_blocking=True).odr_blocking,
        )
    # The gate fires before any state mutation: the iter stays ACTIVE.
    assert state.iters["P01-I01"].status == IterStatus.ACTIVE
    assert state.current.iter_id == "P01-I01"
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("finding=odr_below_floor" in m and "severity=blocking" in m for m in messages)


def test_close_iter_below_floor_warn_only_closes(caplog: pytest.LogCaptureFixture) -> None:
    state = _empty_state()
    _open_iter_with_low_odr(state)
    with caplog.at_level(logging.WARNING):
        it = close_iter(
            state,
            iter_id="P01-I01",
            audit_id="AUD-1",
            odr_blocking=VerifyBlock(odr_blocking=False).odr_blocking,
        )
    # Advisory-only: the sub-floor ratio logs but never blocks the close.
    assert it.status == IterStatus.CLOSED
    assert state.current.iter_id is None
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("finding=odr_below_floor" in m and "severity=advisory" in m for m in messages)


def test_close_iter_below_floor_default_off_closes(caplog: pytest.LogCaptureFixture) -> None:
    # The default VerifyBlock ships odr_blocking=False, so omitting the arg
    # keeps the advisory-only behaviour.
    assert VerifyBlock().odr_blocking is False
    state = _empty_state()
    _open_iter_with_low_odr(state)
    with caplog.at_level(logging.WARNING):
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1")
    assert it.status == IterStatus.CLOSED


def test_close_iter_above_floor_blocking_closes(caplog: pytest.LogCaptureFixture) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    # ODR = 2/2 = 1.0, at or above the floor even with blocking on.
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
        it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1", odr_blocking=True)
    assert it.status == IterStatus.CLOSED
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_close_iter_at_floor_blocking_closes() -> None:
    # ODR = 3/4 = 0.75; a floor of 0.75 is NOT below (the check is strict <),
    # so the close is allowed even with blocking on.
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _insert_closed_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        criteria=[
            _odr_criterion("CR-01", tier=OracleTier.T1_STATIC),
            _odr_criterion("CR-02", tier=OracleTier.T2_STRUCTURAL),
            _odr_criterion("CR-03", tier=OracleTier.T5_GOLDEN),
            _odr_criterion("CR-04", tier=OracleTier.T7_JURY),
        ],
    )
    it = close_iter(
        state,
        iter_id="P01-I01",
        audit_id="AUD-1",
        odr_floor=0.75,
        odr_blocking=True,
    )
    assert it.status == IterStatus.CLOSED


def test_close_iter_custom_floor_blocking_raises() -> None:
    # The same 0.75 ratio trips a stricter 0.80 floor.
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _insert_closed_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        criteria=[
            _odr_criterion("CR-01", tier=OracleTier.T1_STATIC),
            _odr_criterion("CR-02", tier=OracleTier.T2_STRUCTURAL),
            _odr_criterion("CR-03", tier=OracleTier.T5_GOLDEN),
            _odr_criterion("CR-04", tier=OracleTier.T7_JURY),
        ],
    )
    with pytest.raises(LifecycleError, match="below floor"):
        close_iter(
            state,
            iter_id="P01-I01",
            audit_id="AUD-1",
            odr_floor=0.80,
            odr_blocking=True,
        )
    assert state.iters["P01-I01"].status == IterStatus.ACTIVE


def test_close_iter_no_required_criteria_blocking_closes() -> None:
    # An iter with no required criteria hits the EMPTY_RATIO (1.0) sentinel,
    # so it is never below floor and the close is allowed even with blocking.
    state = _empty_state()
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    _insert_closed_wave(state, wave_id="P01-I01-W01", iter_id="P01-I01", criteria=[])
    it = close_iter(state, iter_id="P01-I01", audit_id="AUD-1", odr_blocking=True)
    assert it.status == IterStatus.CLOSED
