"""Golden snapshot for the FA7 fleet run-summary terminal card (P30-I13-W07).

When the daemon-owned fleet auto-drain loop reaches a terminal stop, the
Autopilot pane opens the
:class:`~eawf.surfaces.tui.screens.overlays.run_summary.RunSummaryModal` over the
cockpit -- a one-screen debrief that NAMES which terminal reason (``drained`` /
``converged`` / ``budget``) ended the run, then lays out the ``N closed /
M failed / K blocked`` lane tally, the EU + $ spend totals, the elapsed window,
the forks-resolved count, and the per-wave outcome list.

These tests pin two goldens (one per distinct terminal reason) so a layout or
wording regression on the debrief card -- or a regression that conflates two
terminal reasons -- is caught:

* the BUDGET-halt card (a spend cap fired); and
* the DRAINED card (the frontier emptied) -- the two headers differ, pinning that
  the card DISTINGUISHES the terminal reasons.

Regenerate the goldens after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 \
        uv run pytest tests/snapshots/tui/test_run_summary_overlay.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import (
    CurrentPointers,
    FleetCounters,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
    Project,
    State,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.run_summary import RunSummaryModal
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_SIZE = (120, 40)
_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _done_run(reason: FleetTerminalReason) -> FleetRun:
    """Build a terminal (DONE) :class:`FleetRun` for *reason* with fixed figures."""
    return FleetRun(
        run_state=FleetRunState.DONE,
        concurrency=4,
        frontier=[],
        counters=FleetCounters(
            claimed=8,
            dispatched=8,
            closed=5,
            failed=1,
            blocked=2,
            forks_resolved=3,
            spent_eu=7.5,
            spent_usd=4.25,
        ),
        terminal_reason=reason,
        elapsed_hours=1.5,
        throughput=3.0,
        armed_at=_T0,
        ended_at=_T0,
    )


def _state() -> State:
    """Build a minimal repo state (the card is pushed directly, not via a run)."""
    return State.model_validate(
        {
            "schema_version": "1.3",
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


def test_run_summary_budget_snapshot(tmp_path: Path) -> None:
    """The run-summary card for a BUDGET halt renders its debrief golden.

    Pins the card layout (header naming the budget stop, counts / totals rows,
    per-wave outcome list) so a layout regression is caught.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.push_screen(RunSummaryModal(_done_run(FleetTerminalReason.BUDGET)))
            await settle_screen(pilot)
            assert isinstance(app.screen, RunSummaryModal)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "fleet run complete" in frame
            assert "budget" in frame  # the terminal reason is named
            assert "5 closed" in frame
            assert "forks resolved" in frame
            assert_screen_snapshot(app, _GOLDEN / "run_summary_budget.txt")

    asyncio.run(body())


def test_run_summary_drained_snapshot(tmp_path: Path) -> None:
    """The run-summary card for a DRAINED stop renders its (distinct) golden.

    The drained header differs from the budget header, pinning that the card
    DISTINGUISHES the three terminal reasons rather than collapsing them.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.push_screen(RunSummaryModal(_done_run(FleetTerminalReason.DRAINED)))
            await settle_screen(pilot)
            assert isinstance(app.screen, RunSummaryModal)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "drained" in frame  # distinct terminal-reason headline
            assert "budget" not in frame
            assert_screen_snapshot(app, _GOLDEN / "run_summary_drained.txt")

    asyncio.run(body())


@pytest.mark.parametrize("width", [40, 48])
def test_run_summary_budget_narrow_snapshot(tmp_path: Path, width: int) -> None:
    """The run-summary card stays coherent at 40/48 columns."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(width, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(RunSummaryModal(_done_run(FleetTerminalReason.BUDGET)))
            await settle_screen(pilot)
            assert isinstance(app.screen, RunSummaryModal)
            assert_screen_snapshot(app, _GOLDEN / f"run_summary_budget_w{width}.txt")

    asyncio.run(body())
