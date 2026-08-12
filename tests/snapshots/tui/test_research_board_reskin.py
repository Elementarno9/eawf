"""Golden snapshots for the research-board cosmic-terminal reskin.

Pins the two reskin behaviours the wave delivers, each captured from the
:class:`~eawf.surfaces.tui.modes.research_board.ResearchBoardModeScreen` mounted
IN ISOLATION on a bare themed host (mirroring the status-pane reskin suite) so
the frame is a pure function of the bound fixture state with no app-level brand
header / sibling-owned chrome and no off-disk daemon read:

* the populated lifecycle layout -- the topic tree (campaign / round / topic /
  question levels), the claims tab leading each claim row with its lifecycle
  SIGIL (not the raw status word), and the Options / Conflicts / Unresolved
  tabs -- so a regression on the sigil-per-claim migration is caught; and
* the honest-empty surface -- the literal cosmic-terminal headline
  :data:`~eawf.surfaces.tui.modes.research_board.EMPTY_NOTICE` over the
  press-``n`` compose sub-line :data:`~eawf.surfaces.tui.modes.research_board.EMPTY_SUBLINE`,
  pinned verbatim from the mock (the ``n`` compose modal itself is deferred --
  the hint renders, not the behaviour).

Both frames pin the unicode render mode so the sigil column is deterministic.
The host carries only the read-only ``state`` / ``_state_path`` / ``render_mode``
the screen reads; there is no daemon socket, so the action keys never fire and
the checkpoint drawer reads its honest idle line.

Regenerate the goldens after an intentional layout change with::

    EAWF_DAEMONLESS=1 EAWF_SNAPSHOT_REGEN=1 uv run pytest \
        tests/snapshots/tui/test_research_board_reskin.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive

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
from eawf.surfaces.tui.modes.research_board import (
    EMPTY_ID,
    EMPTY_NOTICE,
    EMPTY_SUBLINE,
    ResearchBoardModeScreen,
    claim_sigil_markup,
    question_sigil_markup,
)
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: A wide terminal so the three panes lay out side-by-side, anchoring the
#: populated golden to the full lifecycle layout.
_SIZE = (120, 40)

assert _THEME.is_file(), f"missing theme: {_THEME}"


class _HostApp(App[None]):
    """Bare themed host carrying the read-only surface the screen reads.

    The screen reads ``state`` (claims + open questions), ``_state_path`` (the
    campaign store + the pause store), and ``render_mode`` (the sigil column)
    off ``self.app``. The host exposes exactly those and no daemon socket, so
    the screen renders read-only with no app-level brand header / sibling chrome.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")
    state: reactive[State | None] = reactive(None)

    def __init__(self, *, state: State | None, state_path: Path | None) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self.state = state
        self._state_path = state_path

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(ResearchBoardModeScreen())

    def _daemon_socket_available(self) -> bool:
        """No daemon under the bare host -- the action keys never fire."""
        return False


def _claim(
    claim_id: str,
    title: str,
    *,
    status: ClaimStatus,
    evidence_refs: list[str] | None = None,
) -> Claim:
    """Build a claim row in *status* with optional evidence refs."""
    return Claim(
        id=claim_id,
        scope_id="QR",
        title=title,
        status=status,
        evidence_refs=evidence_refs or [],
        created_at=_T0,
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


def _mixed_claims() -> tuple[Claim, ...]:
    """One claim per lifecycle status so every claim sigil shape renders."""
    return (
        _claim(
            "CL-0001",
            "Implied vol surface is downward sloping in strike",
            status=ClaimStatus.SUPPORTED,
            evidence_refs=["docs/vol.md", "urn:eawf:v1:artifact:QR/AR-1"],
        ),
        _claim(
            "CL-0002",
            "Term-structure arbitrage is exploitable intraday",
            status=ClaimStatus.REFUTED,
            evidence_refs=["docs/arb-refuted.md"],
        ),
        _claim(
            "CL-0003",
            "Overnight gamma decay drives the skew",
            status=ClaimStatus.OPEN,
        ),
        _claim(
            "CL-0004",
            "Earlier vol-cluster claim subsumed by CL-0001",
            status=ClaimStatus.SUPERSEDED,
        ),
    )


def _mixed_questions() -> tuple[OpenQuestion, ...]:
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


def _seeded_state(claims: tuple[Claim, ...], questions: tuple[OpenQuestion, ...]) -> State:
    """Build a repo state carrying the given claims + open questions."""
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
            "claims": {c.id: c.model_dump(mode="json") for c in claims} or None,
            "open_questions": {q.id: q.model_dump(mode="json") for q in questions} or None,
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_seeded_scope(
    tmp_path: Path,
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
) -> Path:
    """Write a seeded state + a staged campaign store, return the state path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(_seeded_state(claims, questions).model_dump_json(), encoding="utf-8")
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


def _write_round_record(state_path: Path, *, round_number: int, saturated: bool) -> None:
    """Append one executed-round record to the scope's research_round store.

    Seeds the real run record the W08 FA8 auto-run state derives from -- the
    board reads it back to classify the round (saturated -> the closed sigil)
    rather than inferring from the question ledger.
    """
    from eawf.runtime.daemon.methods.research import ResearchRoundPayload, persist_round

    persist_round(
        state_path,
        ResearchRoundPayload(
            campaign_id=_campaign_payload().campaign_id,
            round_number=round_number,
            domains=["market-structure"],
            finding_lines=["Implied vol surface is downward sloping in strike"],
            claim_ids=["CL-0001"],
            saturated=saturated,
            checkpoint=True,
            recorded_at=_T0,
        ),
    )


# --------------------------------------------------------------------------
# Empty-state copy -- the literal mock headline + press-n sub-line
# --------------------------------------------------------------------------


def test_empty_state_literals_pinned_from_mock() -> None:
    """The empty-state copy is pinned verbatim from the cosmic-terminal mock.

    The strict-spec pinned-literals strip joins the framing copy and the
    ``press n`` compose prompt with the reskin's middle-dot separator.
    """
    assert EMPTY_NOTICE == "no word spoken yet"
    assert EMPTY_SUBLINE == "a research campaign begins with a question · press n"


# --------------------------------------------------------------------------
# Claim sigil mapping -- a sigil per claim, never a raw status word
# --------------------------------------------------------------------------


def test_claim_sigil_maps_every_status_to_a_lifecycle_shape() -> None:
    """Each claim status renders a lifecycle sigil glyph, never its raw word."""
    for status in ClaimStatus:
        markup = claim_sigil_markup(status, mode="unicode")
        # The status word never leaks into the rendered markup.
        assert status.value not in markup
    # The shape-bearing statuses lead with their lifecycle glyph.
    assert glyph(Sigil.PENDING, mode="unicode") in claim_sigil_markup(
        ClaimStatus.OPEN, mode="unicode"
    )
    assert glyph(Sigil.CLOSED, mode="unicode") in claim_sigil_markup(
        ClaimStatus.SUPPORTED, mode="unicode"
    )
    assert glyph(Sigil.FAILED, mode="unicode") in claim_sigil_markup(
        ClaimStatus.REFUTED, mode="unicode"
    )


def test_claim_sigil_supported_is_tinted_closed_green() -> None:
    """A supported claim's sigil carries the Wong closed-green tint hex."""
    from eawf.surfaces.tui.widgets.sigils import tint

    markup = claim_sigil_markup(ClaimStatus.SUPPORTED, mode="unicode")
    assert tint(Sigil.CLOSED) is not None
    assert f"[{tint(Sigil.CLOSED)}]" in markup


def test_claim_sigil_superseded_is_muted() -> None:
    """A superseded claim has no live shape -- it renders the muted dot."""
    markup = claim_sigil_markup(ClaimStatus.SUPERSEDED, mode="unicode")
    assert markup.startswith("[$muted]")


def test_question_sigil_maps_shape_bearing_statuses() -> None:
    """Open / answered / dropped questions render a sigil, never the raw word."""
    for status in (
        OpenQuestionStatus.OPEN,
        OpenQuestionStatus.ANSWERED,
        OpenQuestionStatus.DROPPED,
    ):
        markup = question_sigil_markup(status, mode="unicode")
        assert status.value not in markup
    assert glyph(Sigil.CLOSED, mode="unicode") in question_sigil_markup(
        OpenQuestionStatus.ANSWERED, mode="unicode"
    )


# --------------------------------------------------------------------------
# Snapshot: the populated lifecycle layout (sigil per claim)
# --------------------------------------------------------------------------


def test_research_board_reskin_populated_snapshot(tmp_path: Path) -> None:
    """The mounted board renders the campaign-stats center detail for the default node.

    W03 made the center pane a polymorphic detail that morphs by selection. The
    default campaign selection renders the campaign stats -- the ROUNDS /
    QUESTIONS / CLAIMS / BUDGET bands with the scoped claim + conflict counts --
    so the populated golden pins the stats layout. (The per-claim lifecycle sigil
    mapping is pinned by the pure ``claim_sigil_markup`` tests above.)
    """
    claims = _mixed_claims()
    questions = _mixed_questions()
    state = _seeded_state(claims, questions)
    state_path = _write_seeded_scope(tmp_path, claims, questions)

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            from textual.widgets import Static

            screen = app.screen
            detail = str(screen.query_one("#research-center-body", Static).render())
            # The campaign-stats bands render with the scoped ledger counts.
            for band in ("ROUNDS", "QUESTIONS", "CLAIMS", "BUDGET", "CHECKPOINT"):
                assert band in detail
            # Four claims, one refuted -> the CLAIMS band counts them + the conflict.
            assert f"{len(claims)} claim(s)" in detail
            assert "1 conflict(s)" in detail  # the one REFUTED claim
            assert_screen_snapshot(app, _GOLDEN / "research_board_reskin_populated.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Snapshot: the FA8 auto-run round tree -- running / saturated / pruned
# --------------------------------------------------------------------------


def _autorun_questions() -> tuple[OpenQuestion, ...]:
    """Questions spanning open + answered + dropped so the round reads running.

    An open question keeps the round RUNNING; the answered one saturates the
    budget tally; the dropped one prunes. The mix pins the running round sigil
    AND the saturated / pruned budget figures in one auto-run frame.
    """
    return (
        _question("OQ-0001", "Which curve model fits the short tenor"),
        _question(
            "OQ-0002",
            "Does the smile invert past the 90 delta wing",
            status=OpenQuestionStatus.ANSWERED,
        ),
        _question(
            "OQ-0003",
            "Is the overnight gamma path worth tracking",
            status=OpenQuestionStatus.DROPPED,
        ),
    )


def test_research_board_fa8_autorun_round_tree_snapshot(tmp_path: Path) -> None:
    """The auto-run round tree derives its FA8 state from the real run records.

    The W08 contract: a campaign whose run persisted a *saturated* round record
    renders the saturated-round sigil (the closed circle) + the live run state
    in the RUN band -- the FA8 auto-run state reads off the real executed-round
    records, never a fabricated runner state nor the retired not-yet-wired line.
    """
    claims = (
        _claim(
            "CL-0001",
            "Implied vol surface is downward sloping in strike",
            status=ClaimStatus.SUPPORTED,
        ),
        _claim(
            "CL-0002",
            "Term-structure arbitrage is exploitable intraday",
            status=ClaimStatus.REFUTED,
        ),
    )
    questions = _autorun_questions()
    state = _seeded_state(claims, questions)
    state_path = _write_seeded_scope(tmp_path, claims, questions)
    # Seed a real saturated round record: the FA8 state now derives from the
    # run, not the question ledger (which would still read running).
    _write_round_record(state_path, round_number=1, saturated=True)

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            from textual.widgets import Static

            screen = app.screen
            tree_body = str(screen.query_one("#research-tree-body", Static).render())
            # The saturated round (from the real record) leads with the closed
            # sigil + the saturated label -- not the question-ledger running state.
            assert glyph(Sigil.CLOSED, mode=app.render_mode) in tree_body
            assert "round saturated" in tree_body
            assert "not yet wired" not in tree_body
            progress_body = str(screen.query_one("#research-progress-body", Static).render())
            # The RUN band reflects the real run state; the ROUND band counts it.
            assert "not yet wired" not in progress_body
            assert "1 run" in progress_body
            assert_screen_snapshot(app, _GOLDEN / "research_board_fa8_autorun.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Snapshot: the honest-empty surface (literal headline + press-n sub-line)
# --------------------------------------------------------------------------


def test_research_board_reskin_empty_snapshot(tmp_path: Path) -> None:
    """The empty board pins the literal headline + the press-n sub-line copy."""
    state = _seeded_state((), ())
    # No campaign store on disk and an empty claim / question map -> honest-empty.
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            from textual.widgets import Static

            empty_body = str(app.screen.query_one(f"#{EMPTY_ID}", Static).render())
            # The literal mock copy renders verbatim (headline + press-n sub-line).
            assert EMPTY_NOTICE in empty_body
            assert EMPTY_SUBLINE in empty_body
            assert_screen_snapshot(app, _GOLDEN / "research_board_reskin_empty.txt")

    asyncio.run(body())


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: never resolve a real daemon socket under the host."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
