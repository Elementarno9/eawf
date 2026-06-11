"""Tests for the Evidence mode pane over the agent-report rollup (W22).

The Evidence mode (digit ``6``) renders the typed agent-report rollup for
the active scope: one row per report joining role / verdict / wave
(``base_id``) / attempt / follow-up count, plus a follow-up detail block.
These tests pin both halves:

* the pure join + render helpers (:func:`build_evidence_rows`,
  :func:`evidence_summary_line`, :func:`render_followups_block`,
  :func:`sort_evidence_rows`) -- unit-testable without mounting Textual;
* the Pilot-driven pane -- it boots honest-empty (the COMMON path, no
  report store on disk), populates when reports land (role / verdict /
  wave join shown), and surfaces follow-ups in the detail block.

The live ``state.json`` has produced ZERO agent reports, so the
honest-empty render is the dominant case the pane is built for first.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before
asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from textual.widgets import DataTable, Static

from eawf.kernel.spec.common import OracleTier, tier_label
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportFollowup,
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    ReviewerReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.cross_vendor_jury import PerItemJurorBallot, RubricItemVote
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.evidence import (
    ABSTAIN_VOTE,
    EMPTY_NOTICE,
    FAIL_VOTE,
    NO_BALLOTS_NOTICE,
    PASS_VOTE,
    ROW_BLOCKED,
    ROW_READY,
    EvidenceModeScreen,
    EvidenceRow,
    build_ballot_grid,
    build_evidence_rows,
    evidence_summary_line,
    render_ballot_grid,
    render_followups_block,
    render_tier_ladder,
    sort_evidence_rows,
    vote_sigil,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from tests.conftest import make_intent

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
#: A repo fixture with NO sibling ``store/`` -- the honest-empty common path.
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO.is_file(), f"missing evidence fixture: {_REPO}"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


# --------------------------------------------------------------------------
# Fixture builders -- state + report-store seeding
# --------------------------------------------------------------------------


def _empty_state() -> State:
    """Build a minimal valid repo state with no phases/iters/waves."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": NOW.isoformat(),
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


def _state_with_wave(wave_id: str = "P29-I02-W22", title: str = "Add Evidence pane") -> State:
    """Build a state carrying one CLOSED wave for the title join.

    Uses the lifecycle transition helpers so every phase/iter/wave model
    carries its required fields (e.g. the derived ``scope_id``) without a
    hand-built dict that drifts as the schema grows.
    """
    state = _empty_state()
    open_phase(state, phase_id="P29", title="phase")
    open_iter(state, iter_id="P29-I02", phase_id="P29", title="iter")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P29-I02",
        title=title,
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = NOW
    return state


def _write_state(state: State, tmp_path: Path) -> Path:
    """Write *state* to ``tmp_path/.ea/state.json`` and return the path."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    return state_path


def _write_executor_report(
    state_path: Path,
    *,
    base_id: str,
    verdict: AgentReportVerdict,
    attempt: int = 1,
    followups: tuple[AgentReportFollowup, ...] = (),
    summary: str = "attempt completed",
) -> None:
    """Append one executor report envelope keyed by ``base_id`` to the store."""
    body = ExecutorReportBody(
        role="executor",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary=summary,
        wave_id=base_id,
        outcome="done",
        followups=list(followups),
    )
    report_id = f"AR-executor-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="codex",
        generated_at=NOW,
        summary=summary,
    )
    _append_report(state_path, AgentSessionRole.EXECUTOR, report_id, base_id, body, header)


def _write_reviewer_report(
    state_path: Path,
    *,
    base_id: str,
    verdict: AgentReportVerdict,
    attempt: int = 1,
) -> None:
    """Append one reviewer report envelope keyed by ``base_id`` to the store."""
    body = ReviewerReportBody(
        role="reviewer",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="review complete",
        target_id=base_id,
    )
    report_id = f"AR-reviewer-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.REVIEWER,
        session_id=f"REV-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="claude",
        generated_at=NOW,
        summary="review complete",
    )
    _append_report(state_path, AgentSessionRole.REVIEWER, report_id, base_id, body, header)


def _append_report(
    state_path: Path,
    role: AgentSessionRole,
    report_id: str,
    base_id: str,
    body: ExecutorReportBody | ReviewerReportBody,
    header: AgentReportHeader,
) -> None:
    """Write one report envelope to the role store under *state_path*."""
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=NOW + timedelta(minutes=header.attempt),
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(role))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_build_evidence_rows_none_state_path_returns_empty() -> None:
    """No state path (user scope) yields no rows -- the honest-empty path."""
    assert build_evidence_rows(None, None) == ()


def test_build_evidence_rows_no_store_returns_empty(tmp_path: Path) -> None:
    """A state path with no report store on disk yields no rows (common path)."""
    state = _state_with_wave()
    state_path = _write_state(state, tmp_path)

    assert build_evidence_rows(state_path, state) == ()


def test_build_evidence_rows_joins_report_to_wave_title(tmp_path: Path) -> None:
    """A seeded report joins its ``base_id`` to the wave title from state."""
    state = _state_with_wave(wave_id="P29-I02-W22", title="Add Evidence pane")
    state_path = _write_state(state, tmp_path)
    _write_executor_report(state_path, base_id="P29-I02-W22", verdict=AgentReportVerdict.PASS)

    rows = build_evidence_rows(state_path, state)

    assert len(rows) == 1
    row = rows[0]
    assert row.role == "executor"
    assert row.verdict == "pass"
    assert row.wave_id == "P29-I02-W22"
    assert row.wave_title == "Add Evidence pane"
    assert row.wave_label == "P29-I02-W22 Add Evidence pane"


def test_build_evidence_rows_unjoined_base_id_has_no_title(tmp_path: Path) -> None:
    """A report whose base_id is not a wave in state renders with no title."""
    state = _state_with_wave(wave_id="P29-I02-W22")
    state_path = _write_state(state, tmp_path)
    _write_executor_report(state_path, base_id="P29", verdict=AgentReportVerdict.PASS)

    rows = build_evidence_rows(state_path, state)

    assert len(rows) == 1
    assert rows[0].wave_id == "P29"
    assert rows[0].wave_title is None
    assert rows[0].wave_label == "P29"


def test_build_evidence_rows_surfaces_followups(tmp_path: Path) -> None:
    """Follow-up titles on the report body land on the row in order."""
    state = _state_with_wave(wave_id="P29-I02-W22")
    state_path = _write_state(state, tmp_path)
    _write_executor_report(
        state_path,
        base_id="P29-I02-W22",
        verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
        followups=(
            AgentReportFollowup(title="regen TUI goldens"),
            AgentReportFollowup(title="wire the daemon push"),
        ),
    )

    rows = build_evidence_rows(state_path, state)

    assert rows[0].followups == ("regen TUI goldens", "wire the daemon push")
    assert rows[0].followup_count == 2


def test_evidence_summary_line_empty_is_honest_notice() -> None:
    """An empty row tuple renders the honest-empty notice."""
    assert evidence_summary_line(()) == EMPTY_NOTICE


def test_evidence_summary_line_counts_reports_and_followups() -> None:
    """The summary line reports the report count + the follow-up total."""
    rows = (
        EvidenceRow(
            report_id="AR-executor-W1-01",
            role="executor",
            verdict="pass-with-followups",
            wave_id="W1",
            wave_title="one",
            attempt=1,
            summary="s",
            followups=("a", "b"),
        ),
        EvidenceRow(
            report_id="AR-reviewer-W1-01",
            role="reviewer",
            verdict="pass",
            wave_id="W1",
            wave_title="one",
            attempt=1,
            summary="s",
            followups=(),
        ),
    )
    assert evidence_summary_line(rows) == "2 reports - 2 followup(s)"


def test_evidence_summary_line_singular_report_word() -> None:
    """A single report uses the singular 'report' word."""
    rows = (
        EvidenceRow(
            report_id="AR-executor-W1-01",
            role="executor",
            verdict="pass",
            wave_id="W1",
            wave_title=None,
            attempt=1,
            summary="s",
            followups=(),
        ),
    )
    assert evidence_summary_line(rows) == "1 report - 0 followup(s)"


def test_render_followups_block_empty_says_no_followups() -> None:
    """No rows renders the no-followups sentinel."""
    assert render_followups_block(()) == "no followups"


def test_render_followups_block_omits_reports_without_followups() -> None:
    """Only reports that emitted a follow-up appear in the block."""
    rows = (
        EvidenceRow(
            report_id="AR-executor-W1-01",
            role="executor",
            verdict="pass-with-followups",
            wave_id="P29-I02-W22",
            wave_title="t",
            attempt=1,
            summary="s",
            followups=("regen goldens",),
        ),
        EvidenceRow(
            report_id="AR-reviewer-W1-01",
            role="reviewer",
            verdict="pass",
            wave_id="P29-I02-W22",
            wave_title="t",
            attempt=1,
            summary="s",
            followups=(),
        ),
    )
    block = render_followups_block(rows)
    assert "P29-I02-W22 :: executor" in block
    assert "  - regen goldens" in block
    assert "reviewer" not in block


def test_sort_evidence_rows_orders_by_natural_wave_then_role() -> None:
    """Rows sort by natural wave id, so W2 precedes W10, then by role."""
    rows = (
        EvidenceRow("r1", "reviewer", "pass", "P29-I02-W10", None, 1, "s", ()),
        EvidenceRow("r2", "executor", "pass", "P29-I02-W2", None, 1, "s", ()),
        EvidenceRow("r3", "auditor", "pass", "P29-I02-W2", None, 1, "s", ()),
    )
    ordered = sort_evidence_rows(rows)
    assert [(r.wave_id, r.role) for r in ordered] == [
        ("P29-I02-W2", "auditor"),
        ("P29-I02-W2", "executor"),
        ("P29-I02-W10", "reviewer"),
    ]


# --------------------------------------------------------------------------
# Jury-ballot grid + oracle-tier ladder (U2 drill) -- pure helpers
# --------------------------------------------------------------------------


def _ballots() -> tuple[PerItemJurorBallot, ...]:
    """Three disjoint-vendor jurors voting on two rubric items.

    B-01 is unanimous pass; B-02 carries one veto (opencode fails it with a
    refutation) so the row reads blocked. The claude juror casts no vote on
    B-02 -- an abstention -- so the grid never silently treats a non-vote as a
    pass.
    """
    return (
        PerItemJurorBallot(
            juror="claude-code",
            votes=(RubricItemVote(item_id="B-01", passed=True),),
        ),
        PerItemJurorBallot(
            juror="codex",
            votes=(
                RubricItemVote(item_id="B-01", passed=True),
                RubricItemVote(item_id="B-02", passed=True),
            ),
        ),
        PerItemJurorBallot(
            juror="opencode",
            votes=(
                RubricItemVote(item_id="B-01", passed=True),
                RubricItemVote(
                    item_id="B-02", passed=False, refutation="row regressed under pytest"
                ),
            ),
        ),
    )


def test_build_ballot_grid_pivots_jurors_into_columns() -> None:
    """The grid pivots per-juror ballots into one row per rubric item."""
    juror_ids, rows = build_ballot_grid(_ballots(), ("B-01", "B-02"))

    assert juror_ids == ("claude-code", "codex", "opencode")
    assert len(rows) == 2
    assert rows[0].item_id == "B-01"
    assert rows[0].votes == (PASS_VOTE, PASS_VOTE, PASS_VOTE)
    assert rows[1].item_id == "B-02"
    assert rows[1].votes == (ABSTAIN_VOTE, PASS_VOTE, FAIL_VOTE)


def test_build_ballot_grid_one_fail_blocks_the_row() -> None:
    """A row with one fail vote rolls up to blocked; an all-pass row to ready."""
    _, rows = build_ballot_grid(_ballots(), ("B-01", "B-02"))

    assert rows[0].status == ROW_READY
    assert rows[1].status == ROW_BLOCKED


def test_build_ballot_grid_abstain_alone_does_not_block() -> None:
    """A row of pass + abstain (no fail) reads ready, not blocked."""
    ballots = (
        PerItemJurorBallot(juror="codex", votes=(RubricItemVote(item_id="B-01", passed=True),)),
        PerItemJurorBallot(juror="opencode", votes=()),  # abstains on B-01
    )
    _, rows = build_ballot_grid(ballots, ("B-01",))

    assert rows[0].votes == (PASS_VOTE, ABSTAIN_VOTE)
    assert rows[0].status == ROW_READY


def test_build_ballot_grid_empty_rubric_yields_no_rows() -> None:
    """An empty rubric (nothing to score) yields the column header but no rows."""
    juror_ids, rows = build_ballot_grid(_ballots(), ())

    assert juror_ids == ("claude-code", "codex", "opencode")
    assert rows == ()


def test_render_ballot_grid_shows_votes_and_row_status() -> None:
    """The rendered grid carries each juror column + the pinned vote sigils.

    The vote cells render the designer-pinned sigil glyphs (``vote_sigil``),
    not the bare vote words, and the trailing column header is the pinned
    ``verdict`` (not ``status``).
    """
    juror_ids, rows = build_ballot_grid(_ballots(), ("B-01", "B-02"))
    block = render_ballot_grid(juror_ids, rows)

    assert "claude-code" in block
    assert "codex" in block
    assert "opencode" in block
    # The trailing column header is the designer-pinned ``verdict``.
    assert block.splitlines()[0].endswith("verdict")
    # B-02: one fail -> the row reads blocked, with each cell the pinned sigil:
    # abstain ring, pass circle, fail cross (rendered in the default column).
    assert vote_sigil(FAIL_VOTE) in block
    assert vote_sigil(ABSTAIN_VOTE) in block
    expected = (
        f"B-02  {vote_sigil(ABSTAIN_VOTE)}  {vote_sigil(PASS_VOTE)}  "
        f"{vote_sigil(FAIL_VOTE)}  {ROW_BLOCKED}"
    )
    assert expected in block


def test_render_ballot_grid_empty_is_honest_notice() -> None:
    """No grid rows renders the honest-empty ballots notice."""
    assert render_ballot_grid((), ()) == NO_BALLOTS_NOTICE


def test_render_tier_ladder_uses_real_tier_names_via_tier_label() -> None:
    """The ladder renders every T1_STATIC..T7_JURY label via tier_label."""
    block = render_tier_ladder(OracleTier.T7_JURY)

    # Every real tier label is present, sourced from tier_label (never hardcoded).
    for tier in OracleTier:
        assert tier_label(tier) in block
    # The full real-name span is covered end to end.
    assert "T1 static" in block
    assert "T7 jury" in block


def test_render_tier_ladder_marks_the_scoring_tier() -> None:
    """The scoring tier line is marked; the others are not."""
    block = render_tier_ladder(OracleTier.T4_CONTRACT)
    lines = block.splitlines()

    scored = next(line for line in lines if tier_label(OracleTier.T4_CONTRACT) in line)
    other = next(line for line in lines if tier_label(OracleTier.T1_STATIC) in line)
    assert scored.startswith(">")
    assert not other.startswith(">")


def test_render_tier_ladder_none_marks_no_tier() -> None:
    """A None scoring tier marks no line (the honest-empty path)."""
    block = render_tier_ladder(None)

    assert not any(line.startswith(">") for line in block.splitlines())
    # The ladder still renders every real tier name.
    assert "T1 static" in block
    assert "T7 jury" in block


# --------------------------------------------------------------------------
# Pilot-driven pane -- honest-empty + populated
# --------------------------------------------------------------------------


def test_evidence_pane_renders_honest_empty_when_no_reports() -> None:
    """Digit 6 boots the Evidence pane honest-empty (the common path)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            assert isinstance(app.screen, EvidenceModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame
            # The empty notice shows; the table is hidden (no rows).
            screen = app.screen
            assert screen.query_one("#evidence-empty", Static).display is True
            assert screen.query_one("#evidence-table", DataTable).display is False

    asyncio.run(body())


def test_evidence_pane_renders_reports_with_role_verdict_wave(tmp_path: Path) -> None:
    """With reports on disk the pane shows role / verdict / wave-join rows."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P29-I02-W22", title="Add Evidence pane")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P29-I02-W22", verdict=AgentReportVerdict.PASS)
        _write_reviewer_report(state_path, base_id="P29-I02-W22", verdict=AgentReportVerdict.PASS)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            table = screen.query_one("#evidence-table", DataTable)
            assert table.display is True
            assert table.row_count == 2
            assert screen.query_one("#evidence-empty", Static).display is False
            frame = normalize_snapshot(capture_screen_text(app))
            assert "executor" in frame
            assert "reviewer" in frame
            # The verdict column renders the tinted lifecycle sigil, not the
            # raw enum word: a clean pass wears the CLOSED filled circle.
            assert glyph(Sigil.CLOSED, mode=app.render_mode) in frame
            # The wave join (id + title) renders in the report column.
            assert "P29-I02-W22" in frame
            assert "Add Evidence pane" in frame
            assert EMPTY_NOTICE not in frame

    asyncio.run(body())


def test_evidence_pane_surfaces_followups_in_detail_block(tmp_path: Path) -> None:
    """A report's follow-ups render under the wave/role heading in the block."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P29-I02-W22")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(
            state_path,
            base_id="P29-I02-W22",
            verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
            followups=(AgentReportFollowup(title="regen TUI goldens"),),
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            # The follow-up block widget exists and the rendered frame shows
            # the follow-up under its wave/role heading.
            assert screen.query_one("#evidence-followups", Static) is not None
            frame = normalize_snapshot(capture_screen_text(app))
            assert "P29-I02-W22 :: executor" in frame
            assert "regen TUI goldens" in frame

    asyncio.run(body())


def test_evidence_pane_renders_seeded_ballot_grid_and_tier_ladder(tmp_path: Path) -> None:
    """Seeded ballots render the juror x rubric grid + the marked tier ladder.

    Drives the live Evidence pane, pushes seeded jury ballots + the scoring
    oracle tier through the :meth:`set_ballots` render seam (the same external-
    push pattern :meth:`set_readiness` uses, since the pane never convenes a
    live jury), and asserts both halves of the U2 drill surface: the
    pass/fail/abstain grid with the blocked row, and the T1_STATIC..T7_JURY
    ladder with the scoring tier marked via the real ``tier_label`` names.
    """

    async def body() -> None:
        state = _state_with_wave(wave_id="P29-I02-W22")
        state_path = _write_state(state, tmp_path)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            screen.set_ballots(
                _ballots(),
                rubric_item_ids=("B-01", "B-02"),
                scored_tier=OracleTier.T7_JURY,
            )
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The juror x rubric grid: the pinned vote sigils (abstain ring,
            # fail cross) render in the app's column + the blocked row word.
            assert "claude-code" in frame
            assert "opencode" in frame
            assert vote_sigil(ABSTAIN_VOTE, mode=app.render_mode) in frame
            assert vote_sigil(FAIL_VOTE, mode=app.render_mode) in frame
            assert ROW_BLOCKED in frame
            # The oracle-tier ladder uses the real tier_label names and marks T7.
            assert "T1 static" in frame
            assert "T7 jury" in frame

    asyncio.run(body())
