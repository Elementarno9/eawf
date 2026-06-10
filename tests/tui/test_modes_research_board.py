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
from textual.widgets import Input, MarkdownViewer, Static
from textual.widgets.markdown import Markdown, MarkdownBlock, MarkdownTableOfContents

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
from eawf.kernel.store.kinds.research_campaign import (
    CampaignTombstone,
    ResearchCampaignPayload,
)
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
    NEW_NO_DAEMON,
    NONE_YET,
    PARK_NO_CHECKPOINT,
    PEEK_RESULT_ID,
    PROGRESS_PANE_ID,
    TREE_PANE_ID,
    CampaignDraft,
    CampaignRow,
    ComposeCampaignModal,
    NodeKind,
    OperatorNoteModal,
    ResearchBoardModeScreen,
    build_research_block,
    build_tree_nodes,
    claim_sigil_markup,
    has_research_signal,
    index_claims_by_question,
    parse_domains,
    question_sigil_markup,
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
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.sigils import status_sigil
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


def _claim(
    claim_id: str = "CL-0001",
    *,
    status: ClaimStatus = ClaimStatus.OPEN,
    answers_question_id: str | None = None,
) -> Claim:
    """Build a claim row in *status*, optionally answering a question."""
    return Claim(
        id=claim_id,
        scope_id="QR",
        title="Implied vol surface is downward sloping in strike",
        status=status,
        answers_question_id=answers_question_id,
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


def test_read_campaign_rows_drops_cancelled_campaign(tmp_path: Path) -> None:
    """A campaign whose latest row is CANCELLED is not a live board row.

    The store is append-only: a cancel appends a tombstoned CANCELLED copy of
    the campaign. The reader must collapse latest-wins per campaign_id and drop
    the cancelled one, else the board renders a duplicate, never-filtered live
    row (the CampaignStatus.CANCELLED contract).
    """
    state_path = _write_state(tmp_path, _project_state())
    active = _campaign_payload()
    _append_campaign(state_path, active)
    cancelled = active.model_copy(
        update={
            "status": CampaignStatus.CANCELLED,
            "tombstone": CampaignTombstone(cancelled_at=_T0, reason="superseded"),
        }
    )
    _append_campaign(state_path, cancelled)
    assert read_campaign_rows(state_path) == ()


def test_read_campaign_rows_collapses_latest_and_keeps_active(tmp_path: Path) -> None:
    """With one cancelled + one active campaign, only the active one renders once."""
    state_path = _write_state(tmp_path, _project_state())
    keep = _campaign_payload(campaign_id="RC-0001")
    drop = _campaign_payload(campaign_id="RC-0002")
    _append_campaign(state_path, keep)
    _append_campaign(state_path, drop)
    _append_campaign(
        state_path,
        drop.model_copy(
            update={
                "status": CampaignStatus.CANCELLED,
                "tombstone": CampaignTombstone(cancelled_at=_T0),
            }
        ),
    )
    rows = read_campaign_rows(state_path)
    assert len(rows) == 1
    assert rows[0].campaign_id == "RC-0001"


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


def test_build_tree_nodes_open_question_groups_under_a_questions_round() -> None:
    """An open question hangs as a question node under the synthetic round.

    The round > questions > claims spine emits the ``questions`` round node
    even for a question-only scope (no campaign), with the question grouped
    beneath it carrying its lifecycle status for the per-status sigil.
    """
    nodes = build_tree_nodes((), (_question(),))
    assert len(nodes) == 2
    assert nodes[0].kind is NodeKind.ROUND
    assert nodes[0].label == "round 1 -- questions"
    assert nodes[1].kind is NodeKind.QUESTION
    assert nodes[1].depth == 2
    assert nodes[1].label == "Which curve model fits the short tenor"
    assert nodes[1].question_status is OpenQuestionStatus.OPEN


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


def test_render_claims_populated_surfaces_sigil_and_title() -> None:
    """A claim row leads with its lifecycle sigil (not the word) and its title."""
    body = render_claims((_claim(status=ClaimStatus.SUPPORTED),))
    # The status renders as a sigil, never the raw status word.
    assert "supported" not in body
    assert claim_sigil_markup(ClaimStatus.SUPPORTED, mode=DEFAULT_RENDER_MODE) in body
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


def test_build_brief_preview_markdown_claim_bullet_leads_with_a_sigil() -> None:
    """The claim bullet renders a leading status sigil via the helper, not a bare word.

    The orphan ClaimStatus bullet must lead with the resolved status glyph
    (W28), so a reader sees the shape -- not just the raw ``.value`` word.
    """
    md = build_brief_preview_markdown((), (_claim(status=ClaimStatus.SUPPORTED),), ())
    sigil = status_sigil(ClaimStatus.SUPPORTED).render(mode=DEFAULT_RENDER_MODE)
    # The bullet leads with the sigil, then the status word + title.
    assert f"- {sigil} supported:" in md
    # It is NOT the pre-W28 bare-value bullet form.
    assert "- supported:" not in md


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
    # ``Enter`` is the canonical capitalized full key name carrying the
    # canonical shared-token action ``open`` (W22 token canon + W03 action canon).
    assert "Enter open" in hints
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


# --------------------------------------------------------------------------
# W11: parse_domains / build_research_block -- the compose-form helpers
# --------------------------------------------------------------------------


def test_parse_domains_empty_field_yields_empty() -> None:
    """An empty / whitespace-only domains field parses to no domain."""
    assert parse_domains("") == ()
    assert parse_domains("   ") == ()


def test_parse_domains_splits_and_trims_comma_separated() -> None:
    """Comma-separated domains split + trim into a clean tuple."""
    assert parse_domains("market-structure, pricing-models") == (
        "market-structure",
        "pricing-models",
    )


def test_parse_domains_splits_on_whitespace_and_newlines() -> None:
    """Whitespace + newlines also separate domains (lenient field parse)."""
    assert parse_domains("alpha\nbeta") == ("alpha", "beta")


def test_parse_domains_dedupes_preserving_first_seen_order() -> None:
    """A repeated domain collapses to one, keeping first-seen order."""
    assert parse_domains("beta, alpha, beta") == ("beta", "alpha")


def test_build_research_block_stages_one_default_domain_config_per_name() -> None:
    """Each composed domain becomes one default-tuned config in the block."""
    block = build_research_block(("market-structure", "pricing-models"))
    assert set(block.domains) == {"market-structure", "pricing-models"}
    assert all(cfg.depth is None for cfg in block.domains.values())


# --------------------------------------------------------------------------
# W11: ComposeCampaignModal -- the compose-form modal (commit / cancel)
# --------------------------------------------------------------------------


def test_compose_modal_commit_dismisses_topic_and_block(tmp_path: Path) -> None:
    """Filling topic + one domain and committing dismisses a typed draft."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    dismissed: list[CampaignDraft | None] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(ComposeCampaignModal(), dismissed.append)
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ComposeCampaignModal)
            modal.query_one(f"#{ComposeCampaignModal.TOPIC_INPUT_ID}", Input).value = "Survey IV"
            modal.query_one(
                f"#{ComposeCampaignModal.DOMAINS_INPUT_ID}", Input
            ).value = "pricing-models"
            modal.action_commit()
            await settle_screen(pilot)

    asyncio.run(body())
    assert len(dismissed) == 1
    draft = dismissed[0]
    assert isinstance(draft, CampaignDraft)
    assert draft.topic == "Survey IV"
    assert set(draft.block.domains) == {"pricing-models"}


def test_compose_modal_cancel_dismisses_none(tmp_path: Path) -> None:
    """``Esc`` cancels the modal with a ``None`` draft (no campaign composed)."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    dismissed: list[CampaignDraft | None] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(ComposeCampaignModal(), dismissed.append)
            await settle_screen(pilot)
            assert isinstance(app.screen, ComposeCampaignModal)
            await pilot.press("escape")
            await settle_screen(pilot)

    asyncio.run(body())
    assert dismissed == [None]


def test_compose_modal_commit_without_domain_stays_open(tmp_path: Path) -> None:
    """A commit with no parsed domain stays open with the missing-domain notice."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    dismissed: list[CampaignDraft | None] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(ComposeCampaignModal(), dismissed.append)
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ComposeCampaignModal)
            modal.query_one(f"#{ComposeCampaignModal.TOPIC_INPUT_ID}", Input).value = "topic only"
            modal.action_commit()  # no domain entered
            await settle_screen(pilot)
            # The modal stays open and surfaces the missing-domain notice.
            assert isinstance(app.screen, ComposeCampaignModal)
            notice = app.screen.query_one(f"#{ComposeCampaignModal.ERROR_ID}", Static)
            assert ComposeCampaignModal.NO_DOMAIN_NOTICE in str(notice.render())

    asyncio.run(body())
    assert dismissed == []


# --------------------------------------------------------------------------
# W11: n -> compose -> commit stages a campaign + re-renders the board
# --------------------------------------------------------------------------


def test_research_board_n_commit_stages_campaign_and_renders_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``n``, filling the form, and committing stages a campaign.

    The commit must call ``research.stage_campaign`` once with the composed
    topic + block (a fake DaemonClient capturing the call), and the board must
    re-render to show the new campaign node. The fake client persists a real
    campaign row so the re-read board surfaces the node honestly.
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    calls: list[tuple[str, dict[str, object]]] = []
    staged_topic = "Survey the options-pricing landscape"

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            # Persist a real campaign row so the board re-read surfaces the node.
            _append_campaign(state_path, _campaign_payload("RC-NEW"))
            return {
                "id": "RC-NEW",
                "campaign_id": "RC-NEW",
                "topic": staged_topic,
                "domain_count": 2,
                "appended_at": _T0.isoformat(),
            }

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
            await pilot.press("n")  # open the compose modal
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ComposeCampaignModal)
            modal.query_one(f"#{ComposeCampaignModal.TOPIC_INPUT_ID}", Input).value = staged_topic
            modal.query_one(
                f"#{ComposeCampaignModal.DOMAINS_INPUT_ID}", Input
            ).value = "market-structure, pricing-models"
            modal.action_commit()
            await settle_screen(pilot)  # drains the staging worker
            board = app.screen
            assert isinstance(board, ResearchBoardModeScreen)
            tree_body = str(board.query_one("#research-tree-body").render())  # type: ignore[attr-defined]
            assert staged_topic in tree_body
            result = board.query_one(f"#{ACTION_RESULT_ID}")
            assert "staged" in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "research.stage_campaign"
    assert params["topic"] == staged_topic
    config = params["config"]
    assert isinstance(config, dict)
    assert set(config["domains"]) == {"market-structure", "pricing-models"}


def test_research_board_n_cancel_issues_zero_rpcs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``n`` then ``Esc`` cancels the modal and issues zero RPCs."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
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
            return {}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("n")  # open the compose modal
            await settle_screen(pilot)
            assert isinstance(app.screen, ComposeCampaignModal)
            await pilot.press("escape")  # cancel
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)

    asyncio.run(body())
    assert calls == []


def test_research_board_n_commit_empty_topic_surfaces_daemon_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-topic commit surfaces the daemon's rejection -- no faked node.

    The form requires a domain (so it commits), but the topic is empty: the
    daemon rejects the staging with a typed error, which the board surfaces
    honestly. No campaign row is persisted, so no fabricated node appears.
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    calls: list[tuple[str, dict[str, object]]] = []

    class _RejectingClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _RejectingClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            calls.append((method, params))
            raise DaemonRpcError(code=-32602, message="campaign topic must be non-empty: ''")

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _RejectingClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("n")
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ComposeCampaignModal)
            # Empty topic, but a domain is supplied so the form commits.
            modal.query_one(f"#{ComposeCampaignModal.DOMAINS_INPUT_ID}", Input).value = "pricing"
            modal.action_commit()
            await settle_screen(pilot)  # drains the staging worker
            board = app.screen
            assert isinstance(board, ResearchBoardModeScreen)
            result = board.query_one(f"#{ACTION_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "daemon rejected request" in rendered
            assert "non-empty" in rendered
            assert "staged" not in rendered
            # No campaign node was fabricated -- the board carries no topic node.
            tree_body = str(board.query_one("#research-tree-body").render())  # type: ignore[attr-defined]
            assert "pricing" not in tree_body

    asyncio.run(body())
    assert len(calls) == 1
    assert calls[0][0] == "research.stage_campaign"
    assert calls[0][1]["topic"] == ""


def test_research_board_n_commit_no_daemon_surfaces_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit with no reachable daemon surfaces the honest unavailable line."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("n")
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ComposeCampaignModal)
            modal.query_one(f"#{ComposeCampaignModal.TOPIC_INPUT_ID}", Input).value = "topic"
            modal.query_one(f"#{ComposeCampaignModal.DOMAINS_INPUT_ID}", Input).value = "pricing"
            modal.action_commit()
            await settle_screen(pilot)
            board = app.screen
            assert isinstance(board, ResearchBoardModeScreen)
            result = board.query_one(f"#{ACTION_RESULT_ID}")
            assert NEW_NO_DAEMON in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())


# --------------------------------------------------------------------------
# W15: round > questions > claims tree with per-status open-question sigils
# --------------------------------------------------------------------------


def test_index_claims_by_question_empty_yields_empty_mapping() -> None:
    """No claim yields an empty question->claims mapping (boundary case)."""
    assert index_claims_by_question(()) == {}


def test_index_claims_by_question_groups_answering_claims() -> None:
    """A claim that back-links a question groups under that question id."""
    answering = _claim("CL-0001", answers_question_id="OQ-0001")
    free = _claim("CL-0002")  # answers no question
    grouped = index_claims_by_question((answering, free))
    assert set(grouped) == {"OQ-0001"}
    assert grouped["OQ-0001"] == (answering,)


def test_index_claims_by_question_omits_freestanding_claims() -> None:
    """A claim answering no question is omitted -- never a parentless leaf."""
    assert index_claims_by_question((_claim("CL-0002"),)) == {}


def test_build_tree_nodes_nests_answering_claims_under_their_question() -> None:
    """The round > questions > claims spine nests a claim under its question.

    A claim that back-links an open question hangs as a claim leaf one indent
    deeper than the question, carrying the claim's lifecycle status for its
    sigil; a question with no answering claim has no claim leaf.
    """
    question = _question("OQ-0001", status=OpenQuestionStatus.OPEN)
    claim = _claim("CL-0001", status=ClaimStatus.SUPPORTED, answers_question_id="OQ-0001")
    nodes = build_tree_nodes((), (question,), claims=(claim,))
    kinds = [node.kind for node in nodes]
    assert kinds == [NodeKind.ROUND, NodeKind.QUESTION, NodeKind.CLAIM]
    claim_node = nodes[2]
    assert claim_node.depth == 3  # one indent deeper than the question
    assert claim_node.claim_status is ClaimStatus.SUPPORTED
    assert claim_node.label == "Implied vol surface is downward sloping in strike"


def test_build_tree_nodes_campaign_and_questions_emit_both_rounds() -> None:
    """A campaign plus open questions emit the campaign round AND questions round."""
    nodes = build_tree_nodes((_campaign_row(),), (_question(),))
    rounds = [node for node in nodes if node.kind is NodeKind.ROUND]
    assert {node.label for node in rounds} == {"round 1", "round 1 -- questions"}
    # The campaign-owned round carries the campaign id; the questions round does not.
    campaign_round = next(node for node in rounds if node.label == "round 1")
    questions_round = next(node for node in rounds if node.label == "round 1 -- questions")
    assert campaign_round.campaign_id == "RC-0001"
    assert questions_round.campaign_id is None


def test_render_tree_renders_per_status_question_sigils() -> None:
    """Each open / answered / dropped question renders its own per-status sigil.

    The tree leads each question row with the lifecycle sigil for its status
    (not a flat pending dot), so the open / answered / dropped status reads off
    the row -- the W15 round > questions tree contract.
    """
    questions = (
        _question("OQ-0001", status=OpenQuestionStatus.OPEN),
        _question("OQ-0002", status=OpenQuestionStatus.ANSWERED),
        _question("OQ-0003", status=OpenQuestionStatus.DROPPED),
    )
    nodes = build_tree_nodes((), questions)
    body = render_tree(nodes, -1, mode=DEFAULT_RENDER_MODE)
    for status in (
        OpenQuestionStatus.OPEN,
        OpenQuestionStatus.ANSWERED,
        OpenQuestionStatus.DROPPED,
    ):
        assert question_sigil_markup(status, mode=DEFAULT_RENDER_MODE) in body
    # The raw status words never leak into the rendered tree rows.
    assert "answered" not in body
    assert "dropped" not in body


def test_render_tree_nests_claim_under_question_with_claim_sigil() -> None:
    """A nested claim row renders the claim's lifecycle sigil under its question."""
    question = _question("OQ-0001", status=OpenQuestionStatus.OPEN)
    claim = _claim("CL-0001", status=ClaimStatus.REFUTED, answers_question_id="OQ-0001")
    nodes = build_tree_nodes((), (question,), claims=(claim,))
    body = render_tree(nodes, -1, mode=DEFAULT_RENDER_MODE)
    assert claim_sigil_markup(ClaimStatus.REFUTED, mode=DEFAULT_RENDER_MODE) in body
    assert "Implied vol surface is downward sloping in strike" in body


def test_research_board_renders_question_tree_with_nested_claims(tmp_path: Path) -> None:
    """The mounted board renders the round > questions > claims tree.

    A seeded scope with an answered question (and a claim that answers it)
    surfaces the questions round, the question grouped under it, and the
    answering claim nested one indent deeper in the live tree pane. The
    per-status sigil markup is pinned by the pure ``render_tree`` unit tests;
    this guards the live nesting + grouping the rendered glyphs collapse to.
    """
    answered = _question("OQ-0001", status=OpenQuestionStatus.ANSWERED)
    claim = _claim("CL-0001", status=ClaimStatus.SUPPORTED, answers_question_id="OQ-0001")
    state = _project_state(
        claims={"CL-0001": claim},
        open_questions={"OQ-0001": answered},
    )
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            tree_body = str(pane.query_one("#research-tree-body").render())  # type: ignore[attr-defined]
            lines = tree_body.splitlines()
            round_label = "round 1 -- questions"
            question_label = "Which curve model fits the short tenor"
            claim_label = "Implied vol surface is downward sloping in strike"
            round_line = next(line for line in lines if round_label in line)
            question_line = next(line for line in lines if question_label in line)
            claim_line = next(line for line in lines if claim_label in line)
            # The claim label is indented deeper than its question label, which is
            # deeper than the round label -- the round > questions > claims spine.
            # Compare label column position so the selection marker (a fixed-width
            # prefix on the cursor row) never skews the measured indent.
            round_col = round_line.index(round_label)
            question_col = question_line.index(question_label)
            claim_col = claim_line.index(claim_label)
            assert round_col < question_col < claim_col

    asyncio.run(body())


# --------------------------------------------------------------------------
# W15: the operator channel -- t (steer) + o (add-question), no key collision
# --------------------------------------------------------------------------


def test_research_board_operator_channel_bindings_exist_and_are_distinct() -> None:
    """``t`` steer + ``o`` add-question are bound and distinct from s / a / n.

    The operator-channel keys must NOT collide with the live snapshot (``s``),
    approve (``a``), or new (``n``) handlers -- each resolves to its own action.
    """
    keys = {
        binding.key: binding.action
        for binding in ResearchBoardModeScreen.BINDINGS
        if hasattr(binding, "key")
    }
    assert keys.get("o") == "add_question"
    assert keys.get("t") == "steer"
    # The free operator-channel keys never displaced the live s / a / n keys.
    assert keys.get("s") == "snapshot"
    assert keys.get("a") == "approve_checkpoint"
    assert keys.get("n") == "new_campaign"
    # No two action keys share a key token (no collision across the whole map).
    bound_keys = [binding.key for binding in ResearchBoardModeScreen.BINDINGS]
    assert len(bound_keys) == len(set(bound_keys))


def test_research_board_operator_channel_keys_in_footer_hints() -> None:
    """The operator-channel keys are advertised in the footer hints."""
    hints = " ".join(ResearchBoardModeScreen.FOOTER_HINTS)
    assert "o ask" in hints
    assert "t steer" in hints


def test_operator_note_modal_commit_dismisses_trimmed_note(tmp_path: Path) -> None:
    """Filling the note and committing dismisses the trimmed text."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    dismissed: list[str | None] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(
                OperatorNoteModal(
                    title="add an open question",
                    label="question",
                    placeholder="ask",
                    noun="question",
                ),
                dismissed.append,
            )
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, OperatorNoteModal)
            modal.query_one(f"#{OperatorNoteModal.NOTE_INPUT_ID}", Input).value = "  which model  "
            modal.action_commit()
            await settle_screen(pilot)

    asyncio.run(body())
    assert dismissed == ["which model"]


def test_operator_note_modal_empty_commit_stays_open(tmp_path: Path) -> None:
    """A whitespace-only commit stays open with the empty-note notice."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    dismissed: list[str | None] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(
                OperatorNoteModal(
                    title="steer the campaign",
                    label="steer note",
                    placeholder="steer",
                    noun="steer note",
                ),
                dismissed.append,
            )
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, OperatorNoteModal)
            modal.query_one(f"#{OperatorNoteModal.NOTE_INPUT_ID}", Input).value = "   "
            modal.action_commit()
            await settle_screen(pilot)
            assert isinstance(app.screen, OperatorNoteModal)
            notice = app.screen.query_one(f"#{OperatorNoteModal.ERROR_ID}", Static)
            assert "steer note cannot be empty" in str(notice.render())

    asyncio.run(body())
    assert dismissed == []


def test_operator_note_modal_cancel_dismisses_none(tmp_path: Path) -> None:
    """``Esc`` cancels the note modal with a ``None`` note (no RPC)."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    dismissed: list[str | None] = []

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(
                OperatorNoteModal(
                    title="add an open question",
                    label="question",
                    placeholder="ask",
                    noun="question",
                ),
                dismissed.append,
            )
            await settle_screen(pilot)
            assert isinstance(app.screen, OperatorNoteModal)
            await pilot.press("escape")
            await settle_screen(pilot)

    asyncio.run(body())
    assert dismissed == [None]


def test_research_board_o_commit_routes_add_question_rpc_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``o``, filling the question, and committing routes the RPC.

    The add-question RPC does not exist yet, so a daemon that answers
    method-not-found surfaces the honest "not yet wired" line -- never a faked
    question. The call must reach ``research.add_question`` with the question
    title under the ``title`` params key.
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    calls: list[tuple[str, dict[str, object]]] = []

    class _MethodNotFoundClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _MethodNotFoundClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            calls.append((method, params))
            raise DaemonRpcError(code=-32601, message="method not found")

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _MethodNotFoundClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("o")  # open the add-question modal
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, OperatorNoteModal)
            modal.query_one(f"#{OperatorNoteModal.NOTE_INPUT_ID}", Input).value = "which model fits"
            modal.action_commit()
            await settle_screen(pilot)  # drains the channel worker
            board = app.screen
            assert isinstance(board, ResearchBoardModeScreen)
            result = board.query_one(f"#{ACTION_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "not yet wired" in rendered
            assert "research.add_question" in rendered

    asyncio.run(body())
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "research.add_question"
    assert params == {"title": "which model fits"}


def test_research_board_t_commit_routes_steer_rpc_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``t``, filling the steer note, and committing routes the RPC.

    The steer RPC does not exist yet, so the method-not-found daemon answer
    surfaces the honest "not yet wired" line. The call must reach
    ``research.steer`` with the note under the ``text`` params key.
    """
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
    calls: list[tuple[str, dict[str, object]]] = []

    class _MethodNotFoundClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _MethodNotFoundClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            calls.append((method, params))
            raise DaemonRpcError(code=-32601, message="method not found")

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _MethodNotFoundClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("t")  # open the steer modal
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, OperatorNoteModal)
            modal.query_one(f"#{OperatorNoteModal.NOTE_INPUT_ID}", Input).value = "prioritise flow"
            modal.action_commit()
            await settle_screen(pilot)  # drains the channel worker
            board = app.screen
            assert isinstance(board, ResearchBoardModeScreen)
            result = board.query_one(f"#{ACTION_RESULT_ID}")
            rendered = str(result.render())  # type: ignore[attr-defined]
            assert "not yet wired" in rendered
            assert "research.steer" in rendered

    asyncio.run(body())
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "research.steer"
    assert params == {"text": "prioritise flow"}


def test_research_board_operator_channel_cancel_issues_zero_rpcs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing ``o`` then ``Esc`` cancels the modal and issues zero RPCs."""
    state_path = _write_state(tmp_path, _project_state(claims={"CL-0001": _claim()}))
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
            return {}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            await pilot.press("o")  # open the add-question modal
            await settle_screen(pilot)
            assert isinstance(app.screen, OperatorNoteModal)
            await pilot.press("escape")  # cancel
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)

    asyncio.run(body())
    assert calls == []


def test_research_board_empty_board_renders_no_word_spoken_hero(tmp_path: Path) -> None:
    """An empty board renders the exact "no word spoken yet" hero.

    The common path on a scope with no campaign / claim / question: the muted
    hero literal (not a fabricated tree) -- the W15 empty-board contract.
    """
    state_path = _write_state(tmp_path, _project_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, ResearchBoardModeScreen)
            assert pane.empty is True
            frame = normalize_snapshot(capture_screen_text(app))
            assert "no word spoken yet" in frame
            # The questions tree pane is NOT mounted on the empty board.
            assert not pane.query(f"#{TREE_PANE_ID}")

    asyncio.run(body())
