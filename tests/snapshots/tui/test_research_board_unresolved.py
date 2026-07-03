"""Golden snapshot for the research_board Unresolved tab (P29-I13-W16).

The Research mode (digit ``3``) center pane carries an ``Unresolved`` tab
projecting the scope's open questions: one row per
:class:`~eawf.kernel.state.models.OpenQuestion`, each naming the question's
status and the round number it belongs to (``round N``). The board's
campaigns are staged with no live multi-round runner, so every row renders
under the synthetic
:data:`~eawf.surfaces.tui.modes.research_board.STAGED_ROUND` the topic tree
labels -- the honest pre-multi-round round number.

The pure :func:`~eawf.surfaces.tui.modes.research_board.render_unresolved`
helper is exercised directly over typed
:class:`~eawf.kernel.state.models.OpenQuestion` fixtures (one row per
question, status + round), and the mounted pane is snapshotted over a
seeded scope so a layout regression on the populated tab is caught. The
render path never calls
:func:`~eawf.workflow.verify.readiness.compute` (it spawns subprocesses);
the questions come from a typed ``tmp_path`` state.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_research_board_unresolved.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    OpenQuestionStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
)
from eawf.kernel.state.models import (
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
    CENTER_BODY_ID,
    NONE_YET,
    STAGED_ROUND,
    render_unresolved,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_GOLDEN = Path(__file__).resolve().parent / "golden"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


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


def _question(
    question_id: str,
    title: str,
    *,
    status: OpenQuestionStatus = OpenQuestionStatus.OPEN,
    blocking: bool = False,
) -> OpenQuestion:
    """Build an open-question row in *status*."""
    return OpenQuestion(
        id=question_id,
        scope_id="QR",
        title=title,
        status=status,
        blocking=blocking,
        created_at=_T0,
    )


def _three_questions() -> tuple[OpenQuestion, ...]:
    """Three open questions: open, answered, and a blocking one."""
    return (
        _question("OQ-0001", "Which curve model fits the short tenor"),
        _question(
            "OQ-0002",
            "Does the smile invert past the 90 delta wing",
            status=OpenQuestionStatus.ANSWERED,
        ),
        _question("OQ-0003", "Is the term-structure arbitrage real", blocking=True),
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


def _seeded_state(questions: tuple[OpenQuestion, ...]) -> State:
    """Build a repo state carrying the given open questions."""
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
            "claims": None,
            "open_questions": {q.id: q.model_dump(mode="json") for q in questions},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_seeded_scope(tmp_path: Path, questions: tuple[OpenQuestion, ...]) -> Path:
    """Write a seeded state + a staged campaign store, return the state path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(_seeded_state(questions).model_dump_json(), encoding="utf-8")
    payload = _campaign_payload()
    envelope = Envelope(
        id=payload.campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id="QR",
        created_at=_T0,
        summary=f"campaign {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(envelope.model_dump_json().encode("utf-8") + b"\n")
    return state_path


# --------------------------------------------------------------------------
# render_unresolved -- pure helper (one row per question + status + round)
# --------------------------------------------------------------------------


def test_render_unresolved_empty_is_none_yet() -> None:
    """An empty question ledger renders the none-yet sentinel."""
    assert render_unresolved(()) == f"[$muted]{NONE_YET}[/]"


def test_render_unresolved_one_row_per_open_question() -> None:
    """Each OPEN question renders one row; the answered one leaves the section."""
    rendered = render_unresolved(_three_questions())
    assert len(rendered.splitlines()) == 2
    assert "Does the smile invert" not in rendered


def test_render_unresolved_row_carries_status_and_round() -> None:
    """Each row names the question status and its round number."""
    rendered = render_unresolved((_question("OQ-9", "open question title"),))
    assert "open" in rendered
    assert f"round {STAGED_ROUND}" in rendered
    assert "open question title" in rendered


def test_render_unresolved_blocking_reads_blocking() -> None:
    """A blocking question reads ``blocking`` (the autonomy-interrupt signal)."""
    rendered = render_unresolved((_question("OQ-B", "gating question", blocking=True),))
    assert "blocking" in rendered
    assert f"round {STAGED_ROUND}" in rendered


def test_render_unresolved_honours_round_number_override() -> None:
    """A caller-supplied round number renders on every row."""
    rendered = render_unresolved((_question("OQ-7", "later round question"),), round_number=3)
    assert "round 3" in rendered


def test_render_unresolved_answered_question_leaves_section() -> None:
    """An answered question no longer renders under Unresolved (none-yet sentinel)."""
    rendered = render_unresolved(
        (_question("OQ-A", "resolved question", status=OpenQuestionStatus.ANSWERED),)
    )
    assert "resolved question" not in rendered
    assert NONE_YET in rendered


# --------------------------------------------------------------------------
# Snapshot: the campaign-stats center detail counts the open questions
# --------------------------------------------------------------------------


def test_research_board_unresolved_snapshot(tmp_path: Path) -> None:
    """The mounted campaign-stats center detail counts the scope's open questions.

    W03 retired the always-global Unresolved tab: the center pane now morphs by
    selection. The default campaign selection renders the campaign stats, whose
    QUESTIONS band counts the still-open + answered + blocking questions -- the
    scoped question figures, not one un-scoped question list.
    """
    questions = _three_questions()
    state_path = _write_seeded_scope(tmp_path, questions)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            from textual.widgets import Static

            detail = str(app.screen.query_one(f"#{CENTER_BODY_ID}", Static).render())
            # Three questions: two OPEN (one of them blocking), one ANSWERED.
            assert "2 open" in detail
            assert "1 answered" in detail
            assert "1 blocking" in detail
            # The stat bands render (a campaign-stats detail, not a question list).
            for band in ("ROUNDS", "QUESTIONS", "CLAIMS", "BUDGET"):
                assert band in detail
            assert_screen_snapshot(app, _GOLDEN / "research_board_unresolved.txt")

    asyncio.run(body())
