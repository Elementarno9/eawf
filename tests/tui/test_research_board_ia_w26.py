"""W26 information-architecture fixes for the Research board mode.

Gates the three P30-I21-W26 deliverables against the pure render helpers plus a
pair of Pilot drives:

* **Scope-questions hoist.** The scope-wide open-question grouping is a depth-0
  root -- a sibling of the campaigns -- so question leaves no longer render as
  children of the last campaign (:func:`build_tree_nodes`).
* **Per-campaign round status.** :class:`CampaignRow` carries its own
  :class:`~eawf.kernel.state.enums.CampaignStatus`, so a CONVERGED campaign's
  round node reads a terminal label rather than the scope-wide "round running";
  a wrapped long label hangs to the node's own text column
  (:func:`render_tree`).
* **Scoped detail + honest Unresolved.** The center pane names (and scopes to)
  the selected node -- the W03 redesign morphs it into the selection's detail,
  retiring the in-tree peek -- and an answered question leaves the Unresolved
  list (:func:`render_unresolved`). The pure ``scope_claims_for_node`` scoping
  helper is still gated here.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    CampaignStatus,
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
    CENTER_SCOPE_ID,
    NONE_YET,
    CampaignRow,
    NodeKind,
    ResearchBoardModeScreen,
    RoundState,
    TreeNode,
    _tree_prefix_width,
    build_tree_nodes,
    read_campaign_rows,
    render_tree,
    render_unresolved,
    scope_claims_for_node,
)
from eawf.surfaces.tui.snapshot import (
    settle_screen,
    toast_messages,
)

_T0 = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Row / state builders
# --------------------------------------------------------------------------


def _campaign_row(
    campaign_id: str = "RC-0001",
    *,
    topic: str = "Survey the options-pricing landscape",
    status: CampaignStatus = CampaignStatus.ACTIVE,
) -> CampaignRow:
    """Build a two-domain campaign row in *status*."""
    return CampaignRow(
        campaign_id=campaign_id,
        topic=topic,
        domains=("market-structure", "pricing-models"),
        default_depth="medium",
        status=status,
    )


def _question(
    question_id: str = "OQ-0001",
    *,
    status: OpenQuestionStatus = OpenQuestionStatus.OPEN,
    title: str = "Which curve model fits the short tenor",
) -> OpenQuestion:
    """Build an open-question row in *status*."""
    return OpenQuestion(
        id=question_id,
        scope_id="QR",
        title=title,
        status=status,
        created_at=_T0,
    )


def _claim(
    claim_id: str = "CL-0001",
    *,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    answers_question_id: str | None = None,
    title: str = "Implied vol surface is downward sloping in strike",
) -> Claim:
    """Build a claim row, optionally answering a question."""
    return Claim(
        id=claim_id,
        scope_id="QR",
        title=title,
        status=status,
        answers_question_id=answers_question_id,
        created_at=_T0,
    )


def _campaign_payload(
    campaign_id: str = "RC-0001",
    *,
    topic: str = "Survey the options-pricing landscape",
    status: CampaignStatus = CampaignStatus.ACTIVE,
) -> ResearchCampaignPayload:
    """Stage a two-domain campaign payload in *status* for the store."""
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(),
            "pricing-models": ResearchDomainConfig(),
        },
    )
    campaign = stage_campaign(topic, block)
    return ResearchCampaignPayload(
        campaign_id=campaign_id, config=block, campaign=campaign, status=status
    )


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


def _strip_markup(text: str) -> str:
    """Drop Textual content-markup tags so column indexes read as visual columns."""
    return re.sub(r"\[/?[^\]]*\]", "", text)


# --------------------------------------------------------------------------
# Criterion 1: the scope-questions grouping hoists to depth 0
# --------------------------------------------------------------------------


def test_build_tree_nodes_nests_scope_questions_under_primary_campaign() -> None:
    """The scope-wide questions nest under the PRIMARY campaign (W02 campaign tree).

    W02 made each campaign a pickable root that owns the research gaps: with two
    campaigns plus scope-wide questions, the questions hang at depth 1 under the
    FIRST (primary) campaign's sub-tree -- there is no separate depth-0 scope
    round when a campaign is staged. (Supersedes the retired W26 depth-0 hoist.)
    """
    campaigns = (
        _campaign_row("RC-0001", topic="Campaign ABC"),
        _campaign_row("RC-0002", topic="Campaign DEF"),
    )
    questions = (_question("OQ-0001"), _question("OQ-0002", title="Second scope question"))
    nodes = build_tree_nodes(campaigns, questions)

    # No scope-level questions round is emitted when a campaign is staged.
    assert not any(node.kind is NodeKind.ROUND and node.campaign_id is None for node in nodes)
    # The two questions nest at depth 1, after the primary (first) campaign root
    # and before the second campaign root.
    question_nodes = [node for node in nodes if node.kind is NodeKind.QUESTION]
    assert len(question_nodes) == 2
    for node in question_nodes:
        assert node.depth == 1
    primary_index = next(i for i, node in enumerate(nodes) if node.kind is NodeKind.CAMPAIGN)
    second_index = next(
        i for i, node in enumerate(nodes) if node.kind is NodeKind.CAMPAIGN and i > primary_index
    )
    question_indexes = [nodes.index(node) for node in question_nodes]
    assert all(primary_index < idx < second_index for idx in question_indexes)


def test_build_tree_nodes_question_only_scope_roots_the_group_at_depth_zero() -> None:
    """A question-only scope roots the questions group at depth 0 (no campaign)."""
    nodes = build_tree_nodes((), (_question(),))
    assert nodes[0].kind is NodeKind.ROUND
    assert nodes[0].depth == 0
    assert nodes[1].kind is NodeKind.QUESTION
    assert nodes[1].depth == 1


# --------------------------------------------------------------------------
# Criterion 2: per-campaign round status + hanging-indent wrap
# --------------------------------------------------------------------------


def test_read_campaign_rows_carries_each_campaign_status(tmp_path: Path) -> None:
    """A CONVERGED campaign's own status survives onto its :class:`CampaignRow`."""
    state_path = _write_state(tmp_path, _project_state())
    _append_campaign(state_path, _campaign_payload("RC-0001", status=CampaignStatus.ACTIVE))
    _append_campaign(
        state_path,
        _campaign_payload("RC-0002", topic="Converged sweep", status=CampaignStatus.CONVERGED),
    )
    rows = {row.campaign_id: row for row in read_campaign_rows(state_path)}
    assert rows["RC-0001"].status is CampaignStatus.ACTIVE
    assert rows["RC-0002"].status is CampaignStatus.CONVERGED


def test_build_tree_nodes_classifies_each_campaign_round_individually() -> None:
    """A CONVERGED campaign reads a terminal round; a running one keeps running.

    With one CONVERGED and one ACTIVE campaign plus an open scope question, the
    converged campaign's round node reads a terminal ``round converged`` label
    (saturated sigil) while the active campaign's round still reads
    ``round running`` -- the fix for a converged campaign that lingered "round
    running" forever off the once-computed scope-wide state.
    """
    campaigns = (
        _campaign_row("RC-0001", topic="Active sweep", status=CampaignStatus.ACTIVE),
        _campaign_row("RC-0002", topic="Converged sweep", status=CampaignStatus.CONVERGED),
    )
    nodes = build_tree_nodes(campaigns, (_question(status=OpenQuestionStatus.OPEN),))

    def _campaign_round(campaign_id: str) -> TreeNode:
        return next(
            node
            for node in nodes
            if node.kind is NodeKind.ROUND and node.campaign_id == campaign_id
        )

    active_round = _campaign_round("RC-0001")
    converged_round = _campaign_round("RC-0002")

    assert active_round.label == "round running"
    assert active_round.round_state is RoundState.RUNNING

    assert converged_round.label == "round converged"
    assert converged_round.label != "round running"
    assert converged_round.round_state is RoundState.SATURATED


def test_render_tree_wraps_long_label_with_hanging_indent() -> None:
    """A wrapped label hangs its continuation lines to the node's text column."""
    long_label = "an unusually long staged topic title that must soft wrap across lines"
    node = TreeNode(
        kind=NodeKind.TOPIC,
        label=long_label,
        depth=2,
        detail="",
    )
    body = render_tree((node,), -1, width=28)
    lines = body.split("\n")
    # The narrow width forces at least one continuation line.
    assert len(lines) >= 2
    label_col = _tree_prefix_width(2)  # depth-2 label opens at column 8
    head = _strip_markup(lines[0])
    # The head line's label opens at the node's text column (a non-space there).
    assert head[label_col] != " "
    for continuation in lines[1:]:
        stripped = _strip_markup(continuation)
        # Each continuation hangs to exactly the node's text column, never
        # orphaning flush to column zero.
        assert stripped[:label_col] == " " * label_col
        assert stripped[label_col] != " "


def test_render_tree_zero_width_keeps_single_line() -> None:
    """The pure-render default (width 0) never splits a label."""
    node = TreeNode(
        kind=NodeKind.TOPIC, label="a long label here that would wrap", depth=1, detail=""
    )
    body = render_tree((node,), -1, width=0)
    assert "\n" not in body


# --------------------------------------------------------------------------
# Criterion 3: scoped claims, honest Unresolved, visible peek
# --------------------------------------------------------------------------


def test_scope_claims_for_node_scopes_to_selected_question() -> None:
    """Selecting a question scopes the Claims pane to the claims answering it."""
    q1_claim = _claim("CL-0001", answers_question_id="OQ-0001", title="Answers Q1")
    q2_claim = _claim("CL-0002", answers_question_id="OQ-0002", title="Answers Q2")
    free = _claim("CL-0003", answers_question_id=None, title="Free-standing")
    claims = (q1_claim, q2_claim, free)
    node = TreeNode(
        kind=NodeKind.QUESTION,
        label="Which curve model fits the short tenor",
        depth=1,
        detail="",
        question_status=OpenQuestionStatus.OPEN,
        question_id="OQ-0001",
    )
    scoped, header = scope_claims_for_node(node, claims)
    assert scoped == (q1_claim,)
    assert "claims answering:" in header
    assert "Which curve model" in header


def test_scope_claims_for_node_campaign_shows_all_claims_with_header() -> None:
    """A campaign selection keeps the full ledger but headers the selection."""
    claims = (_claim("CL-0001"), _claim("CL-0002", title="Second claim"))
    node = TreeNode(kind=NodeKind.CAMPAIGN, label="Survey the landscape", depth=0, detail="")
    scoped, header = scope_claims_for_node(node, claims)
    assert scoped == claims
    assert "Survey the landscape" in header


def test_scope_claims_for_node_no_selection_is_scope_wide() -> None:
    """No selection renders the full ledger under the scope-wide header."""
    claims = (_claim("CL-0001"),)
    scoped, header = scope_claims_for_node(None, claims)
    assert scoped == claims
    assert "scope-wide" in header


def test_render_unresolved_drops_answered_and_dropped_questions() -> None:
    """An answered / dropped question leaves the Unresolved section."""
    questions = (
        _question("OQ-0001", status=OpenQuestionStatus.OPEN, title="Still open question"),
        _question("OQ-0002", status=OpenQuestionStatus.ANSWERED, title="Resolved answer question"),
        _question("OQ-0003", status=OpenQuestionStatus.DROPPED, title="Abandoned question"),
    )
    body = render_unresolved(questions)
    assert "Still open question" in body
    # The resolved rows are gone -- Unresolved never lists an "answered" row.
    assert "Resolved answer question" not in body
    assert "Abandoned question" not in body
    assert "answered" not in body
    assert "dropped" not in body


def test_render_unresolved_all_resolved_renders_none_yet() -> None:
    """With every question resolved, the Unresolved section is honest-empty."""
    questions = (
        _question("OQ-0001", status=OpenQuestionStatus.ANSWERED),
        _question("OQ-0002", status=OpenQuestionStatus.DROPPED),
    )
    assert NONE_YET in render_unresolved(questions)


def test_research_board_in_tree_peek_is_retired(tmp_path: Path) -> None:
    """The in-tree peek Static is retired -- selection drives the center detail.

    W03 replaced the ``#research-peek`` line with the polymorphic center detail,
    so the tree pane carries no peek Static and Enter no longer renders a peek
    block. The center detail changes as the selection moves instead.
    """
    state = _project_state(open_questions={"OQ-0001": _question()})
    state_path = _write_state(tmp_path, state)
    _append_campaign(state_path, _campaign_payload())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            # No peek Static remains in the tree pane.
            assert not pane.query("#research-peek")
            tree_before = str(pane.query_one("#research-tree-body").render())  # type: ignore[attr-defined]
            await pilot.press("enter")  # the retired-peek seam -- a no-op for W03
            await settle_screen(pilot)
            tree_after = str(pane.query_one("#research-tree-body").render())  # type: ignore[attr-defined]
            # Enter accreted no block inside the tree.
            assert tree_after == tree_before

    asyncio.run(body())


def test_research_board_center_scope_header_names_selection(tmp_path: Path) -> None:
    """The Claims pane carries a header naming the selection driving it."""
    state = _project_state(
        claims={"CL-0001": _claim()},
        open_questions={"OQ-0001": _question()},
    )
    state_path = _write_state(tmp_path, state)
    _append_campaign(state_path, _campaign_payload())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            header = str(pane.query_one(f"#{CENTER_SCOPE_ID}").render())  # type: ignore[attr-defined]
            # The default selection is the first campaign node, so the pane
            # headers "showing" the scope it is driving.
            assert "showing" in header

    asyncio.run(body())


def test_research_board_selection_morphs_center_detail(tmp_path: Path) -> None:
    """Moving the cursor morphs the center detail (replaces the retired peek).

    Selection now drives the center pane: the campaign root shows campaign
    stats, and walking the cursor to a question node morphs the detail into that
    question's answer view -- a visible change in the center-body render, with no
    peek toast flashed.
    """
    state = _project_state(open_questions={"OQ-0001": _question()})
    state_path = _write_state(tmp_path, state)
    _append_campaign(state_path, _campaign_payload())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            campaign_detail = str(pane.query_one("#research-center-body").render())  # type: ignore[attr-defined]
            assert "ROUNDS" in campaign_detail  # the campaign-stats view
            for _ in range(len(pane._tree)):  # type: ignore[attr-defined]
                if pane._selected_node().kind is NodeKind.QUESTION:  # type: ignore[attr-defined]
                    break
                await pilot.press("down")
            await settle_screen(pilot)
            question_detail = str(pane.query_one("#research-center-body").render())  # type: ignore[attr-defined]
            # The center morphed -- a visible change, and the question answer view.
            assert question_detail != campaign_detail
            assert "ANSWER" in question_detail
            # No peek toast flashed (the in-tree peek is retired).
            assert "peek" not in "\n".join(toast_messages(app))

    asyncio.run(body())
