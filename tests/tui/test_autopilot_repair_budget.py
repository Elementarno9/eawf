"""Tests: the autopilot lane band displays the repair_budget the daemon ENFORCES (REL-004).

Before REL-004 the pane read a local ``REPAIR_BUDGET = 3`` literal while the
daemon seeded ``CloseAttempt.repair_budget_remaining`` from its own literal, so
the counter an operator read was NOT the ceiling the close would honour. The
single resolver
(:func:`~eawf.kernel.state.models.resolve_close_budget`) now feeds both, and
these tests pin the acceptance criterion: the token the mounted pane renders
carries the same remaining count the daemon enforces for the wave's live close
attempt.

Determinism follows the project Pilot-worker rule: the Pilot body drains workers
via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AuditRequirement,
    CloseAttemptStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import (
    CloseAttempt,
    CurrentPointers,
    FleetCounters,
    FleetLane,
    FleetRun,
    FleetRunState,
    Project,
    State,
    resolve_close_budget,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    LANE_CELL_CLASS,
    REPAIR_BUDGET,
    REPAIR_LABEL,
    AutopilotModeScreen,
    LaneCellRow,
    lane_cells,
    lane_repair_budget,
    render_lane_cell,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

_T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_AUTOPILOT_DIGIT = "2"
_WAVE = "P01-I01-W02"
_SHA = "a" * 40
_DIGEST = "b" * 64


def _close_attempt(*, repair_remaining: int, infra_remaining: int = 1) -> CloseAttempt:
    """Build a live close attempt carrying the budget the daemon enforces."""
    return CloseAttempt(
        id="CA-01",
        wave_id=_WAVE,
        outcome="close submitted",
        tokens_consumed=None,
        generation=1,
        supersedes_id=None,
        status=CloseAttemptStatus.QUEUED,
        integration_id="WI-01",
        candidate_sha=_SHA,
        integrated_sha=_SHA,
        tree_sha=_SHA,
        wave_revision_digest=_DIGEST,
        spec_digest=_DIGEST,
        criteria_digest=_DIGEST,
        gate_manifest_digest=_DIGEST,
        policy_digest=_DIGEST,
        runner_environment_digest=_DIGEST,
        dependency_binding_digest=_DIGEST,
        audit_requirement=AuditRequirement.NONE,
        no_runtime_waiver=False,
        repair_budget_remaining=repair_remaining,
        infrastructure_retry_budget_remaining=infra_remaining,
        requested_at=_T0,
        updated_at=_T0,
        idempotency_key=f"close:{_WAVE}:1",
    )


def _fleet_run() -> FleetRun:
    """Build a DRAINING run with exactly one in-flight lane on :data:`_WAVE`."""
    return FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=4,
        frontier=[],
        lanes={_WAVE: FleetLane(wave_id=_WAVE, attempt=1, pgid=1000, dispatched_at=_T0)},
        counters=FleetCounters(claimed=1, dispatched=1, closed=0, forked=0),
        armed_at=_T0,
    )


def _state(*, attempt: CloseAttempt | None = None) -> State:
    """Build a repo state with a draining run and an optional live close attempt."""
    return State.model_validate(
        {
            "schema_version": "1.20",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="quant-research",
                title="Quant Research",
                domains=["quant"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "fleet_run": _fleet_run().model_dump(mode="json"),
            "close_attempts": (
                {attempt.id: attempt.model_dump(mode="json")} if attempt is not None else {}
            ),
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


# --------------------------------------------------------------------------
# lane_repair_budget -- the enforced budget the cell displays
# --------------------------------------------------------------------------


def test_lane_repair_budget_unbound_state_falls_back_to_seed() -> None:
    """No bound state cannot read an attempt, so the seed budget is displayed."""
    assert lane_repair_budget(None, _WAVE) == REPAIR_BUDGET


def test_lane_repair_budget_no_close_attempt_falls_back_to_seed() -> None:
    """A wave never submitted for close displays the seed budget."""
    assert lane_repair_budget(_state(), _WAVE) == REPAIR_BUDGET


def test_lane_repair_budget_reads_the_enforced_remaining_budget() -> None:
    """A live attempt's REMAINING repair budget is what the cell displays."""
    state = _state(attempt=_close_attempt(repair_remaining=1))
    enforced = resolve_close_budget(attempt=state.close_attempts["CA-01"])
    assert lane_repair_budget(state, _WAVE) == enforced.total_repair_attempts


def test_lane_repair_budget_exhausted_attempt_displays_one_attempt() -> None:
    """A spent repair budget funds only the attempt already in flight."""
    state = _state(attempt=_close_attempt(repair_remaining=0))
    assert lane_repair_budget(state, _WAVE) == 1


def test_lane_repair_budget_unknown_wave_falls_back_to_seed() -> None:
    """A wave id with no attempt of its own never borrows another wave's budget."""
    state = _state(attempt=_close_attempt(repair_remaining=0))
    assert lane_repair_budget(state, "P01-I01-W99") == REPAIR_BUDGET


def test_lane_repair_budget_rejects_a_non_state_argument() -> None:
    """A non-state argument fails fast rather than silently displaying the seed."""
    with pytest.raises(AttributeError):
        lane_repair_budget("not-a-state", _WAVE)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# lane_cells / render_lane_cell -- displayed == enforced
# --------------------------------------------------------------------------


def test_lane_cells_carry_the_enforced_repair_budget() -> None:
    """Each projected cell carries its own wave's enforced budget, not a constant."""
    state = _state(attempt=_close_attempt(repair_remaining=0))
    cells = lane_cells(state.fleet_run, state=state)
    assert len(cells) == 1
    assert cells[0].budget == 1


def test_render_lane_cell_token_matches_the_enforced_budget() -> None:
    """The rendered ``repair n/<budget>`` token reads the row's enforced budget."""
    rendered = render_lane_cell(LaneCellRow(wave_id=_WAVE, attempt=1, exhausted=False, budget=1))
    assert f"{REPAIR_LABEL} 1/1" in rendered


def test_repair_budget_constant_is_resolver_derived_not_a_literal() -> None:
    """The pane's seed denominator comes from the one resolver (no local literal)."""
    assert resolve_close_budget().total_repair_attempts == REPAIR_BUDGET


# --------------------------------------------------------------------------
# mounted pane -- the acceptance criterion under a Pilot
# --------------------------------------------------------------------------


def test_autopilot_pane_repair_budget_token_equals_the_enforced_budget(tmp_path: Path) -> None:
    """REL-004 acceptance: the mounted pane's counter IS the daemon-enforced budget.

    The wave's live close attempt has its repair budget already spent, so the
    daemon will fund no further repair generation. The lane cell must therefore
    render ``repair 1/1`` -- and specifically NOT the pre-REL-004 ``repair 1/3``
    display literal, which promised a ceiling the close would refuse.
    """
    attempt = _close_attempt(repair_remaining=0)
    state = _state(attempt=attempt)
    state_path = _write_state(tmp_path, state)
    enforced = resolve_close_budget(attempt=attempt).total_repair_attempts

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            assert len(pane.query(f".{LANE_CELL_CLASS}")) == 1
            frame = normalize_snapshot(capture_screen_text(app))
            assert f"{REPAIR_LABEL} 1/{enforced}" in frame
            assert f"{REPAIR_LABEL} 1/3" not in frame

    asyncio.run(body())


def test_autopilot_pane_repair_budget_token_tracks_a_funded_attempt(tmp_path: Path) -> None:
    """A funded attempt widens the displayed denominator to the funded ceiling."""
    attempt = _close_attempt(repair_remaining=1)
    state_path = _write_state(tmp_path, _state(attempt=attempt))
    enforced = resolve_close_budget(attempt=attempt).total_repair_attempts

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, AutopilotModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            assert f"{REPAIR_LABEL} 1/{enforced}" in frame

    asyncio.run(body())
