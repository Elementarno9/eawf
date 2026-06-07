"""``ResearchBoardModeScreen`` -- the Research mode 3-pane orchestrator (digit 3).

The Research mode renders the research-campaign control plane for the active
scope as the ratified three-pane orchestrator over the campaign engine:

* **Left pane -- the topic tree.** A campaign > round > topic > unresolved-
  question outline built from the staged
  :class:`~eawf.kernel.store.kinds.research_campaign.ResearchCampaignPayload`
  rows (one campaign node, a synthetic round node, one topic node per staged
  domain) plus the state-resident
  :class:`~eawf.kernel.state.models.OpenQuestion` rows (the unresolved-question
  leaves). ``up`` / ``down`` move a flat selection cursor over the tree nodes;
  ``enter`` peeks the selected node's findings read-only.
* **Center pane -- claims / evidence.** A tab bar
  (``[Claims][Options][Conflicts][Unresolved][Reports][Brief-preview]``) over
  the claim ledger, leading with the live Claims tab: one row per
  :class:`~eawf.kernel.state.models.Claim` with its
  :class:`~eawf.kernel.state.enums.ClaimStatus`.
* **Right pane -- progress / budget.** The run bands RUN / ROUND / ACTIVE /
  WAITING / PAUSED / BUDGET / RISKS, derived from the staged campaign + the
  open-question ledger + the open ``needs_user`` checkpoints.
* **Bottom drawer -- the checkpoint.** The active ``needs_user`` pause for the
  scope (its prompt + resolution options), or the honest no-checkpoint line.

The TUI owns the SHELL + the live read/peek + the honest-wired actions; it does
NOT own the campaign engine. The live multi-round runner / synthesis is spawn-
gated and has no TUI-callable seam yet, so the action keys split into two
honest halves (the idle-contract pattern):

* **Live now (read / peek + checkpoint resolution).** ``enter`` peeks the
  selected node read-only. ``a`` (approve) routes through the real
  ``needs_user.resolve`` RPC and ``p`` (park) through the real
  ``needs_user.park`` RPC when a checkpoint maps to an open pause -- the daemon
  is the canonical mutator for the pause store -- surfacing the honest result;
  with no checkpoint selected each surfaces the honest no-checkpoint line and
  issues no RPC.
* **Honest-unavailable (no live runner yet).** ``r`` (follow-up) and ``s``
  (snapshot) route through the daemon-client seam to their intended method
  names, but no such RPC exists yet, so each surfaces the honest "not yet
  wired" line and never fakes the action. They go live for free once their
  RPCs land.

Honest-empty is the COMMON path, not an edge case: a scope that has staged no
campaign and logged no claim / open question renders the muted
:data:`EMPTY_NOTICE` banner instead of a fabricated three-pane board, exactly
like the autopilot / evidence / trust modes' honest-empty surfaces. The render
half is a set of pure, content-markup-returning helpers (one per pane) so the
composition is unit-testable without mounting Textual.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.ids import natural_key
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup

if TYPE_CHECKING:
    from eawf.kernel.state.models import Claim, OpenQuestion, State
    from eawf.workflow.skills.needs_user import OpenPause

logger = logging.getLogger(__name__)

#: Notice rendered when the scope has no research signal at all -- no staged
#: campaign, no claim, and no open question. The common path on a scope that
#: has run no campaign (no campaign store on disk, empty claim / question
#: maps). Phrased honestly so the empty surface is unmistakable rather than
#: reading as a measured "nothing to research".
EMPTY_NOTICE: str = "no active research campaign"

#: Per-section sentinel rendered when one pane is empty while the board as a
#: whole still carries signal elsewhere (e.g. a campaign is staged but no
#: claim has been logged yet). Muted so the operator reads it as "none yet",
#: distinct from a section that does not apply.
NONE_YET: str = "none yet"

#: Cap on campaign / claim / question rows rendered per pane so a large scope
#: does not flood the panes; an overflow count is appended past the cap.
_MAX_ROWS: int = 12

#: Cap on per-campaign staged-dispatch topic (domain) nodes shown in the tree.
_MAX_TOPICS_PER_CAMPAIGN: int = 8

#: Pane / drawer widget ids (addressable so a host or rebuild can target them).
TREE_PANE_ID: str = "research-tree"
CENTER_PANE_ID: str = "research-center"
PROGRESS_PANE_ID: str = "research-progress"
DRAWER_ID: str = "research-drawer"

#: Id of the honest-empty notice shown when the scope carries no research
#: signal (rendered instead of the three-pane scaffold).
EMPTY_ID: str = "research-empty"

#: Id of the peek-result line under the tree (honest read-only drill output).
PEEK_RESULT_ID: str = "research-peek"

#: Id of the action-result line under the drawer (approve / park / follow-up /
#: snapshot outcome -- honest about whether the request was issued).
ACTION_RESULT_ID: str = "research-action"

#: CSS class on each rendered topic-tree node row.
TREE_NODE_CLASS: str = "research-tree-node"

#: The center-pane tab labels, in render order. Only the Claims tab carries a
#: live projection today; the others are part of the ratified IA and render
#: alongside it so the tab structure is visible (the live tab projections
#: behind Options / Conflicts / etc. land with the multi-round runner).
CENTER_TABS: tuple[str, ...] = (
    "Claims",
    "Options",
    "Conflicts",
    "Unresolved",
    "Reports",
    "Brief-preview",
)

#: Daemon JSON-RPC method the ``a`` (approve) key routes through -- the real
#: needs_user pause resolver (the daemon is the canonical pause-store mutator).
_RESOLVE_METHOD: str = "needs_user.resolve"

#: Daemon JSON-RPC method the ``p`` (park) key routes through -- the real
#: needs_user open-pause lister (parking leaves the pause open for later).
_PARK_METHOD: str = "needs_user.park"

#: Intended daemon methods the not-yet-wired action keys route through. No such
#: RPC exists yet (full registry checked) -- the daemon answers
#: method-not-found, so the action surfaces the honest "not yet wired" line.
#: Wiring the keys to the real method names now is the idle-contract pattern:
#: they go live for free once the matching RPC lands.
_FOLLOWUP_METHOD: str = "research.followup"
_SNAPSHOT_METHOD: str = "research.snapshot"

#: Daemon JSON-RPC method the ``x`` (cancel-campaign) key routes through -- the
#: real cancel-campaign tombstoner (the daemon is the canonical campaign-store
#: mutator).
_CANCEL_METHOD: str = "research.cancel_campaign"

#: Intended daemon method the ``n`` (new-campaign) key routes through. Staging
#: a campaign from the TUI has no callable seam yet (campaigns are staged
#: through the ``eawf research campaign new`` CLI / ``research.create_campaign``
#: RPC, which needs a full ``research:`` block the board cannot yet compose), so
#: ``n`` surfaces the honest "not yet wired" line -- the idle-contract pattern,
#: live for free once a TUI campaign-staging seam lands.
_NEW_CAMPAIGN_METHOD: str = "research.stage_campaign"

#: Drawer line before any checkpoint is open (the idle checkpoint surface).
CHECKPOINT_IDLE: str = "no checkpoint -- nothing awaiting operator review"

#: Action-result line before any action key is pressed.
ACTION_IDLE: str = "enter peek  n new  a approve  p park  r follow-up  s snapshot  x cancel"

#: Action-result line when an approve / park has no checkpoint to act on.
APPROVE_NO_CHECKPOINT: str = "approve: no checkpoint to approve"
PARK_NO_CHECKPOINT: str = "park: no checkpoint to park"

#: Action-result line when a checkpoint action could not reach the daemon.
APPROVE_NO_DAEMON: str = "approve: daemon unavailable -- request not issued"
PARK_NO_DAEMON: str = "park: daemon unavailable -- request not issued"

#: Action-result line when ``x`` cancel fires with no campaign node selected.
CANCEL_NO_CAMPAIGN: str = "cancel: no campaign selected to cancel"

#: Action-result line when the cancel-campaign request could not reach the
#: daemon (the canonical campaign-store mutator).
CANCEL_NO_DAEMON: str = "cancel: daemon unavailable -- request not issued"

#: Honest "not yet wired" line for the keys whose engine runner does not exist
#: yet (follow-up / snapshot). Formatted with the verb so each reads clearly.
_NOT_WIRED_TEMPLATE: str = "{verb}: not yet wired -- no {method} RPC"

#: Honest line when a not-yet-wired action cannot even reach the daemon.
_UNAVAILABLE_TEMPLATE: str = "{verb}: daemon unavailable -- request not issued"

#: Footer hints for the Research pane (arrows primary). The action keys ride
#: after the tree-nav keys so they are discoverable. The mode digits are
#: surfaced by the always-visible mode row, not duplicated in the hint strip.
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens AND the shared-token actions stay pinned to the canonical vocabulary
#: -- which also fixes the historical ``w/u scope`` typo that dropped the repo
#: letter.
_RESEARCH_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("Enter", "open"),
    render_hint_label("d", "brief"),
    render_hint_label("n", "new"),
    render_hint_label("a", "approve"),
    render_hint_label("p", "park"),
    render_hint_label("r", "follow-up"),
    render_hint_label("s", "snapshot"),
    render_hint_label("x", "cancel"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
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


class NodeKind(StrEnum):
    """Closed vocabulary for the kind of a topic-tree node.

    The tree is a campaign > round > topic > unresolved-question outline; each
    rendered node carries its kind so :meth:`ResearchBoardModeScreen.action_peek_selected`
    can describe the peeked node honestly without re-deriving its level.

    Members:
        CAMPAIGN: A staged-campaign node (the tree root per campaign).
        ROUND: The synthetic round node under a campaign (round 1 -- the
            campaign is staged, not yet multi-round-run).
        TOPIC: A staged-domain topic node under a round.
        QUESTION: An unresolved-question leaf under the campaign.
    """

    CAMPAIGN = "campaign"
    ROUND = "round"
    TOPIC = "topic"
    QUESTION = "question"


@dataclass(frozen=True)
class TreeNode:
    """One flattened node in the campaign topic tree.

    The tree is rendered as an indented flat list (one :class:`TreeNode` per
    row) so a single selection cursor walks it with ``up`` / ``down`` and
    ``enter`` peeks the selected node -- no nested-widget focus juggling. The
    glyph / indent encode the node's level visually.

    Attributes:
        kind: The node's :class:`NodeKind` (campaign / round / topic /
            question).
        label: The node's display text (campaign topic, round label, domain
            name, or question title).
        depth: Indent level (``0`` campaign, ``1`` round, ``2`` topic /
            question) driving the rendered indent.
        detail: A short honest peek line surfaced when the node is peeked
            (e.g. the campaign's domain count, or the question's status).
        campaign_id: The id of the campaign this node belongs to, set on a
            :attr:`NodeKind.CAMPAIGN` node so the ``x`` cancel action can
            resolve a selected campaign to its store id; ``None`` on the
            round / topic / question nodes.
    """

    kind: NodeKind
    label: str
    depth: int
    detail: str
    campaign_id: str | None = None


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
    banner instead of a fabricated three-pane board.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.

    Returns:
        ``True`` when any source carries a row; ``False`` when the scope
        has no research signal at all.
    """
    return bool(campaigns or claims or questions)


def build_tree_nodes(
    campaigns: tuple[CampaignRow, ...],
    questions: tuple[OpenQuestion, ...],
) -> tuple[TreeNode, ...]:
    """Flatten the campaign + question rows into the topic-tree node list.

    Builds the campaign > round > topic > unresolved-question outline as a
    flat tuple of :class:`TreeNode` rows in render order: per campaign, the
    campaign node, then a synthetic round-1 node, then one topic node per
    staged domain (capped at :data:`_MAX_TOPICS_PER_CAMPAIGN`). The unresolved
    open questions hang as question leaves after the campaign nodes (a question
    is scope-wide, not pinned to one campaign's topic). Empty when the scope
    has neither a campaign nor an open question.

    Args:
        campaigns: The staged campaign rows for the scope.
        questions: The state-resident open-question rows for the scope.

    Returns:
        The flattened tree nodes in render order; empty when nothing to show.
    """
    nodes: list[TreeNode] = []
    for campaign in campaigns:
        nodes.append(
            TreeNode(
                kind=NodeKind.CAMPAIGN,
                label=campaign.topic,
                depth=0,
                detail=f"{campaign.domain_count} staged topic(s), depth {campaign.default_depth}",
                campaign_id=campaign.campaign_id,
            )
        )
        nodes.append(
            TreeNode(
                kind=NodeKind.ROUND,
                label="round 1",
                depth=1,
                detail="campaign staged -- live multi-round run not yet wired",
            )
        )
        for domain in campaign.domains[:_MAX_TOPICS_PER_CAMPAIGN]:
            nodes.append(
                TreeNode(
                    kind=NodeKind.TOPIC,
                    label=domain,
                    depth=2,
                    detail=f"staged topic in {campaign.topic}",
                )
            )
    for question in questions:
        marker = "blocking" if question.blocking else question.status.value
        nodes.append(
            TreeNode(
                kind=NodeKind.QUESTION,
                label=question.title,
                depth=2,
                detail=f"open question -- {marker}",
            )
        )
    return tuple(nodes)


def render_tree(nodes: tuple[TreeNode, ...], selected: int) -> str:
    """Render the topic-tree pane (one indented row per node).

    Each row carries a level glyph (``v`` campaign, branch for round / topic,
    ``?`` for an unresolved question) and the node label, indented by depth.
    The *selected* row is marked so the peek target is visible. An empty node
    list renders the per-pane :data:`NONE_YET` sentinel.

    Args:
        nodes: The flattened tree nodes in render order.
        selected: Index of the selected node (the peek target), or ``-1``.

    Returns:
        A content-markup string of one tree row per node.
    """
    if not nodes:
        return f"[$muted]{NONE_YET}[/]"
    glyphs = {
        NodeKind.CAMPAIGN: "v",
        NodeKind.ROUND: ">",
        NodeKind.TOPIC: "#",
        NodeKind.QUESTION: "?",
    }
    lines: list[str] = []
    for index, node in enumerate(nodes):
        indent = "  " * node.depth
        glyph = glyphs.get(node.kind, "-")
        marker = "[$accent]>[/] " if index == selected else "  "
        tint = "$warn" if node.kind is NodeKind.QUESTION else "$muted"
        lines.append(f"{marker}{indent}[{tint}]{glyph}[/] {escape_markup(node.label)}")
    return "\n".join(lines)


def render_center_tabs(active: str) -> str:
    """Render the center-pane tab bar with *active* highlighted.

    The tab labels are the ratified center-pane IA
    (``[Claims][Options][Conflicts][Unresolved][Reports][Brief-preview]``);
    only the live Claims tab is highlighted today. Rendering the full bar keeps
    the ratified structure visible even though the non-Claims tabs carry no
    live projection yet.

    Args:
        active: The label of the active tab (highlighted).

    Returns:
        A content-markup tab-bar string.
    """
    cells: list[str] = []
    for label in CENTER_TABS:
        if label == active:
            cells.append(f"[$accent][{escape_markup(label)}][/]")
        else:
            cells.append(f"[$muted][{escape_markup(label)}][/]")
    return " ".join(cells)


def render_claims(claims: tuple[Claim, ...]) -> str:
    """Render the claim-ledger (center pane Claims tab) -- one row per claim.

    Each row names the claim status (tinted by lifecycle position) and its
    title. The rows are capped at :data:`_MAX_ROWS` with a ``+N more`` overflow
    line. An empty claim ledger renders :data:`NONE_YET`.

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


def render_progress(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
    *,
    checkpoints: int,
) -> str:
    """Render the right-pane progress / budget bands.

    The bands -- RUN / ROUND / ACTIVE / WAITING / PAUSED / BUDGET / RISKS --
    are derived honestly from the staged campaign + the ledgers + the open
    checkpoints. Live running / synthesis is spawn-gated with no TUI-callable
    runner, so the RUN band reads ``staged`` (not ``running``) and the budget
    band reads the staged-topic count rather than a live token spend -- the
    honest pre-spawn surface.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.
        checkpoints: The count of open ``needs_user`` checkpoints for the
            scope (the PAUSED band).

    Returns:
        A content-markup string of one band per line.
    """
    blocking = sum(1 for question in questions if question.blocking)
    answered = sum(1 for question in questions if question.status.value == "answered")
    open_q = sum(1 for question in questions if question.status.value == "open")
    topics = sum(campaign.domain_count for campaign in campaigns)
    run_state = "staged" if campaigns else "idle"
    lines = [
        f"[$accent]RUN[/] [$muted]{run_state} -- live run not yet wired[/]",
        "[$accent]ROUND[/] [$muted]1/1 (staged)[/]",
        f"[$accent]ACTIVE[/] [$muted]{len(claims)} claim(s)[/]",
        f"[$accent]WAITING[/] [$muted]{open_q} open question(s)[/]",
        f"[$accent]PAUSED[/] [$muted]{checkpoints} checkpoint(s)[/]",
        f"[$accent]BUDGET[/] [$muted]{topics} staged topic(s), {answered} answered[/]",
    ]
    if blocking:
        lines.append(f"[$warn]RISKS[/] [$warn]{blocking} blocking question(s)[/]")
    else:
        lines.append("[$accent]RISKS[/] [$muted]none[/]")
    return "\n".join(lines)


def render_checkpoint(pause: OpenPause | None) -> str:
    """Render the bottom checkpoint drawer for the active *pause*.

    When a ``needs_user`` pause is open the drawer names its prompt and lays
    out its resolution options inline (the ratified ``[discriminator task]
    [accept stronger] [park]`` shape); ``a`` approves with the first option,
    ``p`` parks. With no open pause the drawer shows the honest
    :data:`CHECKPOINT_IDLE` line.

    Args:
        pause: The open ``needs_user`` pause mapped to the active checkpoint,
            or ``None`` when nothing awaits review.

    Returns:
        A content-markup drawer string.
    """
    if pause is None:
        return f"[$muted]{CHECKPOINT_IDLE}[/]"
    options = " ".join(
        f"[$accent][{escape_markup(option.label)}][/]" for option in pause.question.options
    )
    return (
        f"[$warn]checkpoint[/] {escape_markup(pause.question.question)}\n"
        f"[$muted]options:[/] {options}"
    )


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
    """Research mode 3-pane orchestrator over the campaign engine.

    Composes the ratified three panes -- the campaign topic tree (left), the
    claims / evidence tabs (center), and the progress / budget bands (right) --
    above a bottom checkpoint drawer, inside the shared :class:`ScopeScreen`
    chassis. Reads the host app's read-only ``state`` (for claims + open
    questions), ``_state_path`` (for the append-only ``research_campaign``
    store + the ``needs_user`` pause store). When the scope carries no research
    signal the pane renders the honest-empty :data:`EMPTY_NOTICE` banner rather
    than a fabricated three-pane board.

    ``up`` / ``down`` move the tree cursor; ``enter`` peeks the selected node
    read-only. ``a`` (approve) routes through the real ``needs_user.resolve``
    RPC and ``p`` (park) through ``needs_user.park`` when a checkpoint maps to
    an open pause, surfacing the honest result; ``r`` (follow-up) / ``s``
    (snapshot) route through the daemon-client seam to their not-yet-existing
    methods and surface the honest "not yet wired" line (the idle-contract
    pattern). No action ever fakes an outcome that did not happen.

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
    ResearchBoardModeScreen #research-panes {
        height: 1fr;
    }
    ResearchBoardModeScreen .research-pane {
        border: solid $accent;
        padding: 0 1;
        height: 1fr;
    }
    ResearchBoardModeScreen #research-tree {
        width: 1fr;
    }
    ResearchBoardModeScreen #research-center {
        width: 1fr;
    }
    ResearchBoardModeScreen #research-progress {
        width: 1fr;
    }
    ResearchBoardModeScreen #research-peek {
        height: auto;
        color: $muted;
    }
    ResearchBoardModeScreen #research-drawer {
        height: auto;
        border: solid $warn;
        padding: 0 1;
        margin-top: 1;
    }
    ResearchBoardModeScreen #research-action {
        height: auto;
        color: $muted;
        margin-top: 1;
    }
    ResearchBoardModeScreen #research-empty {
        height: 1fr;
        border: solid $warn;
        padding: 1 2;
    }
    ResearchBoardModeScreen .research-tree-node {
        height: auto;
    }
    """

    #: ``up`` / ``down`` move the tree cursor; ``enter`` peeks the selected
    #: node read-only. ``a`` approve / ``p`` park route checkpoint resolution
    #: through the real needs_user RPCs; ``r`` follow-up / ``s`` snapshot are
    #: the honest-unavailable idle-contract keys. The lowercase ``a``/``p``/
    #: ``r``/``s`` are the brief's canonical research action keys (no app-wide
    #: collision since this pane keeps arrows primary for the tree). The chrome
    #: bindings (palette / help / quit / scope / mode digits) come from the
    #: shared chassis + app-wide bindings, so the pane offers no j/k vim
    #: aliases here.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "select_prev", "up", show=False),
        Binding("down", "select_next", "down", show=False),
        Binding("enter", "peek_selected", "peek", show=False),
        Binding("d", "open_brief", "brief", show=False),
        Binding("n", "new_campaign", "new", show=False),
        Binding("a", "approve_checkpoint", "approve", show=False),
        Binding("p", "park_checkpoint", "park", show=False),
        Binding("r", "followup", "follow-up", show=False),
        Binding("s", "snapshot", "snapshot", show=False),
        Binding("x", "cancel_campaign", "cancel", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _RESEARCH_HINTS

    #: ``True`` once the most recent rebuild saw a scope with no research
    #: signal; drives whether the three-pane scaffold or the honest-empty
    #: notice is shown. Watched so a refresh repaints.
    empty: reactive[bool] = reactive(False, init=False)

    #: Bound state, watched so a fresh revision rebuilds the panes (the
    #: campaign pane re-reads the store on the same tick).
    state: reactive[State | None] = reactive(None)

    #: Index of the selected tree node (the peek target); clamped to the node
    #: list, ``0`` when non-empty, ``-1`` when empty.
    selected: reactive[int] = reactive(0, init=False)

    def __init__(self) -> None:
        """Initialise the pane with an empty node list until first compute."""
        super().__init__()
        self._tree: tuple[TreeNode, ...] = ()

    def compose_body(self) -> ComposeResult:
        """Yield the three-pane scaffold + drawer, or the honest-empty notice.

        When the scope carries no research signal the body is a single honest-
        empty notice (the common path); otherwise it is the three panes (tree /
        center / progress) above the checkpoint drawer + the action-result line.
        The panes start with their composed bodies and :meth:`_rebuild` keeps
        them in sync on every tick / revision.
        """
        campaigns, claims, questions = self._current_rows()
        self.empty = not has_research_signal(campaigns, claims, questions)
        self._tree = build_tree_nodes(campaigns, questions)
        with Vertical(id="research-body"):
            if self.empty:
                yield Static(self._empty_body(), id=EMPTY_ID)
                return
            pause = self._current_checkpoint()
            with Horizontal(id="research-panes"):
                with VerticalScroll(id=TREE_PANE_ID, classes="research-pane") as tree:
                    tree.border_title = "TOPIC TREE"
                    yield Static(render_tree(self._tree, self.selected), id="research-tree-body")
                    yield Static(self._peek_idle(), id=PEEK_RESULT_ID)
                with VerticalScroll(id=CENTER_PANE_ID, classes="research-pane") as center:
                    center.border_title = "CLAIMS / EVIDENCE"
                    yield Static(render_center_tabs("Claims"), id="research-center-tabs")
                    yield Static(render_claims(claims), id="research-center-body")
                with VerticalScroll(id=PROGRESS_PANE_ID, classes="research-pane") as progress:
                    progress.border_title = "PROGRESS / BUDGET"
                    yield Static(
                        render_progress(
                            campaigns, claims, questions, checkpoints=1 if pause else 0
                        ),
                        id="research-progress-body",
                    )
            drawer = Static(render_checkpoint(pause), id=DRAWER_ID)
            drawer.border_title = "CHECKPOINT"
            yield drawer
            yield Static(ACTION_IDLE, id=ACTION_RESULT_ID)

    def on_mount(self) -> None:
        """Seed from app state, arm the refresh seam, and clamp the cursor."""
        super().on_mount()
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        self.set_interval(RESEARCH_REFRESH_S, self._rebuild)
        self._clamp_selection()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Rebuild the panes when the bound state changes."""
        if self.is_mounted:
            self._rebuild()

    def watch_empty(self) -> None:
        """Recompose the body when the empty verdict flips (scaffold <-> notice)."""
        if self.is_mounted:
            self._rebuild()

    def watch_selected(self) -> None:
        """Repaint the tree selection highlight when the cursor moves."""
        if self.is_mounted:
            self._repaint_tree()

    def action_select_prev(self) -> None:
        """Move the tree cursor to the previous node (clamped at the top)."""
        if self._tree:
            self.selected = max(0, self.selected - 1)

    def action_select_next(self) -> None:
        """Move the tree cursor to the next node (clamped at the bottom)."""
        if self._tree:
            self.selected = min(len(self._tree) - 1, self.selected + 1)

    def action_peek_selected(self) -> None:
        """Peek the selected tree node read-only (no mutation).

        Surfaces the selected node's honest detail line under the tree -- a
        read-only drill into the node's findings. With no node selected (an
        honest-empty tree) there is nothing to peek, surfaced honestly.
        """
        node = self._selected_node()
        if node is None:
            self._set_peek("[$muted]nothing to peek[/]")
            return
        line = (
            f"[$accent]peek[/] [$muted]{escape_markup(node.kind.value)}:[/] "
            f"{escape_markup(node.label)} [$muted]-- {escape_markup(node.detail)}[/]"
        )
        self._set_peek(line)
        logger.info(f"action_peek_selected kind={node.kind.value} label={node.label!r}")

    def action_open_brief(self) -> None:
        """Open the scope's brief preview in a MarkdownViewer modal.

        Builds the brief-preview markdown from the active scope's research
        signal (:func:`~eawf.surfaces.tui.modes.brief_viewer.build_brief_preview_markdown`)
        and pushes a
        :class:`~eawf.surfaces.tui.modes.brief_viewer.BriefViewerScreen`, which
        renders it through a routing ``MarkdownViewer`` so the brief reads with a
        heading table-of-contents and a numbered ``## References`` list. An empty
        scope still opens -- the viewer shows the honest empty-brief body.
        """
        from eawf.surfaces.tui.modes.brief_viewer import (
            BriefViewerScreen,
            build_brief_preview_markdown,
        )

        campaigns, claims, questions = self._current_rows()
        brief_markdown = build_brief_preview_markdown(campaigns, claims, questions)
        self.app.push_screen(BriefViewerScreen(brief_markdown))
        logger.info(
            f"action_open_brief campaigns={len(campaigns)} claims={len(claims)} "
            f"questions={len(questions)}"
        )

    def action_approve_checkpoint(self) -> None:
        """Approve the active checkpoint via the real ``needs_user.resolve`` RPC.

        Resolves the open ``needs_user`` pause mapped to the active checkpoint,
        choosing the pause's first option label as the approve choice, through
        the daemon-client seam and surfaces the typed outcome honestly. With no
        open checkpoint there is nothing to approve; when the daemon is
        unreachable the result says so rather than implying a resolution.
        """
        pause = self._current_checkpoint()
        if pause is None:
            self._set_action(f"[$warn]{APPROVE_NO_CHECKPOINT}[/]")
            return
        result_line = self._issue_resolve(pause)
        self._set_action(result_line)
        logger.info(f"action_approve_checkpoint pause={pause.pause_urn!r} result={result_line!r}")

    def action_park_checkpoint(self) -> None:
        """Park the active checkpoint via the real ``needs_user.park`` RPC.

        Parking leaves the pause open for later review; it routes through the
        daemon ``needs_user.park`` lister (the canonical open-pause query) and
        surfaces the honest count of still-open checkpoints. With no open
        checkpoint there is nothing to park; when the daemon is unreachable the
        result says so.
        """
        pause = self._current_checkpoint()
        if pause is None:
            self._set_action(f"[$warn]{PARK_NO_CHECKPOINT}[/]")
            return
        result_line = self._issue_park(pause)
        self._set_action(result_line)
        logger.info(f"action_park_checkpoint pause={pause.pause_urn!r} result={result_line!r}")

    def action_followup(self) -> None:
        """Queue a follow-up task (no engine runner yet -- honest-unavailable).

        Routes through the daemon-client seam to the intended
        ``research.followup`` method. The live multi-round runner is spawn-
        gated with no TUI-callable seam yet, so the daemon answers method-not-
        found and the action surfaces the honest "not yet wired" line -- it
        never implies a follow-up was queued. It goes live for free once the
        runner RPC lands (the idle-contract pattern).
        """
        self._issue_unwired(verb="follow-up", method=_FOLLOWUP_METHOD)

    def action_snapshot(self) -> None:
        """Snapshot the campaign (no engine runner yet -- honest-unavailable).

        Routes through the daemon-client seam to the intended
        ``research.snapshot`` method. No such RPC exists yet, so the daemon
        answers method-not-found and the action surfaces the honest "not yet
        wired" line -- it never implies a snapshot was taken. It goes live for
        free once the runner RPC lands (the idle-contract pattern).
        """
        self._issue_unwired(verb="snapshot", method=_SNAPSHOT_METHOD)

    def action_new_campaign(self) -> None:
        """Stage a new campaign (no TUI staging seam yet -- honest-unavailable).

        Routes through the daemon-client seam to the intended
        ``research.stage_campaign`` method. The board cannot yet compose the
        full ``research:`` block ``research.create_campaign`` requires, so no
        TUI-callable staging seam exists: the daemon answers method-not-found
        and the action surfaces the honest "not yet wired" line -- it never
        implies a campaign was staged. It goes live for free once a TUI
        staging seam lands (the idle-contract pattern).
        """
        self._issue_unwired(verb="new-campaign", method=_NEW_CAMPAIGN_METHOD)

    def action_cancel_campaign(self) -> None:
        """Cancel the selected campaign via the real ``research.cancel_campaign`` RPC.

        Resolves the selected tree node to its campaign id and routes a cancel
        through the daemon-client seam (the daemon is the canonical campaign-
        store mutator), surfacing the honest typed outcome. With no campaign
        node selected there is nothing to cancel; when the daemon is unreachable
        the result says so rather than implying a cancellation.
        """
        campaign_id = self._selected_campaign_id()
        if campaign_id is None:
            self._set_action(f"[$warn]{CANCEL_NO_CAMPAIGN}[/]")
            return
        result_line = self._issue_cancel(campaign_id)
        self._set_action(result_line)
        logger.info(f"action_cancel_campaign campaign={campaign_id!r} result={result_line!r}")

    def _issue_resolve(self, pause: OpenPause) -> str:
        """Issue ``needs_user.resolve`` for *pause* and return a result line.

        Calls the daemon ``needs_user.resolve`` method with the pause urn + the
        pause's first option label as the approve choice through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest
        of the TUI mutates through, when a daemon socket is available. A daemon
        that is unreachable, rejecting, or timing out yields the honest
        unavailable / rejected line rather than a faked resolution.

        Args:
            pause: The open checkpoint pause to resolve.

        Returns:
            A content-markup result line describing the approve outcome.
        """
        choice = pause.question.options[0].label if pause.question.options else "approve"
        if not self._daemon_available():
            return f"[$warn]{APPROVE_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                client.call(
                    _RESOLVE_METHOD,
                    {"pause_urn": pause.pause_urn, "choice": choice},
                )
        except DaemonRpcError as exc:
            logger.debug(f"_issue_resolve daemon_rejected message={exc.message!r}")
            return (
                "[$warn]approve: daemon rejected request[/] "
                f"[$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_resolve daemon_fallback cause={exc!r}")
            return f"[$warn]{APPROVE_NO_DAEMON}[/]"
        return f"[$ok]approve: resolved[/] [$muted]choice={escape_markup(choice)}[/]"

    def _issue_park(self, pause: OpenPause) -> str:
        """Issue ``needs_user.park`` for *pause*'s scope and return a result line.

        Calls the daemon ``needs_user.park`` lister scoped to the pause's scope
        through the daemon-client seam when a daemon socket is available, and
        reports the honest count of still-open checkpoints (parking leaves the
        pause open). A daemon that is unreachable, rejecting, or timing out
        yields the honest unavailable / rejected line.

        Args:
            pause: The open checkpoint pause being parked.

        Returns:
            A content-markup result line describing the park outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{PARK_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(_PARK_METHOD, {"scope_id": pause.scope_id})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_park daemon_rejected message={exc.message!r}")
            return "[$warn]park: daemon rejected request[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_park daemon_fallback cause={exc!r}")
            return f"[$warn]{PARK_NO_DAEMON}[/]"
        pauses = result.get("pauses", [])
        count = len(pauses) if isinstance(pauses, list) else 0
        return f"[$ok]park: left open[/] [$muted]{count} checkpoint(s) still open[/]"

    def _issue_cancel(self, campaign_id: str) -> str:
        """Issue ``research.cancel_campaign`` for *campaign_id* and return a result line.

        Calls the daemon ``research.cancel_campaign`` method with the campaign
        id through the same :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`
        seam the rest of the TUI mutates through, when a daemon socket is
        available. A daemon that is unreachable, rejecting, or timing out yields
        the honest unavailable / rejected line rather than a faked cancellation.

        Args:
            campaign_id: The id of the campaign to cancel.

        Returns:
            A content-markup result line describing the cancel outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{CANCEL_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                client.call(_CANCEL_METHOD, {"campaign_id": campaign_id})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_cancel daemon_rejected message={exc.message!r}")
            return (
                f"[$warn]cancel: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_cancel daemon_fallback cause={exc!r}")
            return f"[$warn]{CANCEL_NO_DAEMON}[/]"
        return f"[$ok]cancel: tombstoned[/] [$muted]campaign={escape_markup(campaign_id)}[/]"

    def _issue_unwired(self, *, verb: str, method: str) -> None:
        """Route a not-yet-wired action through the seam, honestly.

        Issues *method* through the daemon-client seam for muscle-memory +
        discoverability. No such RPC exists yet (follow-up / snapshot), so the
        daemon answers method-not-found, which the action surfaces as the honest
        "not yet wired" line; a daemon that is simply unreachable surfaces the
        honest unavailable line instead. Either way the action never fakes the
        outcome -- it goes live for free once the matching RPC lands (the
        idle-contract pattern).

        Args:
            verb: The action verb (``"follow-up"`` / ``"snapshot"``) for the
                result line + logs.
            method: The intended daemon JSON-RPC method name.
        """
        result_line = self._call_unwired(verb=verb, method=method)
        self._set_action(result_line)
        logger.info(f"_issue_unwired verb={verb} method={method!r} result={result_line!r}")

    def _call_unwired(self, *, verb: str, method: str) -> str:
        """Call a not-yet-existing *method* and return the honest result line.

        Args:
            verb: The action verb for the result line.
            method: The intended daemon JSON-RPC method name (does not exist
                yet).

        Returns:
            A content-markup line: the honest "not yet wired" line when the
            daemon answers method-not-found, or the honest unavailable line when
            the daemon cannot be reached.
        """
        not_wired = _NOT_WIRED_TEMPLATE.format(verb=verb, method=method)
        unavailable = _UNAVAILABLE_TEMPLATE.format(verb=verb)
        if not self._daemon_available():
            return f"[$warn]{unavailable}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                client.call(method, {})
        except DaemonRpcError as exc:
            # A real daemon answers method-not-found for these as-yet-unwired
            # methods; surface that honestly as "not yet wired". Any other RPC
            # error is also surfaced as not-wired -- the method does not exist.
            logger.debug(f"_call_unwired method_absent verb={verb} code={exc.code}")
            return f"[$warn]{not_wired}[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_call_unwired daemon_fallback verb={verb} cause={exc!r}")
            return f"[$warn]{unavailable}[/]"
        # A success result is impossible today (no such RPC). If a future daemon
        # implements the method, report it landed so the honest path inverts the
        # moment the RPC exists.
        return f"[$ok]{verb}: issued[/]"

    def _rebuild(self) -> None:
        """Recompute every pane from state + the campaign / pause stores.

        Recomputes the rows + tree nodes, clamps the cursor, and either swaps
        the body between the three-pane scaffold and the honest-empty notice
        (when the empty verdict flips) or updates each pane in place.
        """
        campaigns, claims, questions = self._current_rows()
        was_empty = self.empty
        self.empty = not has_research_signal(campaigns, claims, questions)
        self._tree = build_tree_nodes(campaigns, questions)
        self._clamp_selection()
        if self.empty != was_empty:
            self._recompose_body()
            return
        if self.empty:
            self._update_one(EMPTY_ID, self._empty_body())
            logger.info("research_rebuild empty=True")
            return
        pause = self._current_checkpoint()
        self._update_one("research-tree-body", render_tree(self._tree, self.selected))
        self._update_one("research-center-body", render_claims(claims))
        self._update_one(
            "research-progress-body",
            render_progress(campaigns, claims, questions, checkpoints=1 if pause else 0),
        )
        self._update_one(DRAWER_ID, render_checkpoint(pause))
        logger.info(
            f"research_rebuild campaigns={len(campaigns)} claims={len(claims)} "
            f"questions={len(questions)} nodes={len(self._tree)} empty={self.empty}"
        )

    def _recompose_body(self) -> None:
        """Tear down + recompose the body (used when the empty verdict flips).

        The three-pane scaffold and the honest-empty notice are different
        widget trees, so a flip between them rebuilds the body subtree rather
        than updating a Static in place.
        """
        from textual.widgets import Footer as _Footer

        bodies = self.query("#research-body")
        if not bodies:
            return
        bodies.first().remove()
        self.mount_all(self.compose_body(), before=self.query_one(_Footer))

    def _update_one(self, widget_id: str, body: str) -> None:
        """Update the Static identified by *widget_id*, if mounted."""
        found = self.query(f"#{widget_id}")
        if found:
            found.first(Static).update(body)

    def _repaint_tree(self) -> None:
        """Repaint the tree body so the selection highlight tracks the cursor."""
        self._update_one("research-tree-body", render_tree(self._tree, self.selected))

    def _clamp_selection(self) -> None:
        """Clamp the tree cursor into the current node list.

        A non-empty list clamps the index into ``0..len-1``; an empty list
        parks the cursor at ``-1`` so :meth:`_selected_node` returns ``None``.
        """
        if not self._tree:
            self.set_reactive(type(self).selected, -1)
            return
        self.set_reactive(type(self).selected, min(max(0, self.selected), len(self._tree) - 1))

    def _selected_node(self) -> TreeNode | None:
        """Return the selected tree node, or ``None`` when none is selected."""
        if not self._tree or not 0 <= self.selected < len(self._tree):
            return None
        return self._tree[self.selected]

    def _selected_campaign_id(self) -> str | None:
        """Return the selected node's campaign id, or ``None``.

        A campaign node carries its store id, so a selected campaign resolves
        directly. A round / topic node under a campaign resolves to its parent
        campaign by walking up the flat node list to the nearest preceding
        campaign node. A selected question leaf (scope-wide, not pinned to one
        campaign) or an empty tree yields ``None`` -- there is no campaign to
        cancel.

        Returns:
            The campaign id of the selected node's campaign, or ``None``.
        """
        node = self._selected_node()
        if node is None:
            return None
        if node.kind is NodeKind.QUESTION:
            return None
        if node.campaign_id is not None:
            return node.campaign_id
        for index in range(self.selected, -1, -1):
            candidate = self._tree[index]
            if candidate.kind is NodeKind.CAMPAIGN and candidate.campaign_id is not None:
                return candidate.campaign_id
        return None

    def _set_peek(self, line: str) -> None:
        """Update the peek-result line under the tree, if mounted."""
        self._update_one(PEEK_RESULT_ID, line)

    def _set_action(self, line: str) -> None:
        """Update the action-result line under the drawer, if mounted."""
        self._update_one(ACTION_RESULT_ID, line)

    def _peek_idle(self) -> str:
        """Return the idle peek line (before any node is peeked)."""
        return "[$muted]enter to peek the selected node[/]"

    def _empty_body(self) -> str:
        """Return the honest-empty notice body (banner + sub-line)."""
        return (
            f"[$warn]{EMPTY_NOTICE}[/]\n[$muted]no staged campaign, claim, or open question yet[/]"
        )

    def _current_rows(
        self,
    ) -> tuple[tuple[CampaignRow, ...], tuple[Claim, ...], tuple[OpenQuestion, ...]]:
        """Return the campaign / claim / question rows for the active scope.

        Reads the campaigns off the append-only ``research_campaign`` store
        under the resolved ``state.json`` and the claims / open questions off
        the bound read-only state. When the campaign store read fails (a
        malformed row) the campaign source degrades to empty rather than
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

    def _current_checkpoint(self) -> OpenPause | None:
        """Return the active ``needs_user`` checkpoint for the scope, or ``None``.

        Reads the open ``needs_user`` pauses off the scope's event store (the
        same store the attention feed + global inbox read) and returns the
        most-recent open pause as the active checkpoint. A read failure (no
        store, malformed rows) degrades to ``None`` so the drawer shows the
        honest no-checkpoint line rather than crashing.

        Returns:
            The active checkpoint pause, or ``None`` when none is open.
        """
        state_path = self._resolved_state_path()
        if state_path is None:
            return None
        from eawf.workflow.skills.needs_user import list_open_pauses

        try:
            pauses = list_open_pauses(state_path)
        except (OSError, ValueError) as exc:
            logger.debug(f"_current_checkpoint read_failed cause={exc!r}")
            return None
        return pauses[-1] if pauses else None

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

    def _daemon_available(self) -> bool:
        """Return whether the App reports a reachable daemon socket.

        Delegates to the App's own daemon-socket probe so the action paths use
        the same reachability verdict the rest of the TUI mutates through; a
        bare harness without the probe degrades to "unavailable" so the actions
        never raise.
        """
        probe = getattr(self.app, "_daemon_socket_available", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except OSError as exc:
            logger.debug(f"_daemon_available probe_failed cause={exc!r}")
            return False


__all__ = [
    "ACTION_IDLE",
    "APPROVE_NO_CHECKPOINT",
    "APPROVE_NO_DAEMON",
    "CANCEL_NO_CAMPAIGN",
    "CANCEL_NO_DAEMON",
    "CENTER_TABS",
    "CHECKPOINT_IDLE",
    "DRAWER_ID",
    "EMPTY_NOTICE",
    "NONE_YET",
    "PARK_NO_CHECKPOINT",
    "PARK_NO_DAEMON",
    "RESEARCH_REFRESH_S",
    "CampaignRow",
    "NodeKind",
    "ResearchBoardModeScreen",
    "TreeNode",
    "build_tree_nodes",
    "has_research_signal",
    "read_campaign_rows",
    "render_center_tabs",
    "render_checkpoint",
    "render_claims",
    "render_progress",
    "render_tree",
]
