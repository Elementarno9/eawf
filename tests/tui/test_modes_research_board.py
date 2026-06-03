"""Tests for the Research mode 3-pane orchestrator over the campaign engine (W08).

The Research mode (digit ``3``) renders the ratified three-pane orchestrator:
the campaign topic tree (left), the claims / evidence tabs (center), and the
progress / budget bands (right), above a bottom checkpoint drawer. These tests
pin the two halves:

* the pure render / projection helpers -- :func:`has_research_signal`,
  :func:`read_campaign_rows`, :func:`build_tree_nodes`, :func:`render_tree`,
  :func:`render_center_tabs`, :func:`render_claims`, :func:`render_progress`,
  and :func:`render_checkpoint` -- tested against directly-built rows / on-disk
  stores so the composition is verified without mounting Textual; and
* the mounted pane under a Pilot: digit ``3`` switches to the mode and the
  breadcrumb trails with the ``Research`` segment; an honest-empty scope (no
  campaign, no claim, no question) renders the "no active research campaign"
  banner (the load-bearing regression guard); a seeded scope (a staged
  campaign plus claims + open questions) mounts the three panes + the drawer
  and surfaces the topic tree, claims, and progress bands from the engine
  stores; the ``enter`` / ``a`` / ``p`` / ``r`` / ``s`` keys are bound and
  invoke their actions; ``a`` / ``p`` with no checkpoint surface the honest
  no-checkpoint line and issue no RPC, and with a stubbed daemon issue the real
  ``needs_user.resolve`` / ``needs_user.park`` RPCs; ``r`` / ``s`` surface the
  honest "not yet wired" line (the idle-contract pattern).

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before
asserting (``pilot.pause()`` is CPU-idle-based, not worker-aware).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import MarkdownViewer
from textual.widgets.markdown import Markdown, MarkdownBlock, MarkdownTableOfContents

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
from eawf.platform.artifacts.references import Citation
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.brief_viewer import (
    BRIEF_EMPTY_MARKDOWN,
    BRIEF_VIEWER_ID,
    BriefMarkdownViewer,
    BriefViewerScreen,
    build_brief_preview_markdown,
)
from eawf.surfaces.tui.modes.research_board import (
    ACTION_RESULT_ID,
    APPROVE_NO_CHECKPOINT,
    CENTER_PANE_ID,
    CENTER_TABS,
    CHECKPOINT_IDLE,
    DRAWER_ID,
    EMPTY_ID,
    EMPTY_NOTICE,
    NONE_YET,
    PARK_NO_CHECKPOINT,
    PEEK_RESULT_ID,
    PROGRESS_PANE_ID,
    TREE_PANE_ID,
    CampaignRow,
    NodeKind,
    ResearchBoardModeScreen,
    build_tree_nodes,
    has_research_signal,
    read_campaign_rows,
    render_center_tabs,
    render_checkpoint,
    render_claims,
    render_progress,
    render_tree,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import OpenPause, record_pause

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


def _pause(pause_urn: str = "urn:eawf:v1:event:QR/needs-user-abc") -> OpenPause:
    """Build an in-memory open pause for the checkpoint-drawer render helper."""
    question = UserQuestion(
        question="Resolve C3 contradiction",
        options=[
            UserQuestionOption(label="discriminator task"),
            UserQuestionOption(label="accept stronger"),
            UserQuestionOption(label="park"),
        ],
    )
    return OpenPause(
        pause_urn=pause_urn,
        scope_id="QR",
        session="urn:eawf:v1:session:QR/s1",
        question=question,
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


def _seed_pause(state_path: Path) -> str:
    """Record an open ``needs_user`` pause for the scope and return its urn."""
    question = UserQuestion(
        question="Resolve C3 contradiction",
        options=[
            UserQuestionOption(label="accept stronger"),
            UserQuestionOption(label="park"),
        ],
    )
    return record_pause(
        state_path,
        scope_id="QR",
        session="urn:eawf:v1:session:QR/s1",
        question=question,
    )


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
# read_campaign_rows -- the store reader (boundary + populated, preserved)
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
# build_tree_nodes -- the campaign > round > topic > question outline
# --------------------------------------------------------------------------


def test_build_tree_nodes_empty_when_no_signal() -> None:
    """No campaign and no question yields an empty tree node list."""
    assert build_tree_nodes((), ()) == ()


def test_build_tree_nodes_campaign_emits_campaign_round_topic_levels() -> None:
    """A staged campaign emits a campaign node, a round node, and topic nodes."""
    nodes = build_tree_nodes((_campaign_row(),), ())
    kinds = [node.kind for node in nodes]
    assert kinds[0] is NodeKind.CAMPAIGN
    assert kinds[1] is NodeKind.ROUND
    # Two staged domains -> two topic nodes after the round node.
    assert kinds[2] is NodeKind.TOPIC
    assert kinds[3] is NodeKind.TOPIC
    topic_labels = [node.label for node in nodes if node.kind is NodeKind.TOPIC]
    assert "market-structure" in topic_labels
    assert "pricing-models" in topic_labels


def test_build_tree_nodes_open_question_is_a_leaf() -> None:
    """An open question hangs as a question leaf node in the tree."""
    nodes = build_tree_nodes((), (_question(),))
    assert len(nodes) == 1
    assert nodes[0].kind is NodeKind.QUESTION
    assert nodes[0].label == "Which curve model fits the short tenor"


def test_persisted_campaign_surfaces_as_board_topic_node(tmp_path: Path) -> None:
    """A campaign persisted via the shared helper surfaces in the topic tree.

    Exercises the P29-I09-W07 deliverable end to end: the same
    ``persist_campaign`` helper the ``research.create_campaign`` RPC + the
    ``eawf research campaign new`` offline fallback share writes the campaign
    row, and the board's ``read_campaign_rows`` + ``build_tree_nodes`` lift it
    into a campaign node with its staged-domain topic children.
    """
    from eawf.runtime.daemon.methods.research import persist_campaign

    state_path = _write_state(tmp_path, _project_state())
    persist_campaign(state_path, _campaign_payload("campaign-board"))

    rows = read_campaign_rows(state_path)
    assert len(rows) == 1
    assert rows[0].campaign_id == "campaign-board"

    nodes = build_tree_nodes(rows, ())
    campaign_nodes = [node for node in nodes if node.kind is NodeKind.CAMPAIGN]
    assert len(campaign_nodes) == 1
    assert campaign_nodes[0].label == "Survey the options-pricing landscape"
    topic_labels = [node.label for node in nodes if node.kind is NodeKind.TOPIC]
    assert topic_labels == ["market-structure", "pricing-models"]


# --------------------------------------------------------------------------
# Pure render helpers -- empty renders honest-negative, populated surfaces rows
# --------------------------------------------------------------------------


def test_render_tree_empty_renders_none_yet() -> None:
    """An empty node list renders the per-pane none-yet sentinel."""
    assert NONE_YET in render_tree((), -1)


def test_render_tree_populated_surfaces_nodes_and_marks_selection() -> None:
    """A populated tree renders its nodes and marks the selected row."""
    nodes = build_tree_nodes((_campaign_row(),), (_question(),))
    body = render_tree(nodes, 0)
    assert "Survey the options-pricing landscape" in body  # campaign node
    assert "market-structure" in body  # topic node
    assert "Which curve model fits the short tenor" in body  # question leaf


def test_render_center_tabs_highlights_active_and_lists_all() -> None:
    """The center tab bar lists every ratified tab and highlights the active one."""
    body = render_center_tabs("Claims")
    for label in CENTER_TABS:
        assert label in body
    # The active tab is wrapped in the accent var; the others in muted.
    assert "[$accent][Claims][/]" in body
    assert "[$muted][Options][/]" in body


def test_render_claims_empty_renders_none_yet() -> None:
    """An empty claim ledger renders the per-pane none-yet sentinel."""
    assert NONE_YET in render_claims(())


def test_render_claims_populated_surfaces_status_and_title() -> None:
    """A claim row surfaces its lifecycle status and its title."""
    body = render_claims((_claim(status=ClaimStatus.SUPPORTED),))
    assert "supported" in body
    assert "Implied vol surface is downward sloping in strike" in body


def test_render_claims_caps_rows_with_overflow_count() -> None:
    """A claim ledger past the cap renders a ``+N more`` overflow line."""
    claims = tuple(_claim(f"CL-{index:04d}") for index in range(1, 20))
    body = render_claims(claims)
    assert "+7 more" in body  # 19 claims, cap 12 -> 7 overflow


def test_render_progress_surfaces_run_round_and_budget_bands() -> None:
    """The progress pane renders the RUN / ROUND / BUDGET bands honestly."""
    body = render_progress((_campaign_row(),), (_claim(),), (_question(),), checkpoints=1)
    assert "RUN" in body
    assert "ROUND" in body
    assert "BUDGET" in body
    # Live running is spawn-gated, so RUN reads staged (not running).
    assert "not yet wired" in body
    assert "1 checkpoint(s)" in body  # PAUSED band tracks the checkpoint count


def test_render_progress_surfaces_blocking_risk() -> None:
    """A blocking open question surfaces the RISKS band warning."""
    body = render_progress((), (), (_question(blocking=True),), checkpoints=0)
    assert "RISKS" in body
    assert "1 blocking question(s)" in body


def test_render_checkpoint_none_renders_idle_line() -> None:
    """No open pause renders the honest no-checkpoint idle line."""
    assert CHECKPOINT_IDLE in render_checkpoint(None)


def test_render_checkpoint_populated_surfaces_prompt_and_options() -> None:
    """An open pause renders its prompt and lays out its resolution options."""
    body = render_checkpoint(_pause())
    assert "Resolve C3 contradiction" in body
    assert "discriminator task" in body
    assert "accept stronger" in body
    assert "park" in body


# --------------------------------------------------------------------------
# build_brief_preview_markdown -- the brief d-tab document projection (W16)
# --------------------------------------------------------------------------


def test_build_brief_preview_markdown_empty_renders_empty_body() -> None:
    """A scope with no research signal renders the honest empty-brief body."""
    assert build_brief_preview_markdown((), (), ()) == BRIEF_EMPTY_MARKDOWN


def test_build_brief_preview_markdown_populated_has_numbered_references() -> None:
    """A populated scope renders a numbered, anchored ``## References`` block."""
    md = build_brief_preview_markdown(
        (_campaign_row(),),
        (_claim(status=ClaimStatus.SUPPORTED),),
        (_question(),),
    )
    assert "## References" in md
    # The references render as the shared numbered/anchored ordered-list shape.
    assert '1. <a id="ref-1"></a>' in md
    assert '2. <a id="ref-2"></a>' in md
    # The claim title and the campaign topic surface in the brief body.
    assert "Implied vol surface is downward sloping in strike" in md
    assert "Survey the options-pricing landscape" in md


def test_build_brief_preview_markdown_linkifies_inline_markers() -> None:
    """The brief's inline ``[N]`` summary markers are linked to their anchors."""
    md = build_brief_preview_markdown((_campaign_row(),), (_claim(),), ())
    assert r"[\[1\]](#ref-1)" in md
    assert r"[\[2\]](#ref-2)" in md


# --------------------------------------------------------------------------
# BriefViewerScreen -- MarkdownViewer + table-of-contents (W16)
# --------------------------------------------------------------------------


def test_brief_viewer_screen_mounts_markdown_viewer_with_toc(tmp_path: Path) -> None:
    """The brief viewer mounts a MarkdownViewer whose TOC renders.

    Directly pushes a :class:`BriefViewerScreen` over a populated brief body
    and asserts (after draining workers) that the MarkdownViewer mounted, the
    numbered ``## References`` list rendered into its Markdown document, and a
    MarkdownTableOfContents widget rendered (the TOC the viewer gives).
    """
    state_path = _write_state(tmp_path, _project_state())
    brief_markdown = build_brief_preview_markdown(
        (_campaign_row(),),
        (_claim(status=ClaimStatus.SUPPORTED),),
        (_question(),),
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(BriefViewerScreen(brief_markdown))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)
            # The MarkdownViewer mounted under its addressable id.
            viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", MarkdownViewer)
            # The viewer gives a table-of-contents widget.
            toc = screen.query_one(MarkdownTableOfContents)
            assert toc is not None
            # The TOC tracks the brief's headings (Summary + References).
            heading_titles = {title for _level, title, _id in viewer.document._table_of_contents}
            assert "References" in heading_titles
            assert "Summary" in heading_titles
            # The numbered references list rendered into the document source.
            assert '1. <a id="ref-1"></a>' in viewer.document.source

    asyncio.run(body())


def test_research_board_d_key_opens_brief_viewer(tmp_path: Path) -> None:
    """Pressing ``d`` on the research board opens the brief MarkdownViewer."""
    state = _project_state(
        claims={"CL-0001": _claim(status=ClaimStatus.SUPPORTED)},
        open_questions={"OQ-0001": _question()},
    )
    state_path = _write_state(tmp_path, state)
    _append_campaign(state_path, _campaign_payload())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("d")  # open the brief viewer
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)
            viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", MarkdownViewer)
            assert screen.query_one(MarkdownTableOfContents) is not None
            # The references numbered list rendered into the viewer document.
            assert "## References" in viewer.document.source

    asyncio.run(body())


def test_research_board_d_key_in_footer_hints() -> None:
    """The brief key is advertised in the footer hints (discoverable)."""
    hints = " ".join(ResearchBoardModeScreen.FOOTER_HINTS)
    assert "d brief" in hints


# --------------------------------------------------------------------------
# BriefMarkdownViewer -- Markdown.LinkClicked routing (W17)
#
# The deterministic gates for the references capstone: a #ref-N click scrolls
# the target reference row near the viewport top (asserted on scroll_y +
# on-screen offset numerically, so a coincidental overshoot fails), an entity
# href opens the typed reference card, and a #ref-N href never opens a card.
# The "fast-travel landing feel" residual the brief lists for this part is the
# (idle) band-jury's job, not a deterministic gate, so it is not asserted here.
# Worker discipline follows the project rule: settle_screen drains workers
# before each assertion so the scroll position is deterministic.
# --------------------------------------------------------------------------

#: A reference anchor on a list row that the viewer's custom scroll must reach
#: -- it is NOT a heading slug, so Textual's ``goto_anchor`` cannot resolve it,
#: which is exactly the gap W17 closes.
_REF_ANCHOR = "#ref-1"

#: An entity reference href the link catalog resolves whole to one decision ref.
_ENTITY_HREF = "urn:eawf:v1:decision:QR/D01"


def _tall_brief_markdown() -> str:
    """Build a brief whose references sit mid-document, below the first fold.

    Mirrors the real brief render shape (an inline-``[N]`` summary, then a
    numbered/anchored ``## References`` block via
    :func:`render_references` + :func:`link_inline_citations`) but pads filler
    before AND after the references so ``ref-1`` starts below the viewport top
    yet is not pinned to the document bottom -- so a top-scroll lands the row at
    the exact viewport top rather than clamping against ``max_scroll_y``.
    """
    from eawf.surfaces.render.artifact_chassis import (
        link_inline_citations,
        render_references,
    )

    pre = "\n\n".join(f"Pre filler paragraph {i} with several words of body." for i in range(40))
    post = "\n\n".join(f"Post filler paragraph {i} with several words of body." for i in range(40))
    citations = [
        Citation(n=1, ref=".ea/state.json", title="state ledger"),
        Citation(n=2, ref=".ea/store/research_campaign.jsonl", title="campaign store"),
    ]
    lines = [
        "# Brief preview",
        "",
        "## Summary",
        "",
        "intro marker [1] and second marker [2].",
        "",
        pre,
        "",
        *render_references(citations),
        "",
        post,
        "",
    ]
    return link_inline_citations("\n".join(lines))


def _reference_row_block(viewer: BriefMarkdownViewer, number: str) -> MarkdownBlock:
    """Return the rendered reference-row block whose ``[N]`` matches *number*."""
    block = viewer._reference_row_block(number)
    assert block is not None, f"reference row [{number}] not found in rendered document"
    return block


def test_brief_viewer_ref_click_scrolls_row_near_top(tmp_path: Path) -> None:
    """A ``#ref-1`` click scrolls the target row near the viewport top.

    The numeric load-bearing gate: posting ``Markdown.LinkClicked('#ref-1')``
    (the message a rendered inline ``[1]`` / row self-link click delivers) must
    leave the reference row's on-screen top within a small tolerance of the
    viewer's content top, AND must have moved the scroll toward the row (a
    strictly larger ``scroll_y`` than the initial top-of-document ``0``) -- the
    "moved toward the row" check kills an overshoot that lands the row near the
    top only by coincidence.
    """
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(BriefViewerScreen(_tall_brief_markdown()))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)
            viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", BriefMarkdownViewer)
            # The document opens at the top; ref-1 sits below the fold.
            assert viewer.scroll_y == pytest.approx(0.0)
            target = _reference_row_block(viewer, "1")
            assert target.region.y > viewer.content_region.y + 1  # below the fold
            # Deliver the click message the rendered self-link / inline marker fires.
            viewer.post_message(Markdown.LinkClicked(viewer.document, _REF_ANCHOR))
            await settle_screen(pilot)
            # Moved toward the row (not a coincidental landing at scroll_y 0).
            assert viewer.scroll_y > 0.0
            # The row's top now lands within a small band of the viewport top.
            offset = target.region.y - viewer.content_region.y
            assert 0 <= offset <= 2, f"ref-1 landed {offset} rows from the top, not near it"

    asyncio.run(body())


def test_brief_viewer_ref_click_never_opens_card(tmp_path: Path) -> None:
    """A ``#ref-N`` click scrolls but never opens a reference card.

    The screen stack stays at the brief viewer (no ``ReferenceModal`` pushed) --
    only the scroll position changes -- so the de-link contract that a citation
    jump is navigation-free holds.
    """
    from eawf.surfaces.tui.screens.overlays.reference import ReferenceModal

    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(BriefViewerScreen(_tall_brief_markdown()))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)
            depth_before = len(app.screen_stack)
            viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", BriefMarkdownViewer)
            viewer.post_message(Markdown.LinkClicked(viewer.document, _REF_ANCHOR))
            await settle_screen(pilot)
            # No card mounted; the brief viewer is still the active screen.
            assert isinstance(app.screen, BriefViewerScreen)
            assert len(app.screen_stack) == depth_before
            assert not app.query(ReferenceModal)

    asyncio.run(body())


def test_brief_viewer_entity_href_opens_typed_card(tmp_path: Path) -> None:
    """An entity-reference href routes through ``action_open_ref`` to its card.

    A click whose href is one whole EAWF reference (a decision URN here) must
    open the typed reference card: the host app's ``action_open_ref`` fires with
    the catalog-resolved ``(kind, target)`` and a ``ReferenceModal`` mounts on
    top of the brief viewer.
    """
    from eawf.surfaces.tui.screens.overlays.reference import ReferenceModal

    state_path = _write_state(tmp_path, _project_state())
    routed: list[tuple[str, str]] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        original = EaApp.action_open_ref

        def _spy(self: EaApp, kind: str, target: str) -> None:
            routed.append((kind, target))
            original(self, kind, target)

        monkey = pytest.MonkeyPatch()
        monkey.setattr(EaApp, "action_open_ref", _spy)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await settle_screen(pilot)
                await app.push_screen(BriefViewerScreen(_tall_brief_markdown()))
                await settle_screen(pilot)
                screen = app.screen
                assert isinstance(screen, BriefViewerScreen)
                viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", BriefMarkdownViewer)
                viewer.post_message(Markdown.LinkClicked(viewer.document, _ENTITY_HREF))
                await settle_screen(pilot)
                # The typed card opened on top of the brief viewer.
                assert isinstance(app.screen, ReferenceModal)
        finally:
            monkey.undo()

    asyncio.run(body())
    # The catalog resolved the href to a decision ref and routed it once.
    assert routed == [("decision", _ENTITY_HREF)]


def test_brief_viewer_external_href_is_safe_noop(tmp_path: Path) -> None:
    """A plain external href is neither a row scroll nor an entity card.

    An href the link catalog does not resolve whole (an external URL here) is a
    logged no-op: it opens no card, does not move the scroll, and -- crucially
    -- does not fall through to the base viewer's file-load branch (which would
    crash trying to read the href as a path), so a stray brief link is inert
    rather than fatal.
    """
    from eawf.surfaces.tui.screens.overlays.reference import ReferenceModal

    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(BriefViewerScreen(_tall_brief_markdown()))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)
            viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", BriefMarkdownViewer)
            viewer.post_message(Markdown.LinkClicked(viewer.document, "https://example.com"))
            await settle_screen(pilot)
            # No card; still the brief viewer; the scroll did not move.
            assert isinstance(app.screen, BriefViewerScreen)
            assert not app.query(ReferenceModal)
            assert viewer.scroll_y == pytest.approx(0.0)

    asyncio.run(body())


def test_brief_viewer_scroll_to_reference_unknown_anchor_is_noop(tmp_path: Path) -> None:
    """An unparseable / absent reference anchor scrolls nothing (no raise)."""
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(BriefViewerScreen(_tall_brief_markdown()))
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, BriefViewerScreen)
            viewer = screen.query_one(f"#{BRIEF_VIEWER_ID}", BriefMarkdownViewer)
            # A non-numeric anchor and an out-of-range row both report no-match.
            assert viewer.scroll_to_reference("ref-nope") is False
            assert viewer.scroll_to_reference("ref-99") is False
            assert viewer.scroll_y == pytest.approx(0.0)

    asyncio.run(body())


# --------------------------------------------------------------------------
# Mounted pane -- registration, honest-empty, three-pane structure, keys
# --------------------------------------------------------------------------


def test_research_board_mode_registers_on_digit_three(tmp_path: Path) -> None:
    """Digit ``3`` switches to the Research mode and trails the breadcrumb."""
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert "Research" in header_row

    asyncio.run(body())


def test_research_board_pane_renders_honest_empty(tmp_path: Path) -> None:
    """A scope with no campaign / claim / question renders the empty banner.

    The load-bearing honesty assertion: a scope with no research signal must
    show "no active research campaign" rather than a fabricated three-pane
    board (the most important regression guard for this rebuild).
    """
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            assert pane.empty is True
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame
            # The honest-empty notice is mounted; the three panes are NOT.
            assert pane.query(f"#{EMPTY_ID}")
            assert not pane.query(f"#{TREE_PANE_ID}")

    asyncio.run(body())


def test_research_board_pane_mounts_three_panes_and_drawer(tmp_path: Path) -> None:
    """A staged campaign mounts the three pane containers + the checkpoint drawer."""
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
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            assert pane.empty is False
            # The ratified 3-pane structure + drawer all mount.
            assert pane.query(f"#{TREE_PANE_ID}")
            assert pane.query(f"#{CENTER_PANE_ID}")
            assert pane.query(f"#{PROGRESS_PANE_ID}")
            assert pane.query(f"#{DRAWER_ID}")

    asyncio.run(body())


def test_research_board_pane_renders_seeded_campaign(tmp_path: Path) -> None:
    """The mounted pane surfaces a seeded campaign + claims + progress bands.

    Builds a scope with a persisted campaign record plus a claim and an open
    question in state, then asserts the rendered frame carries the campaign
    topic (tree), the claim title (center), the progress bands (right), and
    that the honest-empty banner is absent on the populated path.
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
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # Pane headings + the populated signal are visible in the frame.
            assert "TOPIC TREE" in frame
            assert "CLAIMS / EVIDENCE" in frame
            assert "PROGRESS / BUDGET" in frame
            assert "market-structure" in frame  # staged topic node (tree)
            assert "Implied vol surface" in frame  # claim title (center)
            assert "RUN" in frame  # progress band (right)
            # Honest-empty banner is absent on the populated path.
            assert EMPTY_NOTICE not in frame
            # The campaign topic renders into the tree body (the narrow pane
            # soft-wraps the long topic in the frame, so assert on the widget's
            # own renderable rather than the truncated visual frame).
            tree_body = pane.query_one("#research-tree-body")
            assert "Survey the options-pricing landscape" in str(tree_body.render())  # type: ignore[attr-defined]
            progress_body = pane.query_one("#research-progress-body")
            assert "BUDGET" in str(progress_body.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_research_board_pane_keeps_chassis_brand(tmp_path: Path) -> None:
    """Even honest-empty, the Research pane keeps the shared chassis brand row."""
    from eawf.surfaces.tui.widgets.header import BRAND

    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            header_row = frame.splitlines()[0]
            assert BRAND in header_row
            assert "Research" in header_row

    asyncio.run(body())


# --------------------------------------------------------------------------
# Bindings -- the 5 ratified action keys are bound + advertised
# --------------------------------------------------------------------------


def test_research_board_action_bindings_exist() -> None:
    """The pane binds enter / a / p / r / s (and the arrow tree-nav keys)."""
    keys = {
        binding.key: binding.action
        for binding in ResearchBoardModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("enter") == "peek_selected"
    assert keys.get("a") == "approve_checkpoint"
    assert keys.get("p") == "park_checkpoint"
    assert keys.get("r") == "followup"
    assert keys.get("s") == "snapshot"
    assert keys.get("up") == "select_prev"
    assert keys.get("down") == "select_next"


def test_research_board_action_keys_in_footer_hints() -> None:
    """The action keys are advertised in the footer hints (discoverable)."""
    hints = " ".join(ResearchBoardModeScreen.FOOTER_HINTS)
    # ``Enter`` is the canonical capitalized full key name (W22 canon).
    assert "Enter peek" in hints
    assert "a approve" in hints
    assert "p park" in hints
    assert "r follow-up" in hints
    assert "s snapshot" in hints


def test_research_board_enter_peeks_selected_node(tmp_path: Path) -> None:
    """Pressing ``enter`` peeks the selected tree node read-only (no mutation)."""
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
            await pilot.press("enter")  # peek the first (campaign) node
            await settle_screen(pilot)
            result = pane.query_one(f"#{PEEK_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "peek" in rendered
            assert "campaign" in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Checkpoint actions -- no-target honest line + real needs_user RPCs
# --------------------------------------------------------------------------


def test_research_board_approve_no_checkpoint_surfaces_honest_line(tmp_path: Path) -> None:
    """With no open checkpoint, ``a`` surfaces the honest no-checkpoint line (no RPC)."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            await pilot.press("a")  # approve with no checkpoint
            await settle_screen(pilot)
            result = pane.query_one(f"#{ACTION_RESULT_ID}")
            assert APPROVE_NO_CHECKPOINT in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_research_board_park_no_checkpoint_surfaces_honest_line(tmp_path: Path) -> None:
    """With no open checkpoint, ``p`` surfaces the honest no-checkpoint line (no RPC)."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            await pilot.press("p")  # park with no checkpoint
            await settle_screen(pilot)
            result = pane.query_one(f"#{ACTION_RESULT_ID}")
            assert PARK_NO_CHECKPOINT in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


def test_research_board_approve_issues_needs_user_resolve_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an open checkpoint + a stubbed daemon, ``a`` issues ``needs_user.resolve``.

    The approve action must reach the daemon with the open pause's urn + a
    choice from its options, and surface the honest resolved line.
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    pause_urn = _seed_pause(state_path)
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"pause_urn": pause_urn, "choice": params.get("choice"), "scope_id": "QR"}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            await pilot.press("a")  # approve the open checkpoint
            await settle_screen(pilot)
            result = pane.query_one(f"#{ACTION_RESULT_ID}")
            assert "resolved" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    assert calls and calls[0][0] == "needs_user.resolve"
    assert calls[0][1]["pause_urn"] == pause_urn
    # The approve choice is one of the pause's option labels.
    assert calls[0][1]["choice"] == "accept stronger"


def test_research_board_park_issues_needs_user_park_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an open checkpoint + a stubbed daemon, ``p`` issues ``needs_user.park``.

    The park action must reach the daemon's open-pause lister scoped to the
    pause's scope and surface the honest left-open line.
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    _seed_pause(state_path)
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"pauses": [{"pause_urn": "u", "scope_id": "QR", "session": "s"}]}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            await pilot.press("p")  # park the open checkpoint
            await settle_screen(pilot)
            result = pane.query_one(f"#{ACTION_RESULT_ID}")
            assert "left open" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    assert calls and calls[0][0] == "needs_user.park"
    assert calls[0][1]["scope_id"] == "QR"


# --------------------------------------------------------------------------
# Honest-unavailable -- r / s surface "not yet wired" (idle-contract)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "verb", "method"),
    [
        ("r", "follow-up", "research.followup"),
        ("s", "snapshot", "research.snapshot"),
    ],
)
def test_research_board_unwired_keys_surface_not_yet_wired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    verb: str,
    method: str,
) -> None:
    """follow-up / snapshot surface the honest "not yet wired" line.

    Their engine-runner RPCs do not exist, so with a reachable daemon stubbed
    to answer method-not-found the action surfaces that the method is not wired
    and never fakes the action (the idle-contract pattern).
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))

    class _MethodNotFoundClient:
        def __enter__(self) -> _MethodNotFoundClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(code=-32601, message="method not found")

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", lambda *a, **k: _MethodNotFoundClient())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            await pilot.press(key)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            result = pane.query_one(f"#{ACTION_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "not yet wired" in rendered
            assert method in rendered
            assert verb in rendered

    asyncio.run(body())


@pytest.mark.parametrize("key", ["r", "s"])
def test_research_board_unwired_keys_no_daemon_surface_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    """With no daemon, follow-up / snapshot surface the honest unavailable line."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            await pilot.press(key)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            result = pane.query_one(f"#{ACTION_RESULT_ID}")
            assert "daemon unavailable" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
