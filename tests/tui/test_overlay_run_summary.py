"""Tests for the FA7 fleet run-summary terminal card.

When the daemon-owned fleet auto-drain loop reaches a terminal stop, the
Autopilot pane opens the
:class:`~eawf.surfaces.tui.screens.overlays.run_summary.RunSummaryModal` over the
cockpit -- a one-screen debrief that NAMES which terminal reason (``drained`` /
``converged`` / ``budget``) ended the run, then lays out the ``N closed /
M failed / K blocked`` lane tally, the EU + $ spend totals, the elapsed window,
the forks-resolved count, and the per-wave outcome list. ``Enter`` / ``Esc``
returns to the cockpit.

These tests pin the two halves:

* the pure render helpers (:func:`terminal_headline`, :func:`render_counts_row`,
  :func:`render_totals_row`, :func:`outcome_lines`) -- tested directly against
  built :class:`FleetRun` rows so the figures are verified WITHOUT mounting
  Textual, proving every figure is read off the persisted counters (the C2
  reads-not-recomputes contract); and
* the mounted overlay under a Pilot: the card renders the counts / totals / outcome
  list with a header naming the terminal reason, and ``Enter`` returns to the
  cockpit.

Determinism follows the project Pilot-worker rule: each Pilot body drains workers
via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
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
from eawf.surfaces.tui.screens.overlays.run_summary import (
    OUTCOMES_CAPTION,
    OUTCOMES_EMPTY,
    RUN_SUMMARY_COUNTS_ID,
    RUN_SUMMARY_HEADER_ID,
    RUN_SUMMARY_OUTCOMES_ID,
    RUN_SUMMARY_TITLE,
    RUN_SUMMARY_TOTALS_ID,
    RunSummaryModal,
    format_elapsed,
    outcome_lines,
    render_counts_row,
    render_totals_row,
    terminal_headline,
)
from eawf.surfaces.tui.snapshot import settle_screen

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _done_run(
    *,
    reason: FleetTerminalReason = FleetTerminalReason.DRAINED,
    closed: int = 5,
    failed: int = 1,
    blocked: int = 2,
    forks_resolved: int = 3,
    spent_eu: float = 7.5,
    spent_usd: float = 4.25,
    elapsed_hours: float | None = 1.5,
) -> FleetRun:
    """Build a terminal (DONE) :class:`FleetRun` with the figures the card reads.

    Every figure the FA7 card surfaces (counts / EU / $ / elapsed /
    forks-resolved / terminal reason) is seeded here so the assertions read off a
    persisted terminal record rather than a recomputed tally.
    """
    return FleetRun(
        run_state=FleetRunState.DONE,
        concurrency=4,
        frontier=[],
        counters=FleetCounters(
            claimed=closed + failed + blocked,
            dispatched=closed + failed + blocked,
            closed=closed,
            failed=failed,
            blocked=blocked,
            forks_resolved=forks_resolved,
            spent_eu=spent_eu,
            spent_usd=spent_usd,
        ),
        terminal_reason=reason,
        elapsed_hours=elapsed_hours,
        throughput=3.0 if elapsed_hours else None,
        armed_at=_T0,
        ended_at=_T0,
    )


def _state(*, fleet_run: FleetRun | None = None) -> State:
    """Build a minimal repo state, optionally carrying a terminal fleet run."""
    return State.model_validate(
        {
            "schema_version": "1.10" if fleet_run is not None else "1.3",
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
            "fleet_run": fleet_run.model_dump(mode="json") if fleet_run is not None else None,
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
# terminal_headline -- the C1 terminal-reason naming (boundary + error paths)
# --------------------------------------------------------------------------


def test_terminal_headline_drained_names_drained_reason() -> None:
    """A drained run's headline names the frontier-drained stop (C1)."""
    headline = terminal_headline(_done_run(reason=FleetTerminalReason.DRAINED))
    assert "drained" in headline


def test_terminal_headline_converged_names_converged_reason() -> None:
    """A converged run's headline names the convergence-criterion stop (C1)."""
    headline = terminal_headline(_done_run(reason=FleetTerminalReason.CONVERGED))
    assert "converged" in headline


def test_terminal_headline_budget_names_budget_reason() -> None:
    """A budget-halt run's headline names the spend-cap stop (C1)."""
    headline = terminal_headline(_done_run(reason=FleetTerminalReason.BUDGET))
    assert "budget" in headline


def test_terminal_headline_three_reasons_are_distinct() -> None:
    """The three terminal reasons render three DISTINCT headlines (C1).

    The card must DISTINGUISH the three stops, so no two reasons collapse to the
    same header phrase.
    """
    drained = terminal_headline(_done_run(reason=FleetTerminalReason.DRAINED))
    converged = terminal_headline(_done_run(reason=FleetTerminalReason.CONVERGED))
    budget = terminal_headline(_done_run(reason=FleetTerminalReason.BUDGET))
    assert len({drained, converged, budget}) == 3


def test_terminal_headline_missing_reason_reads_honest_fallback() -> None:
    """A run with no terminal reason recorded reads an honest fallback, not blank.

    The error path: the card opened before the daemon stamped a reason, so the
    headline says so rather than rendering a blank header.
    """
    run = FleetRun(run_state=FleetRunState.DONE, armed_at=_T0, terminal_reason=None)
    headline = terminal_headline(run)
    assert "not recorded" in headline


# --------------------------------------------------------------------------
# render_counts_row / render_totals_row / outcome_lines -- reads-not-recompute
# --------------------------------------------------------------------------


def test_render_counts_row_reads_closed_failed_blocked_off_counters() -> None:
    """The counts row reads N/M/K STRAIGHT off the persisted counters (C2)."""
    body = render_counts_row(_done_run(closed=5, failed=1, blocked=2))
    assert "5 closed" in body
    assert "1 failed" in body
    assert "2 blocked" in body


def test_render_totals_row_reads_eu_usd_elapsed_forks_off_run() -> None:
    """The totals row reads EU / $ / elapsed / forks-resolved off the run (C2)."""
    body = render_totals_row(
        _done_run(spent_eu=7.5, spent_usd=4.25, forks_resolved=3, elapsed_hours=1.5)
    )
    assert "7.5" in body  # spent_eu
    assert "4.25" in body  # spent_usd
    assert "1h30m" in body  # elapsed_hours, compact h/m form
    assert "forks resolved" in body
    assert "3" in body  # forks_resolved


def test_render_totals_row_missing_elapsed_reads_honest_dash() -> None:
    """A run with no elapsed window recorded reads a dash, not a fabricated zero."""
    body = render_totals_row(_done_run(elapsed_hours=None))
    assert "elapsed" in body
    assert "--" in body


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (None, "--"),
        (0.0, "0m"),
        (0.03216431222222222, "2m"),  # the live-3a sub-hour window
        (0.5, "30m"),
        (0.999, "1h00m"),  # rounds up across the hour boundary
        (1.0, "1h00m"),
        (1.5, "1h30m"),
        (2.0, "2h00m"),
    ],
)
def test_format_elapsed_renders_compact_h_m(hours: float | None, expected: str) -> None:
    """Elapsed renders as [#h]##m: dash for None, ##m sub-hour, #h##m at/over an hour."""
    assert format_elapsed(hours) == expected


def test_outcome_lines_lists_one_line_per_nonzero_outcome_class() -> None:
    """The outcome list reads one labelled line per non-zero outcome class (C1)."""
    lines = outcome_lines(_done_run(closed=5, failed=1, blocked=2, forks_resolved=3))
    joined = "\n".join(lines)
    assert "closed" in joined
    assert "failed" in joined
    assert "blocked" in joined
    assert "fork resolved" in joined


def test_outcome_lines_omits_zero_outcome_classes() -> None:
    """An outcome class with zero lanes is omitted from the list (no faked row)."""
    lines = outcome_lines(_done_run(closed=5, failed=0, blocked=0, forks_resolved=0))
    joined = "\n".join(lines)
    assert "closed" in joined
    assert "failed" not in joined
    assert "blocked" not in joined


def test_outcome_lines_no_outcomes_reads_honest_empty() -> None:
    """A run with no finished lane yields the honest-empty outcome marker."""
    lines = outcome_lines(_done_run(closed=0, failed=0, blocked=0, forks_resolved=0))
    assert lines == (f"[$muted]{OUTCOMES_EMPTY}[/]",)


def test_outcomes_caption_names_tallies_not_per_wave_rows() -> None:
    """The caption matches the class-tally rows the card renders."""
    assert OUTCOMES_CAPTION == "outcome tallies"


# --------------------------------------------------------------------------
# Mounted overlay -- renders the card, Enter returns to cockpit
# --------------------------------------------------------------------------


def test_run_summary_card_renders_counts_totals_and_outcomes(tmp_path: Path) -> None:
    """The mounted card renders the counts, totals, and per-wave outcome list.

    The load-bearing C1 assertion under a Pilot: the card surfaces the
    ``N closed / M failed / K blocked`` tally, the EU / $ / elapsed /
    forks-resolved totals, the per-wave outcome list, and a header that NAMES the
    terminal reason.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            run = _done_run(reason=FleetTerminalReason.BUDGET)
            await app.push_screen(RunSummaryModal(run))
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, RunSummaryModal)
            header = str(modal.query_one(f"#{RUN_SUMMARY_HEADER_ID}").render())  # type: ignore[attr-defined]
            assert RUN_SUMMARY_TITLE in header
            assert "budget" in header  # the terminal reason is named
            counts = str(modal.query_one(f"#{RUN_SUMMARY_COUNTS_ID}").render())  # type: ignore[attr-defined]
            assert "5 closed" in counts
            assert "1 failed" in counts
            assert "2 blocked" in counts
            totals = str(modal.query_one(f"#{RUN_SUMMARY_TOTALS_ID}").render())  # type: ignore[attr-defined]
            assert "forks resolved" in totals
            # The per-wave outcome list container is mounted with rows.
            assert modal.query(f"#{RUN_SUMMARY_OUTCOMES_ID}")
            assert modal.query(".run-summary-outcome")

    asyncio.run(body())


def test_run_summary_enter_returns_to_cockpit(tmp_path: Path) -> None:
    """``Enter`` dismisses the run-summary card and returns to the cockpit."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("2")  # -> autopilot cockpit
            await settle_screen(pilot)
            cockpit = app.screen
            await app.push_screen(RunSummaryModal(_done_run()))
            await settle_screen(pilot)
            assert isinstance(app.screen, RunSummaryModal)
            await pilot.press("enter")  # return to cockpit
            await settle_screen(pilot)
            assert app.screen is cockpit  # the card dismissed back to the cockpit

    asyncio.run(body())


def test_run_summary_esc_returns_to_cockpit(tmp_path: Path) -> None:
    """``Esc`` also dismisses the run-summary card (the card's only verb)."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("2")
            await settle_screen(pilot)
            cockpit = app.screen
            await app.push_screen(RunSummaryModal(_done_run()))
            await settle_screen(pilot)
            assert isinstance(app.screen, RunSummaryModal)
            await pilot.press("escape")
            await settle_screen(pilot)
            assert app.screen is cockpit

    asyncio.run(body())
