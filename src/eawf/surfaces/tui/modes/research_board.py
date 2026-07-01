"""``ResearchBoardModeScreen`` -- the Research mode 3-pane orchestrator (digit 3).

The Research mode renders the research-campaign control plane for the active
scope as the ratified three-pane orchestrator over the campaign engine:

* **Left pane -- the campaign tree.** A campaign-topic > round > domain >
  unresolved-question outline built from the staged
  :class:`~eawf.kernel.store.kinds.research_campaign.ResearchCampaignPayload`
  rows (one campaign node carrying the topic, a round node whose FA8 auto-run
  state derives from the real executed-round records, one ``domain: <name>``
  leaf per staged domain the topic is fanned across) plus the
  state-resident :class:`~eawf.kernel.state.models.OpenQuestion` rows (the
  unresolved-question leaves). ``up`` / ``down`` move a flat selection cursor
  over the tree nodes; ``enter`` peeks the selected node's findings read-only.
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

The TUI owns the SHELL + the live read/peek + the live actions; it does NOT own
the campaign engine. Every action key now routes to a live daemon RPC (the
P30-I18 binding pass wired the campaign run, the operator channels, and the
follow-up / snapshot queries), so each surfaces the daemon's honest result and
never fakes an outcome:

* **Checkpoint resolution.** ``enter`` peeks the selected node read-only. ``a``
  (approve) routes through the real ``needs_user.resolve`` RPC and ``p`` (park)
  through the real ``needs_user.park`` RPC when a checkpoint maps to an open
  pause -- the daemon is the canonical mutator for the pause store -- surfacing
  the honest result; with no checkpoint selected each surfaces the honest
  no-checkpoint line and issues no RPC.
* **Run queries + operator channels.** ``r`` (follow-up) / ``s`` (snapshot)
  query the live ``research.followup`` / ``research.snapshot`` RPCs for the
  selected campaign; ``o`` / ``t`` / ``b`` / ``v`` push the add-question / steer
  / broadcast / override operator inputs through their live RPCs. Each surfaces
  the daemon's sent / rejected result.

Honest-empty is the COMMON path, not an edge case: a scope that has staged no
campaign and logged no claim / open question renders the muted
:data:`EMPTY_NOTICE` banner instead of a fabricated three-pane board, exactly
like the autopilot / evidence / trust modes' honest-empty surfaces. The render
half is a set of pure, content-markup-returning helpers (one per pane) so the
composition is unit-testable without mounting Textual.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from eawf.kernel.spec.research_campaign import ResearchDomainConfig, ResearchProfileBlock
from eawf.kernel.state.enums import (
    CampaignStatus,
    ClaimStatus,
    OpenQuestionStatus,
    StoreKind,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Round
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.empty_state import render_empty_state
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.seal import seal_art_widget
from eawf.surfaces.tui.widgets.sigils import Sigil

if TYPE_CHECKING:
    from eawf.kernel.spec.operator_input import CampaignProgressState
    from eawf.kernel.state.models import Claim, OpenQuestion, State
    from eawf.surfaces.tui.widgets.eu_bar import RenderMode
    from eawf.workflow.skills.needs_user import OpenPause

logger = logging.getLogger(__name__)

#: Empty-state headline rendered when the scope has no research signal at all
#: -- no staged campaign, no claim, and no open question. The common path on a
#: scope that has run no campaign (no campaign store on disk, empty claim /
#: question maps). The literal copy is pinned from the cosmic-terminal reskin
#: mock (``new-surfaces-mock-handoff.md``) so the empty surface reads in the
#: reskin's voice rather than a measured "nothing to research".
EMPTY_NOTICE: str = "no word spoken yet"

#: Empty-state sub-line under :data:`EMPTY_NOTICE`. Pins the mock's literal copy
#: verbatim per the strict-spec pinned-literals strip
#: (``new-surfaces-mock-handoff.md`` + the designer-reply pinned literals): the
#: framing copy then the middle-dot-joined ``press n`` compose prompt. The chip
#: form the reskin briefly carried dropped the pinned ``press n`` literal, so the
#: sub-line restores it -- the ``[ n new campaign ]`` action chip still renders
#: the live affordance beneath the hero copy, but the pinned literal is the
#: strict-spec authority for the sub-line text.
EMPTY_SUBLINE: str = "a research campaign begins with a question · press n"

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

#: The synthetic round number an unresolved question renders under. The board's
#: campaigns are staged (the live multi-round runner is spawn-gated), so every
#: open question belongs to the same staged round the topic tree labels
#: "round 1" -- the honest pre-multi-round round number, live for free once a
#: per-round question projection lands.
STAGED_ROUND: int = 1

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

#: Id of the Unresolved-tab section header in the center pane (one row per
#: open question with its status + round number).
UNRESOLVED_HEADER_ID: str = "research-unresolved-header"

#: Id of the Unresolved-tab body in the center pane (the rendered question rows).
UNRESOLVED_BODY_ID: str = "research-unresolved-body"

#: Id of the Options-tab body in the center pane (claims grouped by supporting
#: evidence -- the live candidate answers).
OPTIONS_BODY_ID: str = "research-options-body"

#: Id of the Conflicts-tab body in the center pane (claims grouped by
#: contradicting evidence -- the surfaced conflicts).
CONFLICTS_BODY_ID: str = "research-conflicts-body"

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

#: Live daemon methods the follow-up / snapshot action keys route through
#: (P30-I18-W03): ``r`` queries the campaign's next round, ``s`` snapshots the
#: run state. Both answer with a typed body the board surfaces honestly.
_FOLLOWUP_METHOD: str = "research.followup"
_SNAPSHOT_METHOD: str = "research.snapshot"

#: Daemon JSON-RPC method the ``g`` (run) key routes through -- the live
#: campaign driver (the daemon is the only surface that can spawn the
#: researcher sessions a live run needs). The RPC backgrounds the run on a
#: daemon worker thread and enforces the ACTIVE-campaign + not-already-in-flight
#: guards, so the key surfaces the honest run handle or the daemon's rejection
#: rather than faking a launch.
_RUN_METHOD: str = "research.run"

#: Daemon JSON-RPC method the ``x`` (cancel-campaign) key routes through -- the
#: real cancel-campaign tombstoner (the daemon is the canonical campaign-store
#: mutator).
_CANCEL_METHOD: str = "research.cancel_campaign"

#: Daemon JSON-RPC method the ``n`` (new-campaign) key routes through -- the
#: real plan-only stager (the daemon is the canonical campaign-store mutator).
#: The compose modal collects a topic + at least one domain into a
#: :class:`~eawf.kernel.spec.research_campaign.ResearchProfileBlock`, and the
#: commit issues the staging RPC off the UI thread; an empty-topic commit is
#: rejected by the daemon and surfaced honestly rather than faking a node.
_NEW_CAMPAIGN_METHOD: str = "research.stage_campaign"

#: Daemon JSON-RPC methods the operator-channel keys route through -- the four
#: campaign-fork channels the FA8 auto-run cockpit shares with the wave cockpit
#: (one grammar): ``o`` (add-question) appends an :class:`OpenQuestion` row to
#: the scope, ``t`` (steer) pushes an operator steer note into the running
#: campaign, ``b`` (broadcast) fans a notice to every running round of the
#: campaign, and ``v`` (override) forces an operator verdict onto the blocking
#: fork. All four RPCs are live (P30-I18-W02 add-question + W04 steer /
#: broadcast / override): each key collects its text through a modal and routes
#: the committed note off the UI thread, surfacing the daemon's honest sent /
#: rejected result rather than fabricating a row, a steer, a broadcast, or an
#: override. The steer / broadcast / override channels thread the selected
#: campaign id so the daemon lands the operator input against the running
#: campaign; the scope-wide add-question carries none.
_ADD_QUESTION_METHOD: str = "research.add_question"
_STEER_METHOD: str = "research.steer"
_BROADCAST_METHOD: str = "research.broadcast"
_OVERRIDE_METHOD: str = "research.override"

#: Drawer line before any checkpoint is open (the idle checkpoint surface).
CHECKPOINT_IDLE: str = "no checkpoint -- nothing awaiting operator review"

#: Action-result line before any action key is pressed. The mode hotkeys used
#: to live here (duplicating the footer), so the two key strips overlapped on
#: n/o/a/p; the keys now ride the dedicated top mode-key line
#: (:data:`MODE_KEYS_LINE`) and this line reverts to showing only live action
#: results.
ACTION_IDLE: str = "no action yet"

#: Id of the top mode-hotkey line (the mode-unique action keys), rendered FIRST
#: above the global-nav footer line. Other modes with extra keys can opt into
#: the same top line by yielding their own mode-key Static.
MODE_KEYS_ID: str = "research-mode-keys"

#: The mode-unique action keys, carried ONLY on the top mode-key line so no key
#: repeats between it and the global-nav footer (which keeps the tree-nav +
#: scope / palette / help / quit vocabulary).
MODE_KEYS_LINE: str = (
    "n new · o ask · t steer · b broadcast · v override · "
    "a approve · p park · r follow-up · s snapshot · g run · x cancel"
)

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

#: Action-result line when ``g`` run fires with no campaign node selected.
RUN_NO_CAMPAIGN: str = "run: no campaign selected to run"

#: Action-result line when the run request could not reach the daemon (the
#: only surface that can spawn a live researcher session).
RUN_NO_DAEMON: str = "run: daemon unavailable -- request not issued"

#: Action-result line while the staging RPC is in flight (the worker dispatched
#: the call off the UI thread). The result line flips to the honest outcome
#: once the worker returns.
NEW_PENDING: str = "new: staging campaign..."

#: Action-result line when a new-campaign commit could not reach the daemon
#: (the canonical campaign-store mutator) so nothing was staged.
NEW_NO_DAEMON: str = "new: daemon unavailable -- request not issued"

#: Action-result line while an operator-channel RPC (add-question / steer) is in
#: flight off the UI thread; the line flips to the honest outcome once the
#: worker returns. Formatted with the verb so each channel reads clearly.
_CHANNEL_PENDING_TEMPLATE: str = "{verb}: sending..."

#: Action-result line when an operator-channel commit could not reach the daemon
#: so nothing was sent. Formatted with the verb.
_CHANNEL_NO_DAEMON_TEMPLATE: str = "{verb}: daemon unavailable -- request not issued"

#: Honest line when a follow-up / snapshot run query cannot reach the daemon.
_UNAVAILABLE_TEMPLATE: str = "{verb}: daemon unavailable -- request not issued"

#: Footer hints for the Research pane (arrows primary). The action keys ride
#: after the tree-nav keys so they are discoverable. The mode digits are
#: surfaced by the always-visible mode row, not duplicated in the hint strip.
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens AND the shared-token actions stay pinned to the canonical vocabulary
#: -- which also fixes the historical ``w/u scope`` typo that dropped the repo
#: letter.
#: The board advertises its full action + FA8 operator-input vocabulary so every
#: bound key is discoverable in the strip (the pinned P30 FA8-advertisement
#: contracts in ``test_modes_research_board.py``). The strip is long, but the
#: shared :func:`~eawf.surfaces.tui.widgets.footer.fit_hints` chokepoint sheds
#: this per-mode MIDDLE band (keeping the nav head + the global tail ``c`` config
#: / ``/`` palette / ``?`` help / ``q`` quit pinned) when it overflows a narrow
#: row, so the cross-surface affordances never clip even though the authored set
#: stays complete.
_RESEARCH_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("Enter", "open"),
    render_hint_label("d", "brief"),
    # The mode-unique action keys (n/o/t/b/v/a/p/r/s/g/x) are NOT repeated here:
    # they ride the dedicated top mode-key line (:data:`MODE_KEYS_LINE`) so the
    # two footer strips no longer overlap. This bottom line keeps the tree-nav +
    # global-nav (scope / palette / help / quit) vocabulary only.
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)

#: Research-board refresh cadence in seconds (matches the trust / metrics
#: surface 5 s tick so the pane can switch to daemon-push later without
#: changing the visible contract).
RESEARCH_REFRESH_S: float = 5.0

#: :class:`~eawf.kernel.state.enums.ClaimStatus` -> the lifecycle
#: :class:`~eawf.surfaces.tui.widgets.sigils.Sigil` whose SHAPE the claim row
#: renders, or ``None`` for a status with no live lifecycle shape (it renders
#: a muted dot instead). A claim's status is its evidence verdict, so the
#: shape reads as the closest lifecycle phase: ``OPEN`` is still pending
#: (hollow circle), ``SUPPORTED`` reads as closed-resolved (filled circle),
#: ``REFUTED`` reads as failed (the multiplication x), and ``SUPERSEDED`` is
#: inert (no shape -- a muted dot). The COLOUR comes from
#: :func:`~eawf.surfaces.tui.widgets.sigils.tint` so shape + hue stay
#: single-homed in the sigils helper, never a raw status word.
_CLAIM_SIGIL: dict[ClaimStatus, Sigil | None] = {
    ClaimStatus.OPEN: Sigil.PENDING,
    ClaimStatus.SUPPORTED: Sigil.CLOSED,
    ClaimStatus.REFUTED: Sigil.FAILED,
    ClaimStatus.SUPERSEDED: None,
}

#: :class:`~eawf.kernel.state.enums.OpenQuestionStatus` -> the lifecycle
#: :class:`~eawf.surfaces.tui.widgets.sigils.Sigil` whose SHAPE an open-question
#: marker renders, or ``None`` for a status with no live lifecycle shape (a
#: muted dot). ``OPEN`` is pending (hollow circle), ``ANSWERED`` reads as
#: closed-resolved (filled circle), and ``DROPPED`` is inert (no shape).
#: ``BLOCKED`` is absent on purpose: a blocking question short-circuits to the
#: literal ``blocking`` ``$warn`` marker (the autonomy-interrupt signal) before
#: this map is consulted, so it never reaches the shape path.
_QUESTION_SIGIL: dict[OpenQuestionStatus, Sigil | None] = {
    OpenQuestionStatus.OPEN: Sigil.PENDING,
    OpenQuestionStatus.ANSWERED: Sigil.CLOSED,
    OpenQuestionStatus.DROPPED: None,
}


class RoundState(StrEnum):
    """The FA8 auto-run state of a campaign round, derived from its ledgers.

    The live multi-round runner is spawn-gated, so the persisted campaign
    record carries no per-round run status (only ACTIVE / CANCELLED). The
    board derives the auto-run state honestly from the open-question +
    candidate-claim ledgers already on hand, so the running / saturated /
    pruned grammar reads off real signal rather than a fabricated runner.

    Members:
        RUNNING: At least one open question still needs an answer -- the
            round is being worked (the auto-run live-round state). Renders
            the :attr:`~eawf.surfaces.tui.widgets.sigils.Sigil.RUNNING`
            filled-diamond.
        SATURATED: Every tracked question is resolved (none open) yet at
            least one was answered -- the round saturated (no more candidate
            answers to add). Renders the
            :attr:`~eawf.surfaces.tui.widgets.sigils.Sigil.CLOSED` circle.
        PRUNED: Every tracked question was pruned (dropped) without an
            answer -- the round closed out with nothing kept. Renders the
            withheld :attr:`~eawf.surfaces.tui.widgets.sigils.Sigil.ABANDONED`
            mark so a pruned round never reads as a clean close.
        IDLE: The campaign is staged but carries no tracked question yet --
            the pre-auto-run round (no live round to classify). Renders the
            :attr:`~eawf.surfaces.tui.widgets.sigils.Sigil.PENDING` ring.
    """

    RUNNING = "running"
    SATURATED = "saturated"
    PRUNED = "pruned"
    IDLE = "idle"


#: :class:`RoundState` -> the lifecycle :class:`Sigil` its round node renders.
#: A running round is the live diamond, a saturated round the closed circle, a
#: pruned round the withheld abandoned mark (never the clean circle), and an
#: idle (pre-auto-run) round the pending ring.
_ROUND_SIGIL: dict[RoundState, Sigil] = {
    RoundState.RUNNING: Sigil.RUNNING,
    RoundState.SATURATED: Sigil.CLOSED,
    RoundState.PRUNED: Sigil.ABANDONED,
    RoundState.IDLE: Sigil.PENDING,
}


@dataclass(frozen=True)
class RoundProgress:
    """The FA8 auto-run progress of a campaign round, derived from its ledger.

    A pure projection of the open-question + candidate-claim counts onto the
    auto-run round grammar (running / saturated / pruned + a budget figure) so
    the round node + budget band read off real signal rather than a fabricated
    runner. The live multi-round-runner state lands free once the runner emits
    a per-round status -- this is the honest pre-spawn surface.

    Attributes:
        state: The classified :class:`RoundState` of the round.
        open_count: Open (still-running) questions.
        answered_count: Answered (saturating) questions.
        pruned_count: Pruned candidates -- dropped questions + refuted /
            superseded claims that were set aside this round.
        spent_topics: Staged topics fanned out across the campaigns (the
            honest pre-spawn budget figure -- one staged sweep per topic).
    """

    state: RoundState
    open_count: int
    answered_count: int
    pruned_count: int
    spent_topics: int


def classify_round_state(questions: tuple[OpenQuestion, ...]) -> RoundState:
    """Classify the campaign round's auto-run state from its question ledger.

    Derives the round grammar honestly from the open-question statuses: any
    OPEN question means the round is still :attr:`RoundState.RUNNING`; with no
    open question, an ANSWERED one means the round :attr:`RoundState.SATURATED`
    and only DROPPED ones means it was :attr:`RoundState.PRUNED`; an empty
    ledger is :attr:`RoundState.IDLE` (the pre-auto-run round).

    Args:
        questions: The state-resident open-question rows for the scope.

    Returns:
        The classified round state.
    """
    if not questions:
        return RoundState.IDLE
    if any(question.status is OpenQuestionStatus.OPEN for question in questions):
        return RoundState.RUNNING
    if any(question.status is OpenQuestionStatus.ANSWERED for question in questions):
        return RoundState.SATURATED
    return RoundState.PRUNED


def classify_round_state_from_records(
    rounds: tuple[Round, ...],
    questions: tuple[OpenQuestion, ...],
) -> RoundState:
    """Classify the FA8 auto-run state from the REAL run records (W08 live-wire).

    The live-wired FA8 grammar: once a campaign run has persisted round records
    the auto-run state derives from the real run, not the question ledger --

    * the terminal round was :attr:`~eawf.kernel.state.models.Round.saturated`
      -> :attr:`RoundState.SATURATED` (the loop halted because the campaign
      converged);
    * a run executed rounds but none saturated -> :attr:`RoundState.RUNNING`
      (the run is in flight / halted on budget, still working);
    * no run has persisted a round yet -> fall back to the question-ledger
      classification (:func:`classify_round_state`), the honest pre-run surface.

    A :attr:`RoundState.PRUNED` round still derives from the question ledger
    (the records carry no pruned-round terminal), so the fallback covers it.

    Args:
        rounds: The executed round records read from the round store.
        questions: The state-resident open-question rows (the pre-run fallback).

    Returns:
        The classified round state, derived from real run records when present.
    """
    if not rounds:
        return classify_round_state(questions)
    if any(rnd.saturated for rnd in rounds):
        return RoundState.SATURATED
    return RoundState.RUNNING


def compute_round_progress(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
    rounds: tuple[Round, ...] = (),
) -> RoundProgress:
    """Project the campaign ledgers + run records onto the auto-run round progress.

    Counts the open / answered questions, the pruned candidates (dropped
    questions + refuted / superseded claims set aside this round), and the
    staged-topic budget figure, then classifies the round state. When real run
    records are present the state derives from them
    (:func:`classify_round_state_from_records`, the W08 live-wire); otherwise it
    falls back to the question-ledger classification. A pure function of the
    rows on hand.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.
        rounds: The executed round records; empty pre-run (the state then
            falls back to the question-ledger classification).

    Returns:
        The derived :class:`RoundProgress`.
    """
    open_count = sum(1 for question in questions if question.status is OpenQuestionStatus.OPEN)
    answered_count = sum(
        1 for question in questions if question.status is OpenQuestionStatus.ANSWERED
    )
    dropped_questions = sum(
        1 for question in questions if question.status is OpenQuestionStatus.DROPPED
    )
    pruned_claims = sum(
        1 for claim in claims if claim.status in (ClaimStatus.REFUTED, ClaimStatus.SUPERSEDED)
    )
    spent_topics = sum(campaign.domain_count for campaign in campaigns)
    return RoundProgress(
        state=classify_round_state_from_records(rounds, questions),
        open_count=open_count,
        answered_count=answered_count,
        pruned_count=dropped_questions + pruned_claims,
        spent_topics=spent_topics,
    )


def read_round_rows(state_path: Path | None) -> tuple[Round, ...]:
    """Read the executed research rounds off the scope's round store.

    Reads every record in the append-only ``research_round`` JSONL store under
    *state_path* (``<state_dir>/store/research_round.jsonl``), validating each
    :class:`~eawf.kernel.store.envelope.Envelope` and projecting its payload into
    a typed :class:`~eawf.kernel.state.models.Round`. The rows are the real
    rounds a campaign run executed -- the entity that retires the board's
    synthetic single round node. Returns an empty tuple (the COMMON pre-run
    path) when *state_path* is ``None`` or the store does not exist, so a staged-
    but-not-run scope renders the honest pre-run surface rather than crashing.

    Args:
        state_path: Path to the scope's ``state.json``; the round store resolves
            under its sibling ``store/`` directory. ``None`` yields no rows.

    Returns:
        The executed rounds in ascending round order across every campaign.
    """
    if state_path is None:
        return ()
    path = store_path(state_path, StoreKind.RESEARCH_ROUND)
    if not path.exists():
        return ()
    rounds: list[Round] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = envelope.payload
        rounds.append(
            Round(
                campaign_id=str(payload["campaign_id"]),
                round_number=int(payload["round_number"]),
                finding_count=len(payload.get("finding_lines", [])),
                claim_count=len(payload.get("claim_ids", [])),
                saturated=bool(payload.get("saturated", False)),
                checkpoint=bool(payload.get("checkpoint", False)),
                steer_notes=list(payload.get("steer_notes", [])),
            )
        )
    rounds.sort(key=lambda r: (r.campaign_id, r.round_number))
    logger.info(f"read_round_rows rounds={len(rounds)} path={path.name!r}")
    return tuple(rounds)


def project_campaign_progress(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
    rounds: tuple[Round, ...],
) -> CampaignProgressState:
    """Fold the campaign ledgers + executed rounds into a CampaignProgressState.

    The single shared projection the board RUN band + ``eawf status`` both
    render: it answers "can the campaign proceed, or is it blocked?" by reducing
    the per-domain progress (derived from the staged domains + the claim /
    question ledgers) through the pure
    :meth:`~eawf.kernel.spec.operator_input.CampaignProgressState.project`. A
    blocking open question forces ``BLOCKED_AWAIT_USER`` (D-2); a saturated
    terminal round pins the ``SATURATED`` good-stop; otherwise the ready domains
    drive ``RUNNABLE``.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.
        rounds: The executed rounds read from the round store.

    Returns:
        The folded :class:`~eawf.kernel.spec.operator_input.CampaignProgressState`.
    """
    from eawf.kernel.spec.operator_input import (
        CampaignProgressState,
        DomainProgress,
        DomainProgressStatus,
    )

    blocking = sum(1 for question in questions if question.blocking)
    round_index = max((r.round_number for r in rounds), default=0)
    converged = any(r.saturated for r in rounds)
    # One per-domain row per staged domain. A converged campaign marks every
    # domain SATURATED so the projection's all-converged precedence resolves
    # SATURATED (the good terminal is *derived*, not pinned -- SATURATED is not
    # an operator-pinned terminal kind); otherwise the domains are READY so the
    # frontier reads RUNNABLE.
    domain_status = DomainProgressStatus.SATURATED if converged else DomainProgressStatus.READY
    domains = tuple(
        DomainProgress(domain=domain, status=domain_status)
        for campaign in campaigns
        for domain in campaign.domains
    )
    return CampaignProgressState.project(
        round_index=round_index,
        domains=domains,
        blocking_count=blocking,
    )


def _muted_sigil_markup(*, mode: RenderMode) -> str:
    """Return the muted inert-status sigil markup in the active render *mode*.

    A claim / question whose status maps onto no live lifecycle shape
    (``SUPERSEDED`` / ``DROPPED``) renders the pending hollow-circle glyph
    -- the most inert lifecycle shape the sigils helper carries -- tinted
    ``$muted`` so it reads as set aside rather than as a live pending row.

    Args:
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the muted pending glyph.
    """
    glyph = escape_markup(sigils.glyph(Sigil.PENDING, mode=mode))
    return f"[$muted]{glyph}[/]"


def _sigil_markup(sigil: Sigil | None, *, mode: RenderMode) -> str:
    """Return *sigil*'s shape tinted by its lifecycle status, or the muted dot.

    Composes the SHAPE (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) and the
    COLOUR (:func:`~eawf.surfaces.tui.widgets.sigils.tint`) from the sigils
    helper so a status renders as a tinted lifecycle mark, never a raw status
    word. A ``None`` *sigil* (a status with no live lifecycle shape) falls back
    to the muted inert dot.

    Args:
        sigil: The lifecycle sigil the status maps onto, or ``None`` for an
            inert status (rendered as the muted dot).
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the tinted lifecycle glyph, or the muted dot.
    """
    if sigil is None:
        return _muted_sigil_markup(mode=mode)
    glyph = escape_markup(sigils.glyph(sigil, mode=mode))
    hue = sigils.tint(sigil)
    if hue is None:
        return f"[$muted]{glyph}[/]"
    return f"[{hue}]{glyph}[/]"


def claim_sigil_markup(status: ClaimStatus, *, mode: RenderMode) -> str:
    """Return the lifecycle sigil markup for a claim *status*.

    Maps the claim status onto its lifecycle sigil (:data:`_CLAIM_SIGIL`) and
    renders the tinted shape via :func:`_sigil_markup` -- the SHAPE + COLOUR
    both come from the sigils helper, so the claim row leads with a sigil
    rather than a raw status word and never falls through to a ``?``.

    Args:
        status: The claim's :class:`~eawf.kernel.state.enums.ClaimStatus`.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the claim status's tinted lifecycle sigil.
    """
    return _sigil_markup(_CLAIM_SIGIL[status], mode=mode)


def question_sigil_markup(status: OpenQuestionStatus, *, mode: RenderMode) -> str:
    """Return the lifecycle sigil markup for an open-question *status*.

    Maps the question status onto its lifecycle sigil (:data:`_QUESTION_SIGIL`)
    and renders the tinted shape via :func:`_sigil_markup`. ``BLOCKED`` is not
    in the map -- a blocking question short-circuits to the literal
    ``blocking`` marker before this path -- so the lookup covers only the
    shape-bearing statuses (``OPEN`` / ``ANSWERED`` / ``DROPPED``).

    Args:
        status: The question's
            :class:`~eawf.kernel.state.enums.OpenQuestionStatus`.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the question status's tinted lifecycle sigil.

    Raises:
        KeyError: If *status* is ``BLOCKED`` (handled upstream) or any value
            outside the shape-bearing subset.
    """
    return _sigil_markup(_QUESTION_SIGIL[status], mode=mode)


def round_sigil_markup(round_state: RoundState, *, mode: RenderMode) -> str:
    """Return the FA8 auto-run sigil markup for a campaign round *round_state*.

    Maps the round state onto its lifecycle sigil (:data:`_ROUND_SIGIL`) and
    renders the tinted shape via :func:`_sigil_markup` -- a running round leads
    with the live diamond, a saturated round the closed circle, a pruned round
    the withheld abandoned mark, and an idle (pre-auto-run) round the pending
    ring -- so the round row reads its auto-run state off a sigil rather than a
    flat dispatch arrow.

    Args:
        round_state: The round's :class:`RoundState`.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the round state's tinted lifecycle sigil.
    """
    return _sigil_markup(_ROUND_SIGIL[round_state], mode=mode)


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


def parse_domains(raw: str) -> tuple[str, ...]:
    """Split the compose-form domains field into a deduplicated domain tuple.

    The compose modal collects domains as one free-text field: comma- and / or
    whitespace-separated domain names. This normaliser splits on commas first,
    then trims each piece, drops empties, and deduplicates while preserving
    first-seen order so a typo'd double-entry collapses to one staged domain.
    An empty / whitespace-only field yields the empty tuple -- the boundary case
    the modal rejects (a campaign stages at least one domain).

    Args:
        raw: The raw domains-field buffer text.

    Returns:
        The cleaned, deduplicated domain names in first-seen order; empty when
        the field carries no usable domain.
    """
    seen: dict[str, None] = {}
    for piece in raw.replace("\n", ",").split(","):
        domain = piece.strip()
        if domain and domain not in seen:
            seen[domain] = None
    return tuple(seen)


def build_research_block(domains: tuple[str, ...]) -> ResearchProfileBlock:
    """Build a :class:`ResearchProfileBlock` from the composed *domains*.

    Each domain stages with the block-level default depth (the compose modal
    does not yet collect per-domain depth / focus -- those default through the
    :class:`~eawf.kernel.spec.research_campaign.ResearchDomainConfig` defaults).
    The block is the staging input the ``research.stage_campaign`` RPC fans the
    topic out across.

    Args:
        domains: The cleaned domain names the campaign stages across.

    Returns:
        The typed ``research:`` block carrying one default-tuned domain config
        per name.
    """
    return ResearchProfileBlock(domains={domain: ResearchDomainConfig() for domain in domains})


@dataclass(frozen=True)
class CampaignDraft:
    """A composed-but-not-yet-staged campaign (the compose modal's payload).

    The :class:`ComposeCampaignModal` dismisses with this draft on commit; the
    board's :meth:`ResearchBoardModeScreen.action_new_campaign` callback fans it
    out to the ``research.stage_campaign`` RPC off the UI thread. The draft
    carries the operator's raw topic verbatim (an empty / whitespace topic is
    NOT pre-rejected here -- the daemon owns that verdict so the rejection
    surfaces honestly through the real staging path, not a faked client-side
    check).

    Attributes:
        topic: The campaign topic to fan out across the block's domains, as the
            operator typed it (may be empty -- the daemon rejects an empty
            topic).
        block: The typed ``research:`` block the campaign stages from, carrying
            at least one composed domain.
    """

    topic: str
    block: ResearchProfileBlock


class ComposeCampaignModal(ModalScreen["CampaignDraft | None"]):
    """Campaign compose-form modal (returns a :class:`CampaignDraft` on commit).

    A two-field form -- a topic line + a domains line (comma- / whitespace-
    separated) -- the board's ``n`` key opens. ``Enter`` commits: the buffers
    parse into a topic + a :class:`ResearchProfileBlock` and dismiss as a
    :class:`CampaignDraft`; ``Esc`` cancels (dismisses ``None``, so the board
    issues zero RPCs). A commit with no parsed domain stays open with an inline
    notice (a campaign stages at least one domain), so the only dismiss-with-
    draft path carries a usable block. The topic is NOT validated here -- an
    empty topic dismisses a draft so the daemon's rejection surfaces honestly
    through the real staging path rather than a faked client-side guard.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ComposeCampaignModal {
        align: center middle;
    }
    ComposeCampaignModal > #compose-box {
        width: 70%;
        max-width: 90;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ComposeCampaignModal .compose-label {
        text-style: bold;
        color: $accent;
        height: auto;
    }
    ComposeCampaignModal .compose-field {
        margin-bottom: 1;
        border: tall $accent;
    }
    ComposeCampaignModal #compose-error {
        color: $error;
        height: auto;
    }
    ComposeCampaignModal .compose-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Esc`` cancels (dismisses ``None``). ``Enter`` commits via the focused
    #: :class:`Input`'s ``Submitted`` message, not a screen binding -- see
    #: :meth:`on_input_submitted` -- so the same key commits from either field.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel", show=False),
    ]

    #: Widget ids (addressable so the commit reads the buffers + the test drives
    #: the fields without re-deriving the layout).
    TOPIC_INPUT_ID: ClassVar[str] = "compose-topic"
    DOMAINS_INPUT_ID: ClassVar[str] = "compose-domains"
    ERROR_ID: ClassVar[str] = "compose-error"

    #: Inline notice shown when a commit carries no parsed domain (the modal
    #: stays open so the operator can add one without retyping the topic).
    NO_DOMAIN_NOTICE: ClassVar[str] = "add at least one domain"

    def compose(self) -> ComposeResult:
        """Yield the topic + domains inputs above the inline notice + hint row."""
        with Vertical(id="compose-box"):
            yield Static("new research campaign", classes="compose-label")
            yield Static("[$muted]topic[/]", classes="compose-label")
            yield Input(
                placeholder="campaign topic",
                id=self.TOPIC_INPUT_ID,
                classes="compose-field",
            )
            yield Static("[$muted]domains (comma-separated)[/]", classes="compose-label")
            yield Input(
                placeholder="market-structure, pricing-models",
                id=self.DOMAINS_INPUT_ID,
                classes="compose-field",
            )
            # markup=False -- the notice is literal copy, never a tag.
            yield Static("", id=self.ERROR_ID, markup=False)
            yield Static("[ Enter commit - Esc cancel ]", classes="compose-hint")

    def on_mount(self) -> None:
        """Focus the topic input so the operator types the topic first."""
        self.query_one(f"#{self.TOPIC_INPUT_ID}", Input).focus()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        """Commit on ``Enter`` (either field's ``Submitted`` message)."""
        message.stop()
        self.action_commit()

    def action_commit(self) -> None:
        """Commit the form: dismiss a draft, or report the missing-domain notice.

        Parses the domains field; with no usable domain the modal stays open and
        renders the :data:`NO_DOMAIN_NOTICE` inline so the operator adds one. On
        at least one domain the topic + the composed block dismiss as a
        :class:`CampaignDraft` -- the topic passes through unvalidated so the
        daemon owns the empty-topic verdict and its rejection surfaces honestly.
        """
        topic = self.query_one(f"#{self.TOPIC_INPUT_ID}", Input).value
        domains = parse_domains(self.query_one(f"#{self.DOMAINS_INPUT_ID}", Input).value)
        if not domains:
            self.query_one(f"#{self.ERROR_ID}", Static).update(self.NO_DOMAIN_NOTICE)
            return
        logger.info(f"compose_campaign commit topic={topic!r} domains={len(domains)}")
        self.dismiss(CampaignDraft(topic=topic, block=build_research_block(domains)))

    def action_cancel(self) -> None:
        """Dismiss with ``None`` (``Esc`` = cancel; the board issues no RPC)."""
        logger.info("compose_campaign cancel")
        self.dismiss(None)


class OperatorNoteModal(ModalScreen["str | None"]):
    """One-field operator-channel modal (returns the entered note on commit).

    The board's operator-channel keys -- ``o`` (add-question) and ``t``
    (steer) -- push a single free-text line into the running campaign, so each
    opens this one-field modal: a labelled :class:`Input` whose ``Enter``
    commits the trimmed text (``Esc`` cancels, dismissing ``None`` so the board
    issues no RPC). A commit with an empty / whitespace-only line stays open
    with the :data:`EMPTY_NOTICE_TEMPLATE` inline notice so the operator never
    sends a blank note; the only dismiss-with-text path carries a usable line.
    The label + placeholder are supplied per channel so one modal serves both
    the add-question and steer surfaces without a second widget tree.
    """

    DEFAULT_CSS: ClassVar[str] = """
    OperatorNoteModal {
        align: center middle;
    }
    OperatorNoteModal > #note-box {
        width: 70%;
        max-width: 90;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    OperatorNoteModal .note-label {
        text-style: bold;
        color: $accent;
        height: auto;
    }
    OperatorNoteModal .note-field {
        margin-bottom: 1;
        border: tall $accent;
    }
    OperatorNoteModal #note-error {
        color: $error;
        height: auto;
    }
    OperatorNoteModal .note-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Esc`` cancels (dismisses ``None``). ``Enter`` commits via the focused
    #: :class:`Input`'s ``Submitted`` message (see :meth:`on_input_submitted`).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel", show=False),
    ]

    #: Widget ids (addressable so the commit reads the buffer + a test drives
    #: the field without re-deriving the layout).
    NOTE_INPUT_ID: ClassVar[str] = "note-input"
    ERROR_ID: ClassVar[str] = "note-error"

    #: Inline notice shown when a commit carries no non-whitespace text (the
    #: modal stays open). Formatted with the channel noun so each reads clearly.
    EMPTY_NOTICE_TEMPLATE: ClassVar[str] = "{noun} cannot be empty"

    def __init__(self, *, title: str, label: str, placeholder: str, noun: str) -> None:
        """Initialise the modal with its per-channel label / placeholder / noun.

        Args:
            title: The bold heading line at the top of the modal box.
            label: The muted field label above the input.
            placeholder: The input placeholder text.
            noun: The channel noun used in the empty-line notice (e.g.
                ``"question"`` / ``"steer note"``).
        """
        super().__init__()
        self._title = title
        self._label = label
        self._placeholder = placeholder
        self._noun = noun

    def compose(self) -> ComposeResult:
        """Yield the labelled note input above the inline notice + hint row."""
        with Vertical(id="note-box"):
            yield Static(self._title, classes="note-label")
            yield Static(f"[$muted]{escape_markup(self._label)}[/]", classes="note-label")
            yield Input(
                placeholder=self._placeholder,
                id=self.NOTE_INPUT_ID,
                classes="note-field",
            )
            # markup=False -- the notice is literal copy, never a tag.
            yield Static("", id=self.ERROR_ID, markup=False)
            yield Static("[ Enter send - Esc cancel ]", classes="note-hint")

    def on_mount(self) -> None:
        """Focus the note input so the operator types straight away."""
        self.query_one(f"#{self.NOTE_INPUT_ID}", Input).focus()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        """Commit on ``Enter`` (the input's ``Submitted`` message)."""
        message.stop()
        self.action_commit()

    def action_commit(self) -> None:
        """Commit the note: dismiss the trimmed text, or report the empty notice.

        A non-whitespace line dismisses as the trimmed string the board routes
        through the channel RPC; an empty / whitespace-only line stays open and
        renders the :data:`EMPTY_NOTICE_TEMPLATE` inline so the operator never
        sends a blank note.
        """
        note = self.query_one(f"#{self.NOTE_INPUT_ID}", Input).value.strip()
        if not note:
            self.query_one(f"#{self.ERROR_ID}", Static).update(
                self.EMPTY_NOTICE_TEMPLATE.format(noun=self._noun)
            )
            return
        logger.info(f"operator_note commit noun={self._noun!r} chars={len(note)}")
        self.dismiss(note)

    def action_cancel(self) -> None:
        """Dismiss with ``None`` (``Esc`` = cancel; the board issues no RPC)."""
        logger.info(f"operator_note cancel noun={self._noun!r}")
        self.dismiss(None)


class NodeKind(StrEnum):
    """Closed vocabulary for the kind of a topic-tree node.

    The tree is a campaign > round > topic > unresolved-question outline; each
    rendered node carries its kind so :meth:`ResearchBoardModeScreen.action_peek_selected`
    can describe the peeked node honestly without re-deriving its level.

    Members:
        CAMPAIGN: A staged-campaign node (the tree root per campaign).
        ROUND: The synthetic round node under a campaign (round 1 -- the
            campaign is staged, not yet multi-round-run). The open questions
            group under this round, so the round is also emitted for a
            question-only scope that has staged no campaign yet.
        TOPIC: A staged-domain topic node under a round.
        QUESTION: An :class:`~eawf.kernel.state.models.OpenQuestion` node
            grouped under the round, carrying its open / answered / dropped
            status as its per-status lifecycle sigil.
        CLAIM: A :class:`~eawf.kernel.state.models.Claim` leaf nested under
            the question it answers (the round > questions > claims spine).
    """

    CAMPAIGN = "campaign"
    ROUND = "round"
    TOPIC = "topic"
    QUESTION = "question"
    CLAIM = "claim"


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
        question_status: The :class:`~eawf.kernel.state.enums.OpenQuestionStatus`
            of a :attr:`NodeKind.QUESTION` node, so the row renders the per-
            status open / answered / dropped sigil; ``None`` on every other
            node kind.
        claim_status: The :class:`~eawf.kernel.state.enums.ClaimStatus` of a
            :attr:`NodeKind.CLAIM` node, so the nested claim row renders the
            claim's lifecycle sigil; ``None`` on every other node kind.
        round_state: The :class:`RoundState` of a :attr:`NodeKind.ROUND` node,
            so the round row renders the FA8 auto-run running / saturated /
            pruned sigil rather than a flat dispatch arrow; ``None`` on every
            other node kind.
    """

    kind: NodeKind
    label: str
    depth: int
    detail: str
    campaign_id: str | None = None
    question_status: OpenQuestionStatus | None = None
    claim_status: ClaimStatus | None = None
    round_state: RoundState | None = None


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
        The live campaign rows in first-seen order; empty when no campaign
        store exists for the scope. The store is append-only, so a campaign
        re-appended by a cancel resolves to its LATEST row per
        ``campaign_id`` (latest-wins, mirroring the daemon's
        ``read_latest_campaign``), and a campaign whose latest status is
        :attr:`~eawf.kernel.state.enums.CampaignStatus.CANCELLED` is dropped
        -- a tombstoned campaign is not live research signal on the board.
    """
    if state_path is None:
        return ()
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return ()
    # Collapse the append-only store to the latest payload per campaign_id,
    # preserving first-seen order so the board reads stably.
    latest: dict[str, ResearchCampaignPayload] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = ResearchCampaignPayload.model_validate(envelope.payload)
        latest[payload.campaign_id] = payload
    rows: list[CampaignRow] = []
    cancelled = 0
    for payload in latest.values():
        if payload.status is CampaignStatus.CANCELLED:
            cancelled += 1
            continue
        campaign = payload.campaign
        rows.append(
            CampaignRow(
                campaign_id=payload.campaign_id,
                topic=campaign.topic,
                domains=tuple(dispatch.domain for dispatch in campaign.dispatches),
                default_depth=payload.config.default_depth.value,
            )
        )
    logger.info(
        f"read_campaign_rows campaigns={len(rows)} cancelled={cancelled} path={path.name!r}"
    )
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


def index_claims_by_question(
    claims: tuple[Claim, ...],
) -> dict[str, tuple[Claim, ...]]:
    """Group *claims* by the open-question id each one answers.

    A claim that back-links an :class:`~eawf.kernel.state.models.OpenQuestion`
    via :attr:`~eawf.kernel.state.models.Claim.answers_question_id` nests under
    that question in the round > questions > claims tree; a free-standing claim
    (``answers_question_id`` is ``None``) is omitted from the mapping entirely,
    so it never fabricates a parentless tree leaf. Order within each group is
    preserved from the input (the natural-id sort the caller supplies).

    Args:
        claims: The state-resident claim rows for the scope.

    Returns:
        A mapping of question id to the claims that answer it, in input order;
        empty when no claim back-links a question.
    """
    grouped: dict[str, list[Claim]] = {}
    for claim in claims:
        question_id = claim.answers_question_id
        if question_id is not None:
            grouped.setdefault(question_id, []).append(claim)
    return {question_id: tuple(rows) for question_id, rows in grouped.items()}


#: :class:`RoundState` -> the round node's auto-run label + peek detail. The
#: label leads the round row (``round running`` / ``saturated`` / ``pruned``)
#: so the auto-run state reads off the row beside its sigil; the detail is the
#: honest peek line surfaced when the round node is peeked. Once a campaign run
#: has persisted round records the state derives from the real run
#: (:func:`classify_round_state_from_records`); the IDLE line is the honest
#: pre-run surface for a campaign that has staged but not yet run.
_ROUND_COPY: dict[RoundState, tuple[str, str]] = {
    RoundState.RUNNING: ("round running", "live round -- run in flight, gathering findings"),
    RoundState.SATURATED: ("round saturated", "run saturated -- the campaign converged"),
    RoundState.PRUNED: ("round pruned", "round pruned -- candidates dropped, none kept"),
    RoundState.IDLE: ("round idle", "campaign staged -- no round has run yet"),
}


def _round_label_detail(progress: RoundProgress) -> tuple[str, str]:
    """Return the round node's auto-run label + peek detail for *progress*.

    Maps the classified :class:`RoundState` onto its label + detail copy
    (:data:`_ROUND_COPY`) so the running / saturated / pruned grammar reads off
    the round row without re-deriving the state at the call site.

    Args:
        progress: The derived round progress.

    Returns:
        A ``(label, detail)`` pair for the round node.
    """
    return _ROUND_COPY[progress.state]


def build_tree_nodes(
    campaigns: tuple[CampaignRow, ...],
    questions: tuple[OpenQuestion, ...],
    *,
    claims: tuple[Claim, ...] = (),
    rounds: tuple[Round, ...] = (),
) -> tuple[TreeNode, ...]:
    """Flatten the campaign + question + claim rows into the topic-tree node list.

    Builds the campaign > round > topic outline plus the round > questions >
    claims spine as a flat tuple of :class:`TreeNode` rows in render order:

    * per campaign -- the campaign node, then a round node (its FA8 auto-run
      state derived from the real run records when present, else the pre-run
      question-ledger classification), then one topic node per staged domain
      (capped at :data:`_MAX_TOPICS_PER_CAMPAIGN`);
    * then, when the scope carries open questions, a single
      ``round -- questions`` round node grouping every
      :class:`~eawf.kernel.state.models.OpenQuestion` as a question node (each
      carrying its open / answered / dropped status for the per-status sigil),
      with the claims that answer a question
      (:func:`index_claims_by_question`) nested as claim leaves beneath it.

    A question is scope-wide (not pinned to one campaign's topic), so the
    questions round is emitted once for the scope rather than per campaign, and
    it surfaces even for a question-only scope that has staged no campaign yet.
    Empty when the scope has neither a campaign nor an open question.

    Args:
        campaigns: The staged campaign rows for the scope.
        questions: The state-resident open-question rows for the scope.
        claims: The state-resident claim rows; the ones that back-link a
            question nest under it (free-standing claims are not tree leaves).
        rounds: The executed round records; when present the round node's FA8
            auto-run state derives from the real run (the W08 live-wire).

    Returns:
        The flattened tree nodes in render order; empty when nothing to show.
    """
    progress = compute_round_progress(campaigns, claims, questions, rounds)
    round_label, round_detail = _round_label_detail(progress)
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
                label=round_label,
                depth=1,
                detail=round_detail,
                campaign_id=campaign.campaign_id,
                round_state=progress.state,
            )
        )
        for domain in campaign.domains[:_MAX_TOPICS_PER_CAMPAIGN]:
            nodes.append(
                TreeNode(
                    kind=NodeKind.TOPIC,
                    # The leaf IS a domain (the campaign topic is fanned across
                    # domains); label it "domain: <name>" so the tree reads
                    # campaign-topic -> round -> domain, not a bare name that
                    # reads as another topic.
                    label=f"domain: {domain}",
                    depth=2,
                    detail=f"staged domain of campaign topic {campaign.topic!r}",
                    campaign_id=campaign.campaign_id,
                )
            )
    if questions:
        nodes.append(
            TreeNode(
                kind=NodeKind.ROUND,
                label=f"{round_label} -- questions",
                depth=1,
                detail=(
                    f"{progress.open_count} open / {progress.answered_count} answered / "
                    f"{progress.pruned_count} pruned"
                ),
                round_state=progress.state,
            )
        )
        claims_by_question = index_claims_by_question(claims)
        for question in questions:
            marker = "blocking" if question.blocking else question.status.value
            nodes.append(
                TreeNode(
                    kind=NodeKind.QUESTION,
                    label=question.title,
                    depth=2,
                    detail=f"open question -- {marker}",
                    question_status=question.status,
                )
            )
            for claim in claims_by_question.get(question.id, ()):
                nodes.append(
                    TreeNode(
                        kind=NodeKind.CLAIM,
                        label=claim.title,
                        depth=3,
                        detail=f"candidate answer -- {claim.status.value}",
                        claim_status=claim.status,
                    )
                )
    return tuple(nodes)


#: :class:`NodeKind` -> the sigils-helper chrome role its structural level glyph
#: renders: the campaign root is the ``overview`` triple-bar, every round node
#: the ``dispatch`` arrow. The shape-bearing leaves are NOT in this map -- a
#: staged topic draws the ``PENDING`` lifecycle sigil (:data:`_TREE_SIGIL`),
#: and a question / claim leaf draws its OWN per-status lifecycle sigil (via
#: :func:`question_sigil_markup` / :func:`claim_sigil_markup`) so the open /
#: answered / dropped status reads off the row, never a flat pending dot.
_TREE_CHROME: dict[NodeKind, str] = {
    NodeKind.CAMPAIGN: "overview",
    NodeKind.ROUND: "dispatch",
}

#: :class:`NodeKind` -> the fixed lifecycle :class:`Sigil` its level glyph
#: renders when the node carries no per-status sigil. Only the staged-topic
#: leaf is fixed (always ``PENDING`` -- a staged topic is not yet run);
#: question / claim leaves resolve their sigil from the node's own status.
_TREE_SIGIL: dict[NodeKind, Sigil] = {
    NodeKind.TOPIC: Sigil.PENDING,
}


def _tree_node_markup(node: TreeNode, *, mode: RenderMode) -> str:
    """Return the tinted level-sigil markup for a tree *node*.

    Resolves the node's level mark through the sigils helper -- a muted chrome
    role for the structural campaign / round nodes, the fixed pending sigil for
    a staged topic, and the per-status lifecycle sigil (open / answered /
    dropped for a question; the claim's own status for a candidate-answer
    claim) for the shape-bearing leaves. No node kind falls through to a raw
    glyph or a ``?`` -- every level resolves through the sigils helper.

    Args:
        node: The tree node whose level sigil to render.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the node's tinted level sigil.
    """
    if node.kind is NodeKind.QUESTION and node.question_status is not None:
        return question_sigil_markup(node.question_status, mode=mode)
    if node.kind is NodeKind.CLAIM and node.claim_status is not None:
        return claim_sigil_markup(node.claim_status, mode=mode)
    if node.kind is NodeKind.ROUND and node.round_state is not None:
        return round_sigil_markup(node.round_state, mode=mode)
    role = _TREE_CHROME.get(node.kind)
    if role is not None:
        glyph = escape_markup(sigils.chrome(role, mode=mode))
    else:
        glyph = escape_markup(sigils.glyph(_TREE_SIGIL[node.kind], mode=mode))
    return f"[$muted]{glyph}[/]"


def render_tree(
    nodes: tuple[TreeNode, ...], selected: int, *, mode: RenderMode = DEFAULT_RENDER_MODE
) -> str:
    """Render the topic-tree pane (one indented row per node).

    Each row carries a sigils-helper level sigil -- the ``overview`` mark for a
    campaign root, the ``dispatch`` arrow for a round, the pending sigil for a
    staged topic, and the per-status open / answered / dropped lifecycle sigil
    for a question leaf (its answering claims nested one indent deeper with
    their own claim sigil) -- and the node label, indented by depth. The
    *selected* row is marked so the peek target is visible. An empty node list
    renders the per-pane :data:`NONE_YET` sentinel.

    Args:
        nodes: The flattened tree nodes in render order.
        selected: Index of the selected node (the peek target), or ``-1``.
        mode: The App's resolved render-mode label -- selects each glyph's
            ASCII / unicode column.

    Returns:
        A content-markup string of one tree row per node.
    """
    if not nodes:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for index, node in enumerate(nodes):
        indent = "  " * node.depth
        sigil = _tree_node_markup(node, mode=mode)
        marker = "[$accent]>[/] " if index == selected else "  "
        lines.append(f"{marker}{indent}{sigil} {escape_markup(node.label)}")
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


def render_claims(claims: tuple[Claim, ...], *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the claim-ledger (center pane Claims tab) -- one row per claim.

    Each row LEADS with the claim status's lifecycle sigil (shape + tint both
    from the sigils helper via :func:`claim_sigil_markup`) followed by the
    title -- a sigil per claim, never a raw status word. The rows are capped at
    :data:`_MAX_ROWS` with a ``+N more`` overflow line. An empty claim ledger
    renders :data:`NONE_YET`.

    Args:
        claims: The state-resident claim rows for the scope.
        mode: The App's resolved render-mode label -- selects each sigil's
            ASCII / unicode column.

    Returns:
        A content-markup string of one claim line per row.
    """
    if not claims:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for claim in claims[:_MAX_ROWS]:
        sigil = claim_sigil_markup(claim.status, mode=mode)
        lines.append(f"{sigil} {escape_markup(claim.title)}")
    overflow = len(claims) - _MAX_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def group_claims_by_evidence(
    claims: tuple[Claim, ...],
) -> tuple[tuple[Claim, ...], tuple[Claim, ...]]:
    """Split *claims* into the supporting and contradicting evidence groups.

    A claim's :class:`~eawf.kernel.state.enums.ClaimStatus` is its evidence
    verdict: a ``SUPPORTED`` claim carries supporting evidence (a live candidate
    answer), a ``REFUTED`` claim carries contradicting evidence (a conflict).
    An ``OPEN`` or ``SUPERSEDED`` claim is neither yet -- it carries no settled
    evidence verdict, so it joins neither group. Order within each group is
    preserved from the input (the natural-id sort the caller supplies).

    Args:
        claims: The state-resident claim rows for the scope.

    Returns:
        A ``(supporting, contradicting)`` pair: the ``SUPPORTED`` claims and
        the ``REFUTED`` claims, each in input order.
    """
    supporting = tuple(c for c in claims if c.status is ClaimStatus.SUPPORTED)
    contradicting = tuple(c for c in claims if c.status is ClaimStatus.REFUTED)
    return supporting, contradicting


def _render_claim_group(claims: tuple[Claim, ...], *, mode: RenderMode) -> str:
    """Render one claim group -- one row per claim (sigil + evidence count + title).

    Each row LEADS with the claim status's lifecycle sigil (via
    :func:`claim_sigil_markup`), then its evidence-ref count (``N ref(s)``) and
    the title, so the operator reads how strongly each grouped claim is backed.
    Rows cap at :data:`_MAX_ROWS` with a ``+N more`` overflow line; an empty
    group renders :data:`NONE_YET`.

    Args:
        claims: The claims in one evidence group (supporting or contradicting).
        mode: The App's resolved render-mode label -- selects each sigil's
            ASCII / unicode column.

    Returns:
        A content-markup string of one claim line per row.
    """
    if not claims:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for claim in claims[:_MAX_ROWS]:
        refs = len(claim.evidence_refs)
        sigil = claim_sigil_markup(claim.status, mode=mode)
        lines.append(f"{sigil} [$muted]{refs} ref(s)[/] {escape_markup(claim.title)}")
    overflow = len(claims) - _MAX_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def render_options(claims: tuple[Claim, ...], *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the Options tab -- claims grouped by supporting evidence.

    Projects the supporting-evidence group from
    :func:`group_claims_by_evidence` (the ``SUPPORTED`` claims -- the live
    candidate answers backed by evidence), one row per claim with its lifecycle
    sigil + evidence-ref count. An empty group renders :data:`NONE_YET`.

    Args:
        claims: The state-resident claim rows for the scope.
        mode: The App's resolved render-mode label -- selects each sigil's
            ASCII / unicode column.

    Returns:
        A content-markup string of one supporting-claim row per line.
    """
    supporting, _ = group_claims_by_evidence(claims)
    return _render_claim_group(supporting, mode=mode)


def render_conflicts(claims: tuple[Claim, ...], *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the Conflicts tab -- claims grouped by contradicting evidence.

    Projects the contradicting-evidence group from
    :func:`group_claims_by_evidence` (the ``REFUTED`` claims -- the conflicts
    the campaign surfaced), one row per claim with its lifecycle sigil +
    evidence-ref count. An empty group renders :data:`NONE_YET`.

    Args:
        claims: The state-resident claim rows for the scope.
        mode: The App's resolved render-mode label -- selects each sigil's
            ASCII / unicode column.

    Returns:
        A content-markup string of one contradicting-claim row per line.
    """
    _, contradicting = group_claims_by_evidence(claims)
    return _render_claim_group(contradicting, mode=mode)


def render_unresolved(
    questions: tuple[OpenQuestion, ...],
    *,
    round_number: int = STAGED_ROUND,
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> str:
    """Render the Unresolved tab -- one row per open question + status + round.

    Lists one row per :class:`~eawf.kernel.state.models.OpenQuestion`, each
    naming the question's status (a blocking question reads ``blocking`` in
    ``$warn``, else its lifecycle status) and the round it belongs to. Every row
    renders under the same synthetic *round_number* the topic tree labels
    (:data:`STAGED_ROUND`, the staged round before the live multi-round runner).
    Rows cap at :data:`_MAX_ROWS` with a ``+N more`` line; an empty ledger
    renders :data:`NONE_YET`.

    Args:
        questions: The state-resident open-question rows for the scope.
        round_number: The round number every row renders under (defaults to
            :data:`STAGED_ROUND`).

    Returns:
        A content-markup string of one question line per row.
    """
    if not questions:
        return f"[$muted]{NONE_YET}[/]"
    lines: list[str] = []
    for question in questions[:_MAX_ROWS]:
        if question.blocking:
            # A blocking question leads with the attention sigil so the warn
            # signal reads as a glyph (the sigil language), not only a tinted
            # word; a non-blocking question keeps its lifecycle status word.
            marker = f"[$warn]{escape_markup(sigils.chrome('attention', mode=mode))}[/] "
            tint = "$warn"
            status = "blocking"
        else:
            marker = ""
            tint = "$accent"
            status = question.status.value
        lines.append(
            f"{marker}[{tint}]{status}[/] [$muted]round {round_number}[/] "
            f"{escape_markup(question.title)}"
        )
    overflow = len(questions) - _MAX_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


#: :class:`RoundState` -> the ROUND band's auto-run phrase (the human reading
#: of the round's classified state for the progress pane). A running round
#: reads ``running``, a saturated one ``saturated``, a pruned one ``pruned``,
#: and an idle (pre-auto-run) one ``staged``.
_ROUND_BAND_PHRASE: dict[RoundState, str] = {
    RoundState.RUNNING: "running",
    RoundState.SATURATED: "saturated",
    RoundState.PRUNED: "pruned",
    RoundState.IDLE: "staged",
}


def render_progress(
    campaigns: tuple[CampaignRow, ...],
    claims: tuple[Claim, ...],
    questions: tuple[OpenQuestion, ...],
    *,
    checkpoints: int,
    rounds: tuple[Round, ...] = (),
) -> str:
    """Render the right-pane progress / budget bands.

    The bands -- RUN / ROUND / ACTIVE / WAITING / PAUSED / BUDGET / RISKS --
    are derived from the staged campaign + the ledgers + the open checkpoints +
    the executed rounds. The RUN band surfaces the
    :class:`~eawf.kernel.spec.operator_input.CampaignProgressState` answer to
    "can the campaign proceed" (runnable / blocked / saturated) folded over the
    real executed rounds (:func:`project_campaign_progress`); the ROUND band
    counts the rounds the run actually executed (``N/N``), retiring the synthetic
    ``1/1`` placeholder once a run has persisted rounds. The BUDGET band reads
    the staged-topic spend + the saturated / pruned tallies.

    Args:
        campaigns: The staged campaign rows for the scope.
        claims: The state-resident claim ledger rows.
        questions: The state-resident open-question rows.
        checkpoints: The count of open ``needs_user`` checkpoints for the
            scope (the PAUSED band).
        rounds: The executed rounds read from the round store; empty for a
            staged-but-not-run campaign (the ROUND band then reads ``0`` run).

    Returns:
        A content-markup string of one band per line.
    """
    progress = compute_round_progress(campaigns, claims, questions, rounds)
    blocking = sum(1 for question in questions if question.blocking)
    campaign_progress = project_campaign_progress(campaigns, claims, questions, rounds)
    rounds_run = max((r.round_number for r in rounds), default=0)
    run_phrase = "idle" if not campaigns else campaign_progress.kind.value
    # While a round is in flight the completed-round count is still 0 (the round
    # record persists on completion), which read as a stalled "ROUND 0 run" even
    # though claims were accruing. Surface the running round + live claim count
    # instead, consistent with the tree's "round running" node.
    if progress.state is RoundState.RUNNING:
        running_round = max(rounds_run, 1)
        round_band = (
            f"[$accent]ROUND[/] [$muted]{running_round} running · {len(claims)} claim(s)[/]"
        )
    else:
        round_band = f"[$accent]ROUND[/] [$muted]{rounds_run} run[/]"
    lines = [
        f"[$accent]RUN[/] [$muted]{run_phrase}[/]",
        round_band,
        f"[$accent]ACTIVE[/] [$muted]{len(claims)} claim(s)[/]",
        f"[$accent]WAITING[/] [$muted]{progress.open_count} open question(s)[/]",
        f"[$accent]PAUSED[/] [$muted]{checkpoints} checkpoint(s)[/]",
        (
            f"[$accent]BUDGET[/] [$muted]{progress.spent_topics} staged topic(s), "
            f"{progress.answered_count} answered, {progress.pruned_count} pruned[/]"
        ),
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
    (snapshot) route through the daemon-client seam to the live
    ``research.followup`` / ``research.snapshot`` RPCs for the selected
    campaign, surfacing the honest next-round / run-summary result. ``o``
    (add-question), ``t`` (steer), ``b`` (broadcast), and ``v`` (override) are
    the four FA8 operator-input campaign-fork channels: each collects a
    free-text line through a one-field :class:`OperatorNoteModal` and routes it
    off the UI thread to its live ``research.add_question`` / ``research.steer``
    / ``research.broadcast`` / ``research.override`` RPC, surfacing the daemon's
    honest sent / rejection result. No action ever fakes an outcome that did
    not happen.

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
        border: round $accent;
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
        border: round $warn;
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
        width: 1fr;
        content-align: center middle;
        text-align: center;
        padding: 1 2;
    }
    ResearchBoardModeScreen #research-empty-hero {
        height: 1fr;
        width: 1fr;
        align: center middle;
        padding: 1 2;
    }
    ResearchBoardModeScreen #research-empty-hero #research-empty {
        height: auto;
        margin-top: 1;
    }
    ResearchBoardModeScreen .research-empty-seal {
        width: 1fr;
        height: 19;
        content-align: center middle;
        text-align: center;
        color: $accent;
    }
    ResearchBoardModeScreen .research-center-section {
        height: auto;
        text-style: bold;
        margin-top: 1;
    }
    ResearchBoardModeScreen .research-tree-node {
        height: auto;
    }
    """

    #: ``up`` / ``down`` move the tree cursor; ``enter`` peeks the selected
    #: node read-only. ``a`` approve / ``p`` park route checkpoint resolution
    #: through the real needs_user RPCs; ``r`` follow-up / ``s`` snapshot are
    #: the honest-unavailable idle-contract keys. ``o`` (add-question), ``t``
    #: (steer), ``b`` (broadcast), and ``v`` (override) are the four FA8
    #: operator-input campaign-fork channels -- all FREE keys (no app-wide or
    #: in-pane collision; the app binds ``w/r/u i c q h j k l`` + digits and the
    #: pane keeps arrows primary for the tree), so adding them never displaces
    #: ``s`` (snapshot) / ``a`` (approve) / ``n`` (new) from their live
    #: handlers. The lowercase ``a``/``p``/``r``/``s`` are the brief's canonical
    #: research action keys. The chrome bindings (palette / help / quit / scope
    #: / mode digits) come from the shared chassis + app-wide bindings, so the
    #: pane offers no j/k vim aliases here.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "select_prev", "up", show=False),
        Binding("down", "select_next", "down", show=False),
        Binding("enter", "peek_selected", "peek", show=False),
        Binding("d", "open_brief", "brief", show=False),
        Binding("n", "new_campaign", "new", show=False),
        Binding("o", "add_question", "ask", show=False),
        Binding("t", "steer", "steer", show=False),
        Binding("b", "broadcast", "broadcast", show=False),
        Binding("v", "override", "override", show=False),
        Binding("a", "approve_checkpoint", "approve", show=False),
        Binding("p", "park_checkpoint", "park", show=False),
        Binding("r", "followup", "follow-up", show=False),
        Binding("s", "snapshot", "snapshot", show=False),
        Binding("g", "run_campaign", "run", show=False),
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
        self._tree = build_tree_nodes(
            campaigns, questions, claims=claims, rounds=self._current_round_rows()
        )
        with Vertical(id="research-body"):
            if self.empty:
                if self._render_mode() == "unicode":
                    # Unicode path: the hand-tuned ASCII-art Seal (deterministic
                    # accent-on-surface TEXT, no graphics protocol) is the
                    # centerpiece, so the glyph sigil is dropped from the copy to
                    # avoid a double brand mark. The art centers via the
                    # #research-empty-hero align.
                    with Vertical(id="research-empty-hero"):
                        yield seal_art_widget()
                        yield Static(self._empty_body(with_sigil=False), id=EMPTY_ID)
                else:
                    # ASCII path: the small unicode/ascii brand glyph leads the
                    # hero (the half-block art needs block-glyph coverage).
                    yield Static(self._empty_body(), id=EMPTY_ID)
                return
            pause = self._current_checkpoint()
            mode = self._render_mode()
            with Horizontal(id="research-panes"):
                with VerticalScroll(id=TREE_PANE_ID, classes="research-pane") as tree:
                    tree.border_title = "TOPIC TREE"
                    yield Static(
                        render_tree(self._tree, self.selected, mode=mode), id="research-tree-body"
                    )
                    yield Static(self._peek_idle(), id=PEEK_RESULT_ID)
                with VerticalScroll(id=CENTER_PANE_ID, classes="research-pane") as center:
                    center.border_title = "CLAIMS / EVIDENCE"
                    yield Static(render_center_tabs("Claims"), id="research-center-tabs")
                    yield Static(render_claims(claims, mode=mode), id="research-center-body")
                    yield Static(
                        "[$accent]Options[/] [$muted](supporting evidence)[/]",
                        classes="research-center-section",
                    )
                    yield Static(render_options(claims, mode=mode), id=OPTIONS_BODY_ID)
                    yield Static(
                        "[$accent]Conflicts[/] [$muted](contradicting evidence)[/]",
                        classes="research-center-section",
                    )
                    yield Static(render_conflicts(claims, mode=mode), id=CONFLICTS_BODY_ID)
                    yield Static(
                        "[$accent]Unresolved[/]",
                        id=UNRESOLVED_HEADER_ID,
                        classes="research-center-section",
                    )
                    yield Static(render_unresolved(questions, mode=mode), id=UNRESOLVED_BODY_ID)
                with VerticalScroll(id=PROGRESS_PANE_ID, classes="research-pane") as progress:
                    progress.border_title = "PROGRESS / BUDGET"
                    yield Static(
                        render_progress(
                            campaigns,
                            claims,
                            questions,
                            checkpoints=1 if pause else 0,
                            rounds=self._current_round_rows(),
                        ),
                        id="research-progress-body",
                    )
            drawer = Static(render_checkpoint(pause), id=DRAWER_ID)
            drawer.border_title = "CHECKPOINT"
            yield drawer
            yield Static(ACTION_IDLE, id=ACTION_RESULT_ID)
            # Top mode-key line: the mode-unique action keys, rendered just above
            # the global-nav footer (docked bottom) so the two strips no longer
            # duplicate keys.
            yield Static(MODE_KEYS_LINE, id=MODE_KEYS_ID, classes="research-mode-keys")

    def on_mount(self) -> None:
        """Seed from app state, arm the refresh seam, and clamp the cursor."""
        super().on_mount()
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self.set_interval(RESEARCH_REFRESH_S, self._rebuild)
        self._clamp_selection()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def _on_render_mode(self, _mode: RenderMode) -> None:
        """Repaint the reskinned sigils when the App's render mode swaps."""
        if self.is_mounted:
            self._rebuild()

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
        """Query a follow-up over the campaign's persisted rounds (live RPC).

        Routes through the daemon-client seam to the live ``research.followup``
        RPC for the selected campaign, surfacing the honest next-round figure
        (the round a follow-up run would start at). With no campaign selected
        there is nothing to follow up; when the daemon is unreachable the result
        says so rather than implying a follow-up was queued.
        """
        campaign_id = self._channel_campaign_id()
        if campaign_id is None:
            self._set_action("[$warn]follow-up: no campaign selected[/]")
            return
        result_line = self._issue_run_query(
            verb="follow-up", method=_FOLLOWUP_METHOD, campaign_id=campaign_id
        )
        self._set_action(result_line)

    def action_snapshot(self) -> None:
        """Snapshot the campaign's run state (live RPC).

        Routes through the daemon-client seam to the live ``research.snapshot``
        RPC for the selected campaign, surfacing the honest run summary (rounds
        run + saturation). With no campaign selected there is nothing to
        snapshot; an unreachable daemon surfaces the honest unavailable line.
        """
        campaign_id = self._channel_campaign_id()
        if campaign_id is None:
            self._set_action("[$warn]snapshot: no campaign selected[/]")
            return
        result_line = self._issue_run_query(
            verb="snapshot", method=_SNAPSHOT_METHOD, campaign_id=campaign_id
        )
        self._set_action(result_line)

    def action_new_campaign(self) -> None:
        """Open the campaign compose-form modal; commit stages a new campaign.

        Pushes a :class:`ComposeCampaignModal` and stages the committed draft
        through the real ``research.stage_campaign`` RPC. ``Esc`` cancels the
        modal (the callback receives ``None`` and issues zero RPCs). On commit
        the draft (topic + composed block) routes to :meth:`_stage_committed`,
        which dispatches the staging call off the UI thread and re-renders the
        board to show the new campaign node -- or surfaces the daemon's
        rejection (e.g. an empty topic) honestly without fabricating a node.
        """
        self.app.push_screen(ComposeCampaignModal(), self._stage_committed)

    def _stage_committed(self, draft: CampaignDraft | None) -> None:
        """Stage *draft* off the UI thread, or no-op when the modal cancelled.

        The modal dismiss callback: a ``None`` draft (``Esc`` cancel) issues no
        RPC at all. A committed draft seeds the in-flight :data:`NEW_PENDING`
        line and dispatches :meth:`_stage_campaign_worker` as a worker so the
        daemon round-trip never blocks the UI thread; the worker flips the line
        to the honest outcome and re-renders the board once the call returns.

        Args:
            draft: The composed campaign draft, or ``None`` when the operator
                cancelled the modal.
        """
        if draft is None:
            return
        self._set_action(f"[$muted]{NEW_PENDING}[/]")
        self.run_worker(self._stage_campaign_worker(draft), group="research-stage", exclusive=True)

    async def _stage_campaign_worker(self, draft: CampaignDraft) -> None:
        """Issue ``research.stage_campaign`` for *draft* off the event loop.

        Runs the blocking daemon round-trip in a thread (so the UI thread never
        stalls), then -- back on the event loop -- flips the action line to the
        honest outcome and re-renders the board so a staged campaign's node
        appears. A daemon rejection (an empty topic, an over-bound block) or an
        unreachable daemon surfaces honestly; neither fabricates a campaign
        node.

        Args:
            draft: The composed campaign draft to stage.
        """
        result_line = await asyncio.to_thread(self._issue_stage, draft)
        self._set_action(result_line)
        self._rebuild()
        logger.info(f"_stage_campaign_worker topic={draft.topic!r} result={result_line!r}")

    def _issue_stage(self, draft: CampaignDraft) -> str:
        """Issue ``research.stage_campaign`` for *draft* and return a result line.

        Calls the daemon ``research.stage_campaign`` method through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of
        the TUI mutates through, when a daemon socket is available. A daemon that
        is unreachable, rejecting (an empty topic surfaces as a typed rejection),
        or timing out yields the honest unavailable / rejected line rather than a
        faked staging.

        Args:
            draft: The composed campaign draft (topic + ``research:`` block).

        Returns:
            A content-markup result line describing the staging outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{NEW_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(
                    _NEW_CAMPAIGN_METHOD,
                    {"topic": draft.topic, "config": draft.block.model_dump(mode="json")},
                )
        except DaemonRpcError as exc:
            logger.debug(f"_issue_stage daemon_rejected message={exc.message!r}")
            return f"[$warn]new: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_stage daemon_fallback cause={exc!r}")
            return f"[$warn]{NEW_NO_DAEMON}[/]"
        topic = result.get("topic", draft.topic)
        return f"[$ok]new: staged[/] [$muted]topic={escape_markup(str(topic))}[/]"

    def action_add_question(self) -> None:
        """Open the add-question modal; commit appends an open question.

        The ``o`` operator-channel key pushes a new
        :class:`~eawf.kernel.state.models.OpenQuestion` into the running
        campaign. Pushes a one-field :class:`OperatorNoteModal` collecting the
        question title; ``Esc`` cancels (the callback receives ``None`` and
        issues zero RPCs). A committed title routes off the UI thread to the
        ``research.add_question`` RPC -- which does not exist yet, so the daemon
        answers method-not-found and the channel surfaces the honest "not yet
        wired" line rather than fabricating a question row (the idle-contract
        pattern; it goes live for free once the RPC lands).
        """
        modal = OperatorNoteModal(
            title="add an open question",
            label="question (imperative noun-phrase)",
            placeholder="which curve model fits the short tenor",
            noun="question",
        )
        self.app.push_screen(modal, self._add_question_committed)

    def _add_question_committed(self, note: str | None) -> None:
        """Route a committed add-question *note*, or no-op when cancelled.

        Args:
            note: The committed question title, or ``None`` when the operator
                cancelled the modal (the board issues no RPC).
        """
        self._dispatch_channel(
            note=note,
            verb="ask",
            method=_ADD_QUESTION_METHOD,
            params_key="title",
        )

    def action_steer(self) -> None:
        """Open the steer modal; commit pushes an operator steer note.

        The ``t`` operator-channel key pushes a steer note into the running
        campaign (the balanced-autonomy operator-input channel). Pushes a one-
        field :class:`OperatorNoteModal` collecting the steer text; ``Esc``
        cancels (the callback receives ``None`` and issues zero RPCs). A
        committed note routes off the UI thread to the live ``research.steer``
        RPC (P30-I18-W04) against the selected campaign, surfacing the daemon's
        honest sent / rejected result rather than implying a steer landed.
        """
        modal = OperatorNoteModal(
            title="steer the campaign",
            label="steer note",
            placeholder="prioritise the venues-and-flow domain next round",
            noun="steer note",
        )
        self.app.push_screen(modal, self._steer_committed)

    def _steer_committed(self, note: str | None) -> None:
        """Route a committed steer *note*, or no-op when cancelled.

        Args:
            note: The committed steer text, or ``None`` when the operator
                cancelled the modal (the board issues no RPC).
        """
        self._dispatch_channel(
            note=note,
            verb="steer",
            method=_STEER_METHOD,
            params_key="text",
            campaign_id=self._channel_campaign_id(),
        )

    def action_broadcast(self) -> None:
        """Open the broadcast modal; commit fans a notice to every running round.

        The ``b`` operator-channel key broadcasts an operator notice across the
        campaign's running rounds (the balanced-autonomy notice-broadcast channel
        the campaign cockpit shares with the wave cockpit). Pushes a one-field
        :class:`OperatorNoteModal` collecting the notice; ``Esc`` cancels (the
        callback receives ``None`` and issues zero RPCs). A committed notice
        routes off the UI thread to the live ``research.broadcast`` RPC
        (P30-I18-W04) against the selected campaign, surfacing the daemon's
        honest sent / rejected result rather than implying a broadcast landed.
        """
        modal = OperatorNoteModal(
            title="broadcast a notice",
            label="broadcast notice",
            placeholder="hold synthesis until the new dataset lands",
            noun="broadcast notice",
        )
        self.app.push_screen(modal, self._broadcast_committed)

    def _broadcast_committed(self, note: str | None) -> None:
        """Route a committed broadcast *note*, or no-op when cancelled.

        Args:
            note: The committed broadcast notice, or ``None`` when the operator
                cancelled the modal (the board issues no RPC).
        """
        self._dispatch_channel(
            note=note,
            verb="broadcast",
            method=_BROADCAST_METHOD,
            params_key="notice",
            campaign_id=self._channel_campaign_id(),
        )

    def action_override(self) -> None:
        """Open the override modal; commit forces an operator verdict on the fork.

        The ``v`` operator-channel key overrides the campaign's blocking fork
        with an operator verdict (the balanced-autonomy override channel; an
        override persists locked until the operator clears it). Pushes a one-
        field :class:`OperatorNoteModal` collecting the verdict; ``Esc`` cancels
        (the callback receives ``None`` and issues zero RPCs). A committed
        verdict routes off the UI thread to the live ``research.override`` RPC
        (P30-I18-W04) against the selected campaign, surfacing the daemon's
        honest sent / rejected result rather than implying an override landed.
        """
        modal = OperatorNoteModal(
            title="override the fork",
            label="override verdict",
            placeholder="accept the stronger claim and resume the round",
            noun="override verdict",
        )
        self.app.push_screen(modal, self._override_committed)

    def _override_committed(self, note: str | None) -> None:
        """Route a committed override *note*, or no-op when cancelled.

        Args:
            note: The committed override verdict, or ``None`` when the operator
                cancelled the modal (the board issues no RPC).
        """
        self._dispatch_channel(
            note=note,
            verb="override",
            method=_OVERRIDE_METHOD,
            params_key="verdict",
            campaign_id=self._channel_campaign_id(),
        )

    def _dispatch_channel(
        self,
        *,
        note: str | None,
        verb: str,
        method: str,
        params_key: str,
        campaign_id: str | None = None,
    ) -> None:
        """Dispatch an operator-channel *note* off the UI thread, honestly.

        The shared dispatch for the ``o`` (add-question), ``t`` (steer), ``b``
        (broadcast), and ``v`` (override) channels: a ``None`` *note* (``Esc``
        cancel) issues no RPC at all. A committed note seeds the in-flight
        pending line and dispatches the channel worker so the daemon round-trip
        never blocks the UI thread; the worker flips the line to the honest
        outcome once the call returns. The steer / broadcast / override channels
        thread the selected campaign id so the daemon lands the input against the
        running campaign.

        Args:
            note: The committed channel note, or ``None`` when cancelled.
            verb: The channel verb (``"ask"`` / ``"steer"`` / ...) for the
                result line + logs.
            method: The daemon JSON-RPC method name.
            params_key: The params key the note rides under in the RPC call.
            campaign_id: The campaign the input targets (steer / broadcast /
                override); ``None`` for the scope-wide add-question channel.
        """
        if note is None:
            return
        self._set_action(f"[$muted]{_CHANNEL_PENDING_TEMPLATE.format(verb=verb)}[/]")
        self.run_worker(
            self._channel_worker(
                note=note,
                verb=verb,
                method=method,
                params_key=params_key,
                campaign_id=campaign_id,
            ),
            group="research-channel",
            exclusive=True,
        )

    async def _channel_worker(
        self,
        *,
        note: str,
        verb: str,
        method: str,
        params_key: str,
        campaign_id: str | None = None,
    ) -> None:
        """Issue the operator-channel RPC for *note* off the event loop.

        Runs the blocking daemon round-trip in a thread (so the UI thread never
        stalls), then -- back on the event loop -- flips the action line to the
        honest outcome. A typed rejection (a missing / inactive campaign) or an
        unreachable daemon each surfaces honestly; none fabricates a sent note.

        Args:
            note: The committed channel note to send.
            verb: The channel verb for the result line + logs.
            method: The daemon JSON-RPC method name.
            params_key: The params key the note rides under in the RPC call.
            campaign_id: The target campaign id, or ``None`` for the scope-wide
                add-question channel.
        """
        result_line = await asyncio.to_thread(
            self._issue_channel,
            note=note,
            verb=verb,
            method=method,
            params_key=params_key,
            campaign_id=campaign_id,
        )
        self._set_action(result_line)
        logger.info(f"_channel_worker verb={verb} method={method!r} result={result_line!r}")

    def _issue_channel(
        self,
        *,
        note: str,
        verb: str,
        method: str,
        params_key: str,
        campaign_id: str | None = None,
    ) -> str:
        """Issue the operator-channel RPC for *note* and return a result line.

        Calls *method* through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of
        the TUI mutates through, when a daemon socket is available. The channel
        RPCs are live, so a successful call reports the input sent; a typed
        rejection (a missing / inactive campaign, an over-cap title) surfaces the
        daemon's message and an unreachable daemon surfaces the honest
        unavailable line. None fabricates a sent note.

        Args:
            note: The committed channel note.
            verb: The channel verb for the result line.
            method: The daemon JSON-RPC method name.
            params_key: The params key the note rides under in the RPC call.
            campaign_id: The target campaign id threaded into the params for the
                steer / broadcast / override channels; omitted for add-question.

        Returns:
            A content-markup result line describing the channel outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{_CHANNEL_NO_DAEMON_TEMPLATE.format(verb=verb)}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        call_params: dict[str, object] = {params_key: note}
        if campaign_id is not None:
            call_params["campaign_id"] = campaign_id
        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                client.call(method, call_params)
        except DaemonRpcError as exc:
            # A live channel RPC rejects a missing / inactive campaign with a
            # typed message; surface it honestly rather than implying a send.
            logger.debug(f"_issue_channel daemon_rejected verb={verb} code={exc.code}")
            return (
                f"[$warn]{verb}: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_channel daemon_fallback verb={verb} cause={exc!r}")
            return f"[$warn]{_CHANNEL_NO_DAEMON_TEMPLATE.format(verb=verb)}[/]"
        return f"[$ok]{verb}: sent[/]"

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

    def action_run_campaign(self) -> None:
        """Start a live run of the selected campaign via the ``research.run`` RPC.

        Resolves the selected tree node to its campaign id and routes the start
        through the daemon-client seam (the daemon is the only surface that can
        spawn the researcher sessions a live run needs), surfacing the honest
        typed run handle. With no campaign node selected there is nothing to
        run; when the daemon is unreachable -- or the campaign is not staged /
        already running -- the result says so rather than implying a launch.
        """
        campaign_id = self._selected_campaign_id()
        if campaign_id is None:
            self._set_action(f"[$warn]{RUN_NO_CAMPAIGN}[/]")
            return
        result_line = self._issue_run(campaign_id)
        self._set_action(result_line)
        logger.info(f"action_run_campaign campaign={campaign_id!r} result={result_line!r}")

    def _issue_run(self, campaign_id: str) -> str:
        """Issue ``research.run`` for *campaign_id* and return a result line.

        Calls the daemon ``research.run`` method through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of
        the TUI mutates through, when a daemon socket is available. The RPC
        backgrounds the run and returns a handle immediately; a daemon that is
        unreachable, rejecting (an unknown / non-ACTIVE campaign, or one already
        in flight), or timing out yields the honest unavailable / rejected line
        rather than a faked launch.

        Args:
            campaign_id: The id of the staged ACTIVE campaign to run.

        Returns:
            A content-markup result line describing the run outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{RUN_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                handle = client.call(_RUN_METHOD, {"campaign_id": campaign_id})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_run daemon_rejected message={exc.message!r}")
            return f"[$warn]run: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_run daemon_fallback cause={exc!r}")
            return f"[$warn]{RUN_NO_DAEMON}[/]"
        run_state = handle.get("run_state", "") if isinstance(handle, dict) else ""
        return (
            f"[$ok]run: launched[/] [$muted]campaign={escape_markup(campaign_id)} "
            f"state={escape_markup(str(run_state))}[/]"
        )

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

        Calls the daemon ``research.cancel_campaign`` method through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of
        the TUI mutates through, when a daemon socket is available. A daemon that
        is unreachable, rejecting, or timing out yields the honest unavailable /
        rejected line rather than a faked cancellation.

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

    def _issue_run_query(self, *, verb: str, method: str, campaign_id: str) -> str:
        """Issue a live run-query RPC for *campaign_id* and return a result line.

        The follow-up / snapshot keys route here: they call the live
        ``research.followup`` / ``research.snapshot`` RPC through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of
        the TUI reads through. A successful call surfaces the honest run figure
        (next round / rounds run); a typed rejection (an unknown campaign) or an
        unreachable daemon surfaces honestly. None fabricates a result.

        Args:
            verb: The action verb (``"follow-up"`` / ``"snapshot"``) for the
                result line + logs.
            method: The live daemon JSON-RPC method name.
            campaign_id: The campaign the query targets.

        Returns:
            A content-markup result line describing the query outcome.
        """
        unavailable = _UNAVAILABLE_TEMPLATE.format(verb=verb)
        if not self._daemon_available():
            return f"[$warn]{unavailable}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(method, {"campaign_id": campaign_id})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_run_query daemon_rejected verb={verb} code={exc.code}")
            return (
                f"[$warn]{verb}: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_run_query daemon_fallback verb={verb} cause={exc!r}")
            return f"[$warn]{unavailable}[/]"
        if verb == "follow-up":
            return f"[$ok]follow-up: next round {result.get('next_round', '?')}[/]"
        return f"[$ok]snapshot: {result.get('rounds_run', 0)} round(s) run[/]"

    def _rebuild(self) -> None:
        """Recompute every pane from state + the campaign / pause stores.

        Recomputes the rows + tree nodes, clamps the cursor, and either swaps
        the body between the three-pane scaffold and the honest-empty notice
        (when the empty verdict flips) or updates each pane in place.
        """
        campaigns, claims, questions = self._current_rows()
        was_empty = self.empty
        self.empty = not has_research_signal(campaigns, claims, questions)
        self._tree = build_tree_nodes(
            campaigns, questions, claims=claims, rounds=self._current_round_rows()
        )
        self._clamp_selection()
        if self.empty != was_empty:
            self._recompose_body()
            return
        if self.empty:
            # When the ASCII-art Seal leads the hero (the unicode path composed
            # the #research-empty-hero wrapper), the steady empty-tick update
            # MUST preserve with_sigil=False so the glyph is not re-added beside
            # the art as a second brand mark -- the I15-W20 regression where a
            # tick re-rendered the sigil-bearing body over the seal hero.
            seal_mounted = bool(self.query("#research-empty-hero"))
            self._update_one(EMPTY_ID, self._empty_body(with_sigil=not seal_mounted))
            logger.info(f"research_rebuild empty=True seal_mounted={seal_mounted}")
            return
        pause = self._current_checkpoint()
        mode = self._render_mode()
        self._update_one("research-tree-body", render_tree(self._tree, self.selected, mode=mode))
        self._update_one("research-center-body", render_claims(claims, mode=mode))
        self._update_one(OPTIONS_BODY_ID, render_options(claims, mode=mode))
        self._update_one(CONFLICTS_BODY_ID, render_conflicts(claims, mode=mode))
        self._update_one(UNRESOLVED_BODY_ID, render_unresolved(questions, mode=mode))
        self._update_one(
            "research-progress-body",
            render_progress(
                campaigns,
                claims,
                questions,
                checkpoints=1 if pause else 0,
                rounds=self._current_round_rows(),
            ),
        )
        self._update_one(DRAWER_ID, render_checkpoint(pause))
        logger.info(
            f"research_rebuild campaigns={len(campaigns)} claims={len(claims)} "
            f"questions={len(questions)} nodes={len(self._tree)} empty={self.empty}"
        )

    def _recompose_body(self) -> None:
        """Schedule a full recompose when the empty verdict flips.

        The three-pane scaffold and the honest-empty notice are different
        widget trees, so an empty<->populated flip rebuilds the screen through
        the compose machinery rather than mounting the ``compose_body``
        generator by hand: that generator opens ``with Vertical(...)`` context
        managers, which require an active composition stack, so iterating it
        outside a real ``compose()`` call indexes an empty ``_compose_stacks``
        and raises ``IndexError``. Deferred through ``call_after_refresh`` so
        the async :meth:`~textual.widget.Widget.recompose` is awaited on the
        event loop rather than left dangling.
        """
        self.call_after_refresh(self._recompose_screen)

    async def _recompose_screen(self) -> None:
        """Recompose the flipped body on the event loop, then restore footer hints.

        ``recompose`` re-runs the screen's ``compose`` (header -> body ->
        footer) through the proper composition context, so the flipped
        ``compose_body`` tree mounts cleanly. It re-creates the Footer child,
        which drops the hints :meth:`ScopeScreen.on_mount` set on the prior
        instance, so the canonical strip is re-applied here.
        """
        from eawf.surfaces.tui.widgets.footer import Footer as _Footer

        await self.recompose()
        self.query_one(_Footer).set_hints(self.FOOTER_HINTS)

    def _update_one(self, widget_id: str, body: str) -> None:
        """Update the Static identified by *widget_id*, if mounted."""
        found = self.query(f"#{widget_id}")
        if found:
            found.first(Static).update(body)

    def _repaint_tree(self) -> None:
        """Repaint the tree body so the selection highlight tracks the cursor."""
        self._update_one(
            "research-tree-body", render_tree(self._tree, self.selected, mode=self._render_mode())
        )

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

        Every campaign-owned node -- the campaign root, its round node, and its
        staged-topic nodes -- carries its own ``campaign_id``, so a selection
        inside a campaign sub-tree resolves directly. The scope-wide question
        section (the ``questions`` round node, its question leaves, and their
        nested claim leaves) carries no ``campaign_id``, so a selection there --
        or an empty tree -- yields ``None`` (there is no campaign to cancel).

        Returns:
            The campaign id of the selected node's campaign, or ``None``.
        """
        node = self._selected_node()
        if node is None:
            return None
        return node.campaign_id

    def _channel_campaign_id(self) -> str | None:
        """Resolve the campaign an operator-channel input (steer / b / v) targets.

        The selected node's campaign id when a campaign sub-tree node is
        selected, else the first staged campaign on the board (the operator
        channels address the running campaign, and a single-campaign board needs
        no explicit selection). ``None`` when the scope has staged no campaign --
        the daemon then rejects the input honestly (the channel requires a
        running campaign).

        Returns:
            The target campaign id, or ``None`` when no campaign is staged.
        """
        selected = self._selected_campaign_id()
        if selected is not None:
            return selected
        campaigns, _claims, _questions = self._current_rows()
        return campaigns[0].campaign_id if campaigns else None

    def _set_peek(self, line: str) -> None:
        """Update the peek-result line under the tree, if mounted."""
        self._update_one(PEEK_RESULT_ID, line)

    def _set_action(self, line: str) -> None:
        """Update the action-result line under the drawer, if mounted."""
        self._update_one(ACTION_RESULT_ID, line)

    def _peek_idle(self) -> str:
        """Return the idle peek line (before any node is peeked)."""
        return "[$muted]enter to peek the selected node[/]"

    def _empty_body(self, *, with_sigil: bool = True) -> str:
        """Return the honest-empty hero body: brand sigil + copy + action chips.

        Renders the cosmic-terminal reskin's centered empty-state hero via
        :func:`~eawf.surfaces.tui.widgets.empty_state.render_empty_state`: a
        muted brand sigil over the :data:`EMPTY_NOTICE` headline +
        :data:`EMPTY_SUBLINE` framing copy, then the ``[ n new campaign ]`` /
        ``[ d brief ]`` action chips (both ``n`` and ``d`` are live bindings).
        The pane centers this hero via the ``#research-empty`` CSS.

        Args:
            with_sigil: When ``False`` the leading brand glyph is dropped -- the
                unicode path renders the ASCII-art Seal as the centerpiece
                instead, so the glyph would be a redundant second brand mark.
        """
        return render_empty_state(
            EMPTY_NOTICE,
            EMPTY_SUBLINE,
            mode=self._render_mode(),
            chips=(("n", "new campaign"), ("d", "brief")),
            sigil=with_sigil,
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

    def _current_round_rows(self) -> tuple[Round, ...]:
        """Return the executed research rounds for the active scope.

        Reads the append-only ``research_round`` store under the resolved
        ``state.json`` (:func:`read_round_rows`). A read failure (a malformed
        row) degrades to empty rather than crashing the mode, so a bad store row
        never takes the board down. Empty for a staged-but-not-run scope.

        Returns:
            The executed rounds in round order; empty when no run has persisted
            one yet.
        """
        try:
            return read_round_rows(self._resolved_state_path())
        except Exception as exc:
            logger.debug(f"_current_round_rows read_failed cause={exc!r}")
            return ()

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

    def _render_mode(self) -> RenderMode:
        """Return the App's active render mode, defaulting when unavailable.

        A bare harness without the reactive degrades to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` so the
        reskin's sigil resolution never raises.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

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
    "CONFLICTS_BODY_ID",
    "DRAWER_ID",
    "EMPTY_NOTICE",
    "EMPTY_SUBLINE",
    "MODE_KEYS_ID",
    "MODE_KEYS_LINE",
    "NEW_NO_DAEMON",
    "NEW_PENDING",
    "NONE_YET",
    "OPTIONS_BODY_ID",
    "PARK_NO_CHECKPOINT",
    "PARK_NO_DAEMON",
    "RESEARCH_REFRESH_S",
    "RUN_NO_CAMPAIGN",
    "RUN_NO_DAEMON",
    "STAGED_ROUND",
    "UNRESOLVED_BODY_ID",
    "UNRESOLVED_HEADER_ID",
    "CampaignDraft",
    "CampaignRow",
    "ComposeCampaignModal",
    "NodeKind",
    "OperatorNoteModal",
    "ResearchBoardModeScreen",
    "RoundProgress",
    "RoundState",
    "TreeNode",
    "build_research_block",
    "build_tree_nodes",
    "claim_sigil_markup",
    "classify_round_state",
    "classify_round_state_from_records",
    "compute_round_progress",
    "group_claims_by_evidence",
    "has_research_signal",
    "index_claims_by_question",
    "parse_domains",
    "project_campaign_progress",
    "question_sigil_markup",
    "read_campaign_rows",
    "read_round_rows",
    "render_center_tabs",
    "render_checkpoint",
    "render_claims",
    "render_conflicts",
    "render_options",
    "render_progress",
    "render_tree",
    "render_unresolved",
    "round_sigil_markup",
]
