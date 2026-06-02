"""``ResearchBoardModeScreen`` -- the Research mode pane over the campaign store.

The Research mode (digit ``7``) renders the research-campaign overview for
the active scope: the staged campaigns persisted under the scope's
:class:`~eawf.kernel.store.kinds.research_campaign.ResearchCampaignPayload`
store, the state-resident :class:`~eawf.kernel.state.models.Claim` ledger
(with each claim's :class:`~eawf.kernel.state.enums.ClaimStatus`), and the
state-resident :class:`~eawf.kernel.state.models.OpenQuestion` list (with
each question's :class:`~eawf.kernel.state.enums.OpenQuestionStatus` and the
blocking flag the balanced-autonomy interrupt keys on).

The TUI owns the SHELL only -- the mode screen + its render helpers. The
campaign store and the claim / open-question reducers are research-owned:
this pane READS them and never adds a reducer or mutates a record. The
campaign rows come off the append-only ``research_campaign`` JSONL store
under ``<state_dir>/store/`` (read the same way the agent-report rollup
reads its role stores); the claims and open questions come off the bound
read-only :class:`~eawf.kernel.state.models.State`.

Honest-empty is the COMMON path, not an edge case: a scope that has staged
no campaign and logged no claim / open question (the live ``state.json`` is
exactly this) renders the muted :data:`EMPTY_NOTICE` banner instead of a
fabricated overview, exactly like the evidence / trust modes' honest-empty
surfaces. The render half is a set of pure, content-markup-returning
helpers (one per section) so the composition is unit-testable without
mounting Textual; the screen is a thin :class:`ScopeScreen` body over them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.ids import natural_key
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.markup import escape_markup

if TYPE_CHECKING:
    from eawf.kernel.state.models import Claim, OpenQuestion, State

logger = logging.getLogger(__name__)

#: Notice rendered when the scope has no research signal at all -- no staged
#: campaign, no claim, and no open question. The common path on a scope that
#: has run no campaign (no campaign store on disk, empty claim / question
#: maps). Phrased honestly so the empty surface is unmistakable rather than
#: reading as a measured "nothing to research".
EMPTY_NOTICE: str = "no active research campaign"

#: Per-section sentinel rendered when one section is empty while the board as
#: a whole still carries signal elsewhere (e.g. a campaign is staged but no
#: claim has been logged yet). Muted so the operator reads it as "none yet",
#: distinct from a section that does not apply.
NONE_YET: str = "none yet"

#: Cap on campaign / claim / question rows rendered per section so a large
#: scope does not flood the pane; an overflow count is appended past the cap.
_MAX_ROWS: int = 12

#: Cap on per-campaign staged-dispatch domain names shown inline.
_MAX_DOMAINS_PER_CAMPAIGN: int = 6

#: Footer hints for the Research pane (full key names, arrows primary).
_RESEARCH_HINTS: tuple[str, ...] = (
    "up/down scroll",
    "1-7 mode",
    "w/r/u scope",
    "/ palette",
    "? help",
    "q quit",
)

#: Research-board refresh cadence in seconds (matches the trust / metrics
#: surface 5 s tick so the pane can switch to daemon-push later without
#: changing the visible contract).
RESEARCH_REFRESH_S: float = 5.0


@dataclass(frozen=True)
class CampaignRow:
    """One staged research campaign projected for the board.

    Attributes:
        campaign_id: The persisted campaign id (record key).
        topic: The campaign topic fanned out across the staged domains.
        domains: The staged domain names, in staged (sorted) order.
        default_depth: The campaign-wide default survey depth token.
    """

    campaign_id: str
    topic: str
    domains: tuple[str, ...]
    default_depth: str

    @property
    def domain_count(self) -> int:
        """Return the number of staged domains in this campaign."""
        return len(self.domains)


def read_campaign_rows(state_path: Path | None) -> tuple[CampaignRow, ...]:
    """Read the staged campaign rows off the scope's campaign store.

    Reads every record in the append-only ``research_campaign`` JSONL store
    under *state_path* (``<state_dir>/store/research_campaign.jsonl``) the
    same way the agent-report rollup reads its role stores: line by line,
    validating each :class:`~eawf.kernel.store.envelope.Envelope` and then
    its :class:`~eawf.kernel.store.kinds.research_campaign.ResearchCampaignPayload`.
    Returns an empty tuple -- the COMMON path -- when *state_path* is
    ``None`` or no campaign store exists on disk, so the pane renders
    honest-empty rather than crashing on a scope that has staged no
    campaign.

    Args:
        state_path: Path to the scope's ``state.json``; the campaign store
            resolves under its sibling ``store/`` directory. ``None`` (user
            scope / no resolved state) yields no rows.

    Returns:
        The campaign rows in record (chronological) order; empty when no
        campaign store exists for the scope.
    """
    if state_path is None:
        return ()
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return ()
    rows: list[CampaignRow] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = ResearchCampaignPayload.model_validate(envelope.payload)
        campaign = payload.campaign
        rows.append(
            CampaignRow(
                campaign_id=payload.campaign_id,
                topic=campaign.topic,
                domains=tuple(dispatch.domain for dispatch in campaign.dispatches),
                default_depth=payload.config.default_depth.value,
            )
        )
    logger.info(f"read_campaign_rows campaigns={len(rows)} path={path.name!r}")
    return tuple(rows)


def has_research_signal(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
) -> bool:
    """Return whether the board has any research signal to render.

    The board has signal when at least one of the three sources carries a
    row: a staged campaign, a logged claim, or an open question. When all
    three are empty the pane renders the honest-empty :data:`EMPTY_NOTICE`
    banner instead of a fabricated overview.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.

    Returns:
        ``True`` when any source carries a row; ``False`` when the scope
        has no research signal at all.
    """
    return bool(campaigns or claims or questions)


def render_overview(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
) -> str:
    """Render the board overview line (campaign / claim / question counts).

    When the scope has no research signal the overview is the honest-empty
    banner; otherwise it reports the campaign, claim, and open-question
    counts so the operator reads the board shape at a glance, leading with
    the count of blocking open questions (the only kind the balanced-
    autonomy interrupt raises) when any are present.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.

    Returns:
        A content-markup overview string.
    """
    if not has_research_signal(campaigns, claims, questions):
        return (
            f"[$warn]{EMPTY_NOTICE}[/]\n[$muted]no staged campaign, claim, or open question yet[/]"
        )
    lines = [
        f"campaigns {len(campaigns)}",
        f"claims {len(claims)}",
        f"open questions {len(questions)}",
    ]
    blocking = sum(1 for question in questions if question.blocking)
    if blocking:
        lines.append(f"[$warn]blocking {blocking}[/]")
    return "\n".join(lines)


def render_campaigns(campaigns: tuple[CampaignRow, ...]) -> str:
    """Render the staged-campaign section (one line per campaign).

    Each line names the campaign topic, its default depth, and its staged
    domain names (capped at :data:`_MAX_DOMAINS_PER_CAMPAIGN` with an
    overflow count). The rows are capped at :data:`_MAX_ROWS` with a
    ``+N more`` overflow line. An empty campaign list renders
    :data:`NONE_YET`.

    Args:
        campaigns: The staged campaign rows for the scope.

    Returns:
        A content-markup string of one staged-campaign line per row.
    """
    if not campaigns:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for campaign in campaigns[:_MAX_ROWS]:
        domains = campaign.domains[:_MAX_DOMAINS_PER_CAMPAIGN]
        extra = campaign.domain_count - len(domains)
        shown = ", ".join(escape_markup(domain) for domain in domains)
        domain_tail = shown if extra <= 0 else f"{shown} +{extra}"
        domain_suffix = "" if not domains else f" [$muted]{domain_tail}[/]"
        lines.append(
            f"{escape_markup(campaign.topic)} "
            f"[$accent]{escape_markup(campaign.default_depth)}[/]{domain_suffix}"
        )
    overflow = len(campaigns) - _MAX_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def render_claims(claims: tuple[Claim, ...]) -> str:
    """Render the claim-ledger section (one line per claim, with status).

    Each line names the claim status (tinted by lifecycle position) and its
    title. The rows are capped at :data:`_MAX_ROWS` with a ``+N more``
    overflow line. An empty claim ledger renders :data:`NONE_YET`.

    Args:
        claims: The state-resident claim rows for the scope.

    Returns:
        A content-markup string of one claim line per row.
    """
    if not claims:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for claim in claims[:_MAX_ROWS]:
        lines.append(f"{_claim_status_markup(claim.status.value)} {escape_markup(claim.title)}")
    overflow = len(claims) - _MAX_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def render_open_questions(questions: tuple[OpenQuestion, ...]) -> str:
    """Render the open-question section (one line per question, with status).

    Each line names the question status (tinted by lifecycle position), a
    ``[blocking]`` marker when the question gates further work (the kind the
    balanced-autonomy interrupt raises), and the question title. The rows
    are capped at :data:`_MAX_ROWS` with a ``+N more`` overflow line. An
    empty question list renders :data:`NONE_YET`.

    Args:
        questions: The state-resident open-question rows for the scope.

    Returns:
        A content-markup string of one open-question line per row.
    """
    if not questions:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for question in questions[:_MAX_ROWS]:
        marker = " [$warn]blocking[/]" if question.blocking else ""
        lines.append(
            f"{_question_status_markup(question.status.value)}{marker} "
            f"{escape_markup(question.title)}"
        )
    overflow = len(questions) - _MAX_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def _claim_status_markup(status: str) -> str:
    """Return *status* wrapped in its palette-var colour span.

    Args:
        status: One of the :class:`~eawf.kernel.state.enums.ClaimStatus`
            values.

    Returns:
        The status wrapped in the matching ``theme.tcss`` palette var.
    """
    palette = {
        "open": "$accent",
        "supported": "$ok",
        "refuted": "$warn",
        "superseded": "$muted",
    }
    return f"[{palette.get(status, '$muted')}]{status}[/]"


def _question_status_markup(status: str) -> str:
    """Return *status* wrapped in its palette-var colour span.

    Args:
        status: One of the
            :class:`~eawf.kernel.state.enums.OpenQuestionStatus` values.

    Returns:
        The status wrapped in the matching ``theme.tcss`` palette var.
    """
    palette = {
        "open": "$accent",
        "answered": "$ok",
        "blocked": "$warn",
        "dropped": "$muted",
    }
    return f"[{palette.get(status, '$muted')}]{status}[/]"


def _sorted_claims(state: State | None) -> tuple[Claim, ...]:
    """Return the scope's claims sorted by natural id, empty when unbound.

    Args:
        state: The loaded state, or ``None`` (fresh / user scope) -- the
            latter yields an empty tuple rather than raising.

    Returns:
        The claim rows in natural-id order.
    """
    if state is None or state.claims is None:
        return ()
    return tuple(state.claims[key] for key in sorted(state.claims, key=natural_key))


def _sorted_questions(state: State | None) -> tuple[OpenQuestion, ...]:
    """Return the scope's open questions sorted by natural id, empty when unbound.

    Args:
        state: The loaded state, or ``None`` (fresh / user scope) -- the
            latter yields an empty tuple rather than raising.

    Returns:
        The open-question rows in natural-id order.
    """
    if state is None or state.open_questions is None:
        return ()
    return tuple(state.open_questions[key] for key in sorted(state.open_questions, key=natural_key))


class ResearchBoardModeScreen(ScopeScreen):
    """Research mode pane over the campaign store + claim / question ledgers.

    Composes the board sections -- overview, staged campaigns, the claim
    ledger, and the open-question list -- inside the shared
    :class:`ScopeScreen` chassis. Reads the host app's read-only ``state``
    (for claims + open questions) and ``_state_path`` (for the append-only
    ``research_campaign`` store). When the scope carries no research signal
    the pane renders the honest-empty :data:`EMPTY_NOTICE` banner rather
    than a fabricated overview.

    The screen self-binds to the host
    :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state``: it seeds from
    ``app.state`` on mount and rebuilds when a daemon-pushed revision lands,
    so a claim / question logged after launch surfaces without a relaunch.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ResearchBoardModeScreen #research-body {
        height: 1fr;
        padding: 1 2;
    }
    ResearchBoardModeScreen .research-section {
        border: solid $accent;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }
    ResearchBoardModeScreen #research-section-overview.-empty {
        border: solid $warn;
    }
    """

    #: ``up`` / ``down`` scroll the section column; the chrome bindings
    #: (palette / help / quit / scope / mode digits) come from the shared
    #: chassis + app-wide bindings. Vim keys are aliases only (keymap rule).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", "up", show=False),
        Binding("down", "scroll_down", "down", show=False),
        Binding("pageup", "page_up", "page up", show=False),
        Binding("pagedown", "page_down", "page down", show=False),
        Binding("home", "scroll_home", "home", show=False),
        Binding("end", "scroll_end", "end", show=False),
        Binding("k", "scroll_up", "up", show=False),
        Binding("j", "scroll_down", "down", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _RESEARCH_HINTS

    #: The section column body specs: ``(widget id, heading)`` in render
    #: order. :meth:`_section_body` dispatches each id to its render helper.
    SECTIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("research-section-overview", "RESEARCH"),
        ("research-section-campaigns", "CAMPAIGNS"),
        ("research-section-claims", "CLAIMS"),
        ("research-section-questions", "OPEN QUESTIONS"),
    )

    #: ``True`` once the most recent rebuild saw a scope with no research
    #: signal; drives the overview border tint so the honest-empty state is
    #: visible at a glance. Watched so a refresh repaints the tint.
    empty: reactive[bool] = reactive(False, init=False)

    #: Bound state, watched so a fresh revision rebuilds the claim / question
    #: sections (the campaign section re-reads the store on the same tick).
    state: reactive[State | None] = reactive(None)

    def compose_body(self) -> ComposeResult:
        """Yield the scrollable section column for the research board."""
        campaigns, claims, questions = self._current_rows()
        self.empty = not has_research_signal(campaigns, claims, questions)
        with VerticalScroll(id="research-body"):
            for section_id, heading in self.SECTIONS:
                section = Static(
                    self._section_body(section_id, campaigns, claims, questions),
                    id=section_id,
                    classes="research-section",
                )
                section.border_title = heading
                yield section

    def on_mount(self) -> None:
        """Seed from app state, arm the refresh seam, and tint the overview."""
        super().on_mount()
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        self.set_interval(RESEARCH_REFRESH_S, self._rebuild)
        self._repaint_empty()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Rebuild the board sections when the bound state changes."""
        if self.is_mounted:
            self._rebuild()

    def watch_empty(self) -> None:
        """Repaint the overview tint when the empty verdict changes."""
        if self.is_mounted:
            self._repaint_empty()

    def _repaint_empty(self) -> None:
        """Toggle the ``-empty`` class onto the overview section."""
        overview = self.query("#research-section-overview")
        if overview:
            overview.first(Static).set_class(self.empty, "-empty")

    def _rebuild(self) -> None:
        """Recompute every section from state + the campaign store."""
        campaigns, claims, questions = self._current_rows()
        self.empty = not has_research_signal(campaigns, claims, questions)
        for section_id, _heading in self.SECTIONS:
            sections = self.query(f"#{section_id}")
            if sections:
                sections.first(Static).update(
                    self._section_body(section_id, campaigns, claims, questions)
                )
        logger.info(
            f"research_rebuild campaigns={len(campaigns)} claims={len(claims)} "
            f"questions={len(questions)} empty={self.empty}"
        )

    def _section_body(
        self,
        section_id: str,
        campaigns: tuple[CampaignRow, ...],
        claims: tuple[Claim, ...],
        questions: tuple[OpenQuestion, ...],
    ) -> str:
        """Render *section_id*'s body from the projected rows.

        Args:
            section_id: The section widget id (one of :attr:`SECTIONS`).
            campaigns: The staged campaign rows for the scope.
            claims: The state-resident claim rows.
            questions: The state-resident open-question rows.

        Returns:
            The section's content-markup body.
        """
        if section_id == "research-section-overview":
            return render_overview(campaigns, claims, questions)
        if section_id == "research-section-campaigns":
            return render_campaigns(campaigns)
        if section_id == "research-section-claims":
            return render_claims(claims)
        if section_id == "research-section-questions":
            return render_open_questions(questions)
        return f"[$muted]{NONE_YET}[/]"

    def _current_rows(
        self,
    ) -> tuple[tuple[CampaignRow, ...], tuple[Claim, ...], tuple[OpenQuestion, ...]]:
        """Return the campaign / claim / question rows for the active scope.

        Reads the campaigns off the append-only ``research_campaign`` store
        under the resolved ``state.json`` and the claims / open questions off
        the bound read-only state. When the campaign store read fails (a
        malformed row) the campaign section degrades to empty rather than
        crashing the mode, so a bad store row never takes the board down.

        Returns:
            A ``(campaigns, claims, questions)`` tuple; any of the three is
            empty when its source carries no row.
        """
        state = self._current_state()
        claims = _sorted_claims(state)
        questions = _sorted_questions(state)
        state_path = self._resolved_state_path()
        try:
            campaigns = read_campaign_rows(state_path)
        except Exception as exc:
            logger.debug(f"_current_rows campaign_read_failed cause={exc!r}")
            campaigns = ()
        return campaigns, claims, questions

    def _current_state(self) -> State | None:
        """Return the bound read-only state, if loaded."""
        from eawf.kernel.state.models import State

        state = self.state
        if state is not None:
            return state
        app_state = getattr(self.app, "state", None)
        return app_state if isinstance(app_state, State) else None

    def _resolved_state_path(self) -> Path | None:
        """Return the host app's read-only ``state.json`` path, if any."""
        try:
            state_path = getattr(self.app, "_state_path", None)
        except RuntimeError:
            return None
        return state_path if isinstance(state_path, Path) else None


__all__ = [
    "EMPTY_NOTICE",
    "NONE_YET",
    "RESEARCH_REFRESH_S",
    "CampaignRow",
    "ResearchBoardModeScreen",
    "has_research_signal",
    "read_campaign_rows",
    "render_campaigns",
    "render_claims",
    "render_open_questions",
    "render_overview",
]
