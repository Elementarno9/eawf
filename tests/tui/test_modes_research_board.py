"""Tests for the Research mode pane over the ResearchCampaign store (W13).

The Research mode (digit ``7``) renders the research-campaign overview for
the active scope: the staged campaigns persisted under the per-scope
``research_campaign`` store, the state-resident
:class:`~eawf.kernel.state.models.Claim` ledger (with each claim's
:class:`~eawf.kernel.state.enums.ClaimStatus`), and the state-resident
:class:`~eawf.kernel.state.models.OpenQuestion` list (with each question's
:class:`~eawf.kernel.state.enums.OpenQuestionStatus` and the blocking flag).
These tests pin the two halves:

* the pure render helpers (one per board section), the
  :func:`~eawf.surfaces.tui.modes.research_board.has_research_signal`
  predicate, and the store reader
  :func:`~eawf.surfaces.tui.modes.research_board.read_campaign_rows`, tested
  against directly-built rows / on-disk stores so the composition is
  verified without mounting Textual; and
* the mounted pane under a Pilot: digit ``7`` switches to the mode and the
  breadcrumb leads with the ``Research`` segment; an honest-empty scope (no
  campaign, no claim, no question) renders the "no active research campaign"
  banner; a seeded scope (a staged campaign plus claims + open questions)
  surfaces the campaign topic, the claim title, and the open-question title.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before
asserting (``pilot.pause()`` is CPU-idle-based, not worker-aware).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    ClaimStatus,
    OpenQuestionStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
)
from eawf.kernel.state.models import (
    Claim,
    CurrentPointers,
    OpenQuestion,
    Project,
    State,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.research_board import (
    EMPTY_NOTICE,
    NONE_YET,
    CampaignRow,
    ResearchBoardModeScreen,
    has_research_signal,
    read_campaign_rows,
    render_campaigns,
    render_claims,
    render_open_questions,
    render_overview,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Row / state builders
# --------------------------------------------------------------------------


def _campaign_row(campaign_id: str = "RC-0001") -> CampaignRow:
    """Build a directly-constructed campaign row for the render helpers."""
    return CampaignRow(
        campaign_id=campaign_id,
        topic="Survey the options-pricing landscape",
        domains=("market-structure", "pricing-models"),
        default_depth="medium",
    )


def _claim(claim_id: str = "CL-0001", *, status: ClaimStatus = ClaimStatus.OPEN) -> Claim:
    """Build a claim row in *status*."""
    return Claim(
        id=claim_id,
        scope_id="QR",
        title="Implied vol surface is downward sloping in strike",
        status=status,
        created_at=_T0,
    )


def _question(
    question_id: str = "OQ-0001",
    *,
    status: OpenQuestionStatus = OpenQuestionStatus.OPEN,
    blocking: bool = False,
) -> OpenQuestion:
    """Build an open-question row in *status*."""
    return OpenQuestion(
        id=question_id,
        scope_id="QR",
        title="Which curve model fits the short tenor",
        status=status,
        blocking=blocking,
        created_at=_T0,
    )


def _campaign_payload(campaign_id: str = "RC-0001") -> ResearchCampaignPayload:
    """Stage a two-domain campaign and wrap it in a persistable payload."""
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )
    campaign = stage_campaign("Survey the options-pricing landscape", block)
    return ResearchCampaignPayload(campaign_id=campaign_id, config=block, campaign=campaign)


def _project_state(
    *,
    claims: dict[str, Claim] | None = None,
    open_questions: dict[str, OpenQuestion] | None = None,
) -> State:
    """Build a minimal repo state, optionally with claims + open questions."""
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
            "claims": (
                {cid: c.model_dump(mode="json") for cid, c in claims.items()}
                if claims is not None
                else None
            ),
            "open_questions": (
                {qid: q.model_dump(mode="json") for qid, q in open_questions.items()}
                if open_questions is not None
                else None
            ),
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


def _append_campaign(state_path: Path, payload: ResearchCampaignPayload) -> None:
    """Append *payload* as a campaign-store record under *state_path*."""
    envelope = Envelope(
        id=payload.campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id="QR",
        created_at=_T0,
        updated_at=_T0,
        summary=f"campaign {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(envelope.model_dump_json().encode("utf-8") + b"\n")


# --------------------------------------------------------------------------
# has_research_signal -- the honesty predicate (boundary cases)
# --------------------------------------------------------------------------


def test_has_research_signal_false_when_all_empty() -> None:
    """No campaign, no claim, and no question is no signal (honest-empty)."""
    assert has_research_signal((), (), ()) is False


def test_has_research_signal_true_when_only_campaign_present() -> None:
    """A staged campaign alone lifts the empty verdict."""
    assert has_research_signal((_campaign_row(),), (), ()) is True


def test_has_research_signal_true_when_only_claim_present() -> None:
    """A logged claim alone lifts the empty verdict."""
    assert has_research_signal((), (_claim(),), ()) is True


def test_has_research_signal_true_when_only_question_present() -> None:
    """An open question alone lifts the empty verdict."""
    assert has_research_signal((), (), (_question(),)) is True


# --------------------------------------------------------------------------
# read_campaign_rows -- the store reader (boundary + populated)
# --------------------------------------------------------------------------


def test_read_campaign_rows_none_state_path_returns_empty() -> None:
    """A ``None`` state path yields no campaign rows (user scope path)."""
    assert read_campaign_rows(None) == ()


def test_read_campaign_rows_missing_store_returns_empty(tmp_path: Path) -> None:
    """A scope with no campaign store on disk yields no rows (common path)."""
    state_path = _write_state(tmp_path, _project_state())
    assert read_campaign_rows(state_path) == ()


def test_read_campaign_rows_populated_projects_campaign(tmp_path: Path) -> None:
    """A persisted campaign record projects topic + domains + default depth."""
    state_path = _write_state(tmp_path, _project_state())
    _append_campaign(state_path, _campaign_payload())
    rows = read_campaign_rows(state_path)
    assert len(rows) == 1
    row = rows[0]
    assert row.campaign_id == "RC-0001"
    assert row.topic == "Survey the options-pricing landscape"
    assert row.domains == ("market-structure", "pricing-models")
    assert row.default_depth == "medium"
    assert row.domain_count == 2


# --------------------------------------------------------------------------
# Pure render helpers -- empty renders honest-negative, populated surfaces rows
# --------------------------------------------------------------------------


def test_render_overview_empty_shows_honest_empty_banner() -> None:
    """The empty overview leads with the no-active-campaign banner."""
    body = render_overview((), (), ())
    assert EMPTY_NOTICE in body
    # No fabricated counts on the empty path.
    assert "campaigns 0" not in body


def test_render_overview_populated_shows_counts() -> None:
    """The populated overview reports campaign, claim, and question counts."""
    body = render_overview((_campaign_row(),), (_claim(),), (_question(),))
    assert "campaigns 1" in body
    assert "claims 1" in body
    assert "open questions 1" in body
    assert EMPTY_NOTICE not in body


def test_render_overview_surfaces_blocking_count() -> None:
    """A blocking open question surfaces a blocking count in the overview."""
    questions = (_question(blocking=True), _question("OQ-0002", blocking=False))
    body = render_overview((), (), questions)
    assert "blocking 1" in body


def test_render_campaigns_empty_renders_none_yet() -> None:
    """An empty campaign list renders the per-section none-yet sentinel."""
    assert NONE_YET in render_campaigns(())


def test_render_campaigns_populated_surfaces_topic_and_domains() -> None:
    """A campaign row surfaces its topic, default depth, and staged domains."""
    body = render_campaigns((_campaign_row(),))
    assert "Survey the options-pricing landscape" in body
    assert "medium" in body
    assert "market-structure" in body


def test_render_campaigns_caps_rows_with_overflow_count() -> None:
    """A campaign list past the cap renders a ``+N more`` overflow line."""
    rows = tuple(_campaign_row(f"RC-{index:04d}") for index in range(1, 20))
    body = render_campaigns(rows)
    assert "+7 more" in body  # 19 campaigns, cap 12 -> 7 overflow


def test_render_claims_empty_renders_none_yet() -> None:
    """An empty claim ledger renders the per-section none-yet sentinel."""
    assert NONE_YET in render_claims(())


def test_render_claims_populated_surfaces_status_and_title() -> None:
    """A claim row surfaces its lifecycle status and its title."""
    body = render_claims((_claim(status=ClaimStatus.SUPPORTED),))
    assert "supported" in body
    assert "Implied vol surface is downward sloping in strike" in body


def test_render_open_questions_empty_renders_none_yet() -> None:
    """An empty open-question list renders the per-section none-yet sentinel."""
    assert NONE_YET in render_open_questions(())


def test_render_open_questions_populated_surfaces_status_and_title() -> None:
    """An open-question row surfaces its status and its title."""
    body = render_open_questions((_question(status=OpenQuestionStatus.OPEN),))
    assert "open" in body
    assert "Which curve model fits the short tenor" in body


def test_render_open_questions_blocking_marks_the_row() -> None:
    """A blocking open question carries an inline blocking marker."""
    body = render_open_questions((_question(blocking=True),))
    assert "blocking" in body


# --------------------------------------------------------------------------
# Mounted pane -- registration, honest-empty, and populated render
# --------------------------------------------------------------------------


def test_research_board_mode_registers_on_digit_seven(tmp_path: Path) -> None:
    """Digit ``7`` switches to the Research mode and leads the breadcrumb.

    Pins the registry wiring: the new ModeSpec row registers the mode under
    digit ``7`` (the next free digit), so the digit key switches to a
    :class:`ResearchBoardModeScreen` and the header breadcrumb leads with the
    ``Research`` segment derived from the registry title.
    """
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("7")  # -> research_board
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert "Research" in header_row

    asyncio.run(body())


def test_research_board_pane_renders_honest_empty(tmp_path: Path) -> None:
    """A scope with no campaign / claim / question renders the empty banner.

    The load-bearing honesty assertion: a scope with no research signal must
    show "no active research campaign" rather than a fabricated overview.
    """
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("7")  # -> research_board
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            assert app.screen.empty is True
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame

    asyncio.run(body())


def test_research_board_pane_renders_seeded_campaign(tmp_path: Path) -> None:
    """The mounted pane surfaces a seeded campaign + claims + open questions.

    Builds a scope with a persisted campaign record plus a claim and an open
    question in state, then asserts the rendered frame carries the campaign
    topic, the claim title, and the open-question title, and that the
    honest-empty banner is absent on the populated path.
    """
    state = _project_state(
        claims={"CL-0001": _claim(status=ClaimStatus.SUPPORTED)},
        open_questions={"OQ-0001": _question(blocking=True)},
    )
    state_path = _write_state(tmp_path, state)
    _append_campaign(state_path, _campaign_payload())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("7")  # -> research_board
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            assert app.screen.empty is False
            frame = normalize_snapshot(capture_screen_text(app))
            # Section headings + the populated signal are visible.
            assert "RESEARCH" in frame
            assert "Survey the options-pricing landscape" in frame  # campaign topic
            assert "market-structure" in frame  # staged domain
            assert "Implied vol surface" in frame  # claim title
            assert "Which curve model fits the short tenor" in frame  # question title
            assert "blocking" in frame  # blocking marker on the open question
            # Honest-empty banner is absent on the populated path.
            assert EMPTY_NOTICE not in frame

    asyncio.run(body())


def test_research_board_pane_keeps_chassis_brand(tmp_path: Path) -> None:
    """Even honest-empty, the Research pane keeps the shared chassis brand row."""
    from eawf.surfaces.tui.widgets.header import BRAND

    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("7")  # -> research_board
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert BRAND in header_row
            assert "Research" in header_row

    asyncio.run(body())
