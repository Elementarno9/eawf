"""Tests for the Evidence pane W27 fixes -- labels, columns, layout, live keys.

W27 tightens four things the earlier Evidence pane got wrong:

* a campaign-scoped researcher report labels by its campaign TOPIC (joined
  via the research-campaign store on ``base_id == campaign_id``) instead of
  the raw ``campaign-<hex>`` id, and the producing role moves to its own
  table column so the label column no longer carries the fused ``:: role``
  suffix;
* the oracle-tier ladder renders inline (one row) instead of one tier per
  line, and the Followups section is separated from the reports table by a
  top margin;
* the reports table receives focus on mount so arrow keys move the selection
  and both ``p`` (peek) and ``Enter`` (open) produce a visible response;
* a report whose WAVE is terminal-failed renders a failed cross-mark against
  its self-claimed verdict, so a failed wave never reads as an unqualified
  pass.

The pure join / render helpers are pinned without a Textual mount; the live
keys + focus + layout are pinned under the Pilot harness, draining workers via
:func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from textual.widgets import DataTable, Static

from eawf.kernel.spec.common import OracleTier, tier_label
from eawf.kernel.spec.research_campaign import ResearchProfileBlock, StagedCampaign
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    CampaignStatus,
    Confidence,
    ProjectStatus,
    ScopeKind,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    ResearcherReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modals.evidence_drill import EvidenceDrillModal
from eawf.surfaces.tui.modals.report_detail import ReportDetailModal
from eawf.surfaces.tui.modes.evidence import (
    _COLUMNS,
    EvidenceModeScreen,
    build_evidence_rows,
    render_tier_ladder,
    verdict_cell,
    verdict_sigil,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from eawf.workflow.verify.models import CloseReadiness, CriterionView, GateResult
from tests.conftest import make_intent

_NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
_SIZE = (120, 40)
_CAMPAIGN_ID = "campaign-0a1b2c3d4e5f"
_TOPIC = "quantized attention survey"


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
# Fixture builders -- state + report / campaign store seeding
# --------------------------------------------------------------------------


def _empty_state() -> State:
    """Build a minimal valid repo state with no phases/iters/waves."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _NOW.isoformat(),
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


def _state_with_wave(
    wave_id: str = "P30-I21-W27",
    title: str = "Fix Evidence pane",
    status: WaveStatus = WaveStatus.CLOSED,
) -> State:
    """Build a state carrying one wave in *status* for the title / failed join."""
    state = _empty_state()
    open_phase(state, phase_id="P30", title="phase")
    open_iter(state, iter_id="P30-I21", phase_id="P30", title="iter")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P30-I21",
        title=title,
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    wave = state.waves[wave_id]
    wave.status = status
    if status is WaveStatus.CLOSED:
        wave.closed_at = _NOW
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
) -> None:
    """Append one executor report envelope keyed by ``base_id`` to the store."""
    body = ExecutorReportBody(
        role="executor",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="attempt completed",
        wave_id=base_id,
        outcome="done",
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
        generated_at=_NOW,
        summary="attempt completed",
    )
    _append_report(state_path, AgentSessionRole.EXECUTOR, report_id, base_id, body, header)


def _write_researcher_report(state_path: Path, *, base_id: str) -> None:
    """Append one researcher report envelope keyed by ``base_id`` to the store."""
    body = ResearcherReportBody(
        role="researcher",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="survey complete",
        question="how does quantized attention affect recall?",
        recommendation="adopt 8-bit KV cache",
        evidence_refs=[AgentReportEvidenceRef(kind="artifact", ref="survey.md")],
    )
    report_id = f"AR-researcher-{base_id}-01"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.RESEARCHER,
        session_id="RES-1",
        scope_id=base_id,
        base_id=base_id,
        attempt=1,
        runtime="claude",
        generated_at=_NOW,
        summary="survey complete",
    )
    _append_report(state_path, AgentSessionRole.RESEARCHER, report_id, base_id, body, header)


def _append_report(
    state_path: Path,
    role: AgentSessionRole,
    report_id: str,
    base_id: str,
    body: ExecutorReportBody | ResearcherReportBody,
    header: AgentReportHeader,
) -> None:
    """Write one report envelope to the role store under *state_path*."""
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=_NOW + timedelta(minutes=header.attempt),
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(role))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


def _write_campaign(state_path: Path, *, campaign_id: str, topic: str) -> None:
    """Append one research-campaign envelope to the campaign store."""
    payload = ResearchCampaignPayload(
        campaign_id=campaign_id,
        config=ResearchProfileBlock(),
        campaign=StagedCampaign(topic=topic, dispatches=[]),
        status=CampaignStatus.ACTIVE,
        tombstone=None,
    )
    envelope = Envelope(
        id=campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id=campaign_id,
        created_at=_NOW,
        updated_at=None,
        summary=topic,
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


# --------------------------------------------------------------------------
# Criterion 1 -- campaign topic labels + role column split
# --------------------------------------------------------------------------


def test_build_evidence_rows_labels_campaign_report_by_topic(tmp_path: Path) -> None:
    """A campaign-scoped researcher report labels by its topic, not the raw id."""
    state = _empty_state()
    state_path = _write_state(state, tmp_path)
    _write_campaign(state_path, campaign_id=_CAMPAIGN_ID, topic=_TOPIC)
    _write_researcher_report(state_path, base_id=_CAMPAIGN_ID)

    rows = build_evidence_rows(state_path, state)

    assert len(rows) == 1
    row = rows[0]
    assert row.role == "researcher"
    assert row.campaign_topic == _TOPIC
    # The subject label is the topic; the raw campaign uuid never surfaces.
    assert row.wave_label == _TOPIC
    assert _CAMPAIGN_ID not in row.wave_label


def test_build_evidence_rows_campaign_without_store_row_keeps_raw_id(tmp_path: Path) -> None:
    """A campaign report with no store row falls back to the raw id, not a crash."""
    state = _empty_state()
    state_path = _write_state(state, tmp_path)
    _write_researcher_report(state_path, base_id=_CAMPAIGN_ID)

    rows = build_evidence_rows(state_path, state)

    assert len(rows) == 1
    assert rows[0].campaign_topic is None
    assert rows[0].wave_label == _CAMPAIGN_ID


def test_evidence_columns_split_role_into_its_own_column() -> None:
    """The table carries a dedicated role column beside report / verdict / eu."""
    assert _COLUMNS == ("report", "role", "verdict", "eu")


def test_evidence_pane_report_column_drops_fused_role_and_shows_role_column(
    tmp_path: Path,
) -> None:
    """The report column shows the bare wave label; the role column carries the role."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I21-W27", title="Fix Evidence pane")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            table = screen.query_one("#evidence-table", DataTable)
            assert [str(key.value) for key in table.columns] == ["report", "role", "verdict", "eu"]
            # The report cell is the bare wave label -- no fused ``:: executor``.
            report_cell = table.get_row_at(0)[0]
            role_cell = table.get_row_at(0)[1]
            assert report_cell == "P30-I21-W27 Fix Evidence pane"
            assert "::" not in report_cell
            assert role_cell == "executor"

    asyncio.run(body())


# --------------------------------------------------------------------------
# Criterion 2 -- inline tier ladder + Followups separation
# --------------------------------------------------------------------------


def test_render_tier_ladder_inline_is_single_row_with_every_tier() -> None:
    """The ladder renders one row carrying every T1..T7 label (no per-line split)."""
    block = render_tier_ladder(None)

    assert "\n" not in block
    for tier in OracleTier:
        assert tier_label(tier) in block
    assert "T1 static" in block
    assert "T7 jury" in block


def test_render_tier_ladder_inline_marks_only_the_scoring_tier() -> None:
    """The scoring tier carries the leading mark inline; unscored tiers do not."""
    block = render_tier_ladder(OracleTier.T4_CONTRACT)

    assert "\n" not in block
    assert "> T4 contract" in block
    # No other tier is marked -- the marker sits only against the scored label.
    assert "> T1 static" not in block
    assert "> T7 jury" not in block
    assert block.count(">") == 1


def test_evidence_followups_title_separated_from_table_by_margin(tmp_path: Path) -> None:
    """The Followups title carries a top margin so it reads apart from the table."""

    async def body() -> None:
        state_path = _write_state(_empty_state(), tmp_path)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            title = screen.query_one(".evidence-followups-title", Static)
            assert title.styles.margin.top == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# Criterion 3 -- focus on mount, live p / Enter keys, failed-wave verdict mark
# --------------------------------------------------------------------------


def test_evidence_report_table_focused_on_mount(tmp_path: Path) -> None:
    """The populated report table receives focus on mount (arrow keys are live)."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I21-W27")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)
        _write_executor_report(
            state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS, attempt=2
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            assert screen.query_one("#evidence-table", DataTable).has_focus is True

    asyncio.run(body())


def test_evidence_arrow_key_moves_report_selection(tmp_path: Path) -> None:
    """With the table focused, Down moves the report selection off row 0."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I21-W27")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)
        _write_executor_report(
            state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS, attempt=2
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            table = app.screen.query_one("#evidence-table", DataTable)
            assert table.row_count == 2
            assert table.cursor_coordinate.row == 0
            await pilot.press("down")
            await settle_screen(pilot)
            assert table.cursor_coordinate.row == 1

    asyncio.run(body())


def test_evidence_enter_opens_report_detail_after_auto_focus(tmp_path: Path) -> None:
    """Enter over the auto-focused table opens the report-detail modal (visible)."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I21-W27")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press("enter")
            await settle_screen(pilot)
            assert app.modal_depth() == depth_before + 1
            assert isinstance(app.screen, ReportDetailModal)

    asyncio.run(body())


def test_evidence_p_peek_opens_drill_when_readiness_bound(tmp_path: Path) -> None:
    """p peeks into the selected ledger criterion (visible drill modal)."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I21-W27")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            screen.set_readiness(
                CloseReadiness(
                    ready=False,
                    criteria=[
                        CriterionView(
                            id="CR-01",
                            source="spec",
                            status="pass",
                            gate_results=[GateResult(gate_id="G-01", status="pass")],
                        ),
                    ],
                )
            )
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press("p")
            await settle_screen(pilot)
            assert app.modal_depth() == depth_before + 1
            assert isinstance(app.screen, EvidenceDrillModal)

    asyncio.run(body())


def test_verdict_cell_failed_wave_appends_failed_cross() -> None:
    """A pass on a terminal-failed wave trails the failed cross, unqualified pass does not."""
    clean = verdict_cell("pass", wave_failed=False, mode="unicode")
    qualified = verdict_cell("pass", wave_failed=True, mode="unicode")

    # A non-failed wave renders the bare verdict sigil unchanged.
    assert clean.plain == verdict_sigil("pass", mode="unicode").plain
    # A failed wave leads with the same pass sigil but trails the failed cross.
    assert qualified.plain.startswith(glyph(Sigil.CLOSED, mode="unicode"))
    assert glyph(Sigil.FAILED, mode="unicode") in qualified.plain
    assert qualified.plain != clean.plain


def test_verdict_cell_failed_wave_ascii_mode_uses_ascii_column() -> None:
    """The failed cross-mark honours the ASCII render column."""
    qualified = verdict_cell("pass", wave_failed=True, mode="ascii")

    assert glyph(Sigil.FAILED, mode="ascii") in qualified.plain
    assert qualified.plain.startswith(glyph(Sigil.CLOSED, mode="ascii"))


def test_build_evidence_rows_marks_failed_wave(tmp_path: Path) -> None:
    """A report joined to a terminal-failed wave carries the failed flag."""
    state = _state_with_wave(wave_id="P30-I21-W27", status=WaveStatus.FAILED)
    state_path = _write_state(state, tmp_path)
    _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)

    rows = build_evidence_rows(state_path, state)

    assert len(rows) == 1
    assert rows[0].wave_failed is True


def test_build_evidence_rows_closed_wave_not_marked_failed(tmp_path: Path) -> None:
    """A cleanly-closed wave carries no failed flag (the common path)."""
    state = _state_with_wave(wave_id="P30-I21-W27", status=WaveStatus.CLOSED)
    state_path = _write_state(state, tmp_path)
    _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)

    rows = build_evidence_rows(state_path, state)

    assert rows[0].wave_failed is False


def test_evidence_pane_failed_wave_pass_renders_cross_in_frame(tmp_path: Path) -> None:
    """A self-claimed pass on a FAILED wave shows the failed cross in the frame."""

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I21-W27", status=WaveStatus.FAILED)
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I21-W27", verdict=AgentReportVerdict.PASS)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The pass sigil is present, but so is the failed cross qualifying it.
            assert glyph(Sigil.CLOSED, mode=app.render_mode) in frame
            assert glyph(Sigil.FAILED, mode=app.render_mode) in frame

    asyncio.run(body())
