"""``ArmModal`` -- the FA1 fleet-arm / launch-flow overlay (autopilot ``a``).

The cockpit's "speak the whole frontier into being" surface: a config form the
operator fills before arming the daemon-owned fleet auto-drain loop
(:func:`eawf.runtime.daemon.methods.fleet.drive`). It replaces the old
``arm: deferred`` stub on the
:class:`~eawf.surfaces.tui.modes.autopilot.AutopilotModeScreen` with a typed
launch path: pick the drain scope, the budget caps, the lane concurrency, the
risk policy, and the convergence criterion, then ``Enter`` submits a typed
:class:`ArmSpec` the autopilot pane folds into a ``fleet.drive`` RPC and flips
the cockpit to ``DRAINING``.

Five config groups (the FA1 launch form)
----------------------------------------
The form is a vertical stack of five cycle-on-change config groups, one per
launch dimension, each a closed enum the operator forward-cycles through:

* **scope** -- how wide the drain reaches: this iter / this phase / cross-repo.
* **budget** -- the EU / $ / per-wave caps tier the run runs under.
* **concurrency** -- the lane width (how many waves drain at once).
* **risk policy** -- the auto-close vs fork-tier disposition plus the hard-halt
  toggle (a fork that fails stops the whole fleet).
* **convergence** -- the stop criterion: drain the frontier to empty, or stop
  after K consecutive clean rounds (``kclean``).

The selected option in each group is the typed value the :class:`ArmSpec`
carries; the spec is the overlay's dismiss payload (or ``None`` when the
operator ``Esc``-cancels), so the overlay holds NO arming logic -- it returns a
typed config and the autopilot pane issues the daemon RPC.

Honest-empty over a dry frontier (the load-bearing honesty)
-----------------------------------------------------------
Arming over an empty ready frontier is a no-op: the daemon ``fleet.drive`` loop
refuses to arm a ``DRAINING``-with-zero-lanes run that can never make progress,
so the overlay surfaces the honest :data:`NOTHING_TO_DRAIN` banner and ``Enter``
dismisses ``None`` rather than firing a doomed RPC. The frontier emptiness is
passed in by the autopilot pane (it already computes the ready frontier), so the
overlay never re-derives the claimability rule.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

logger = logging.getLogger(__name__)

#: Daemon JSON-RPC method the arm / launch-flow path drives: arm the
#: daemon-owned fleet auto-drain loop over the ready frontier.
_DRIVE_RPC: str = "fleet.drive"

#: Result line after ``a`` arms the fleet drive: the cockpit flips to DRAINING
#: and the daemon-owned loop claims + dispatches the frontier unattended.
ARM_DRAINING: str = "arm: draining"

#: Result line when ``a`` (arm) is cancelled (``Esc`` on the launch form) -- no
#: drive is armed, so the cockpit stays where it was.
ARM_CANCELLED: str = "arm: cancelled"

#: Result line when the armed ``fleet.drive`` request could not reach the daemon.
ARM_NO_DAEMON: str = "arm: daemon unavailable -- request not issued"

#: Render-mode label threaded into the sigil helper when the host App exposes no
#: ``render_mode`` (a bare standalone harness): the unicode column is the
#: default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"

#: Title above the five config groups -- names the launch target.
ARM_TITLE: str = "arm drain over"

#: Honest-empty banner shown when the ready frontier is dry: arming would refuse
#: at the daemon (an empty frontier cannot arm a make-progress run), so the
#: overlay says so byte-for-byte rather than firing a doomed RPC. The dash is a
#: real em-dash so the cockpit reads as one calm sentence.
NOTHING_TO_DRAIN: str = "nothing to drain — all ready waves closed or blocked"

#: The five launch-form group ids (also the row anchors the cursor walks).
SCOPE_GROUP_ID: str = "arm-scope"
BUDGET_GROUP_ID: str = "arm-budget"
CONCURRENCY_GROUP_ID: str = "arm-concurrency"
RISK_GROUP_ID: str = "arm-risk"
CONVERGENCE_GROUP_ID: str = "arm-convergence"

#: Id of the honest-empty banner row (shown only on a dry frontier).
EMPTY_BANNER_ID: str = "arm-empty"

#: Ids of the read-only preview rows under the editable config groups.
CAPS_ROW_ID: str = "arm-caps"
HALT_ROW_ID: str = "arm-halt"
RISK_ROW_1_ID: str = "arm-risk-row-1"
RISK_ROW_2_ID: str = "arm-risk-row-2"

#: Id of the key-hint footer row.
HINT_ID: str = "arm-hint"

#: Drain-scope options (group 1) -- how wide the fleet reaches, in widening
#: order so the cursor cycles narrow -> broad.
SCOPE_OPTIONS: tuple[str, ...] = ("this iter", "this phase", "cross-repo")

#: Budget-cap tier options (group 2) -- the EU / $ / per-wave caps the run runs
#: under. ``unbounded`` runs with no cap; the named tiers tighten from there.
BUDGET_OPTIONS: tuple[str, ...] = ("unbounded", "lenient", "standard", "strict")

#: Lane-concurrency options (group 3) -- how many waves drain at once. Each maps
#: to the integer lane width the ``fleet.drive`` ``concurrency`` param takes.
CONCURRENCY_OPTIONS: tuple[str, ...] = ("1 lane", "2 lanes", "4 lanes", "8 lanes")

#: Risk-policy options (group 4) -- the fork disposition + hard-halt toggle. A
#: hard-halt policy stops the whole fleet on a fork; the softer tier forks the
#: failing wave and keeps draining the rest.
RISK_OPTIONS: tuple[str, ...] = (
    "auto-close, fork on fail",
    "auto-close, hard-halt on fail",
    "fork all, hard-halt on fail",
)

#: Convergence-criterion options (group 5) -- the stop rule. ``drain`` stops
#: only when the frontier empties; ``kclean`` stops after K consecutive clean
#: rounds. Maps to the ``fleet.drive`` ``convergence`` param.
CONVERGENCE_OPTIONS: tuple[str, ...] = ("drain to empty", "K-clean rounds")

#: The lane-width integer each :data:`CONCURRENCY_OPTIONS` entry maps to.
_CONCURRENCY_LANES: dict[str, int] = {
    "1 lane": 1,
    "2 lanes": 2,
    "4 lanes": 4,
    "8 lanes": 8,
}

#: The EU / USD / waves spend-cap triple each :data:`BUDGET_OPTIONS` tier maps to
#: -- the ``fleet.drive`` ``eu_cap`` / ``usd_cap`` / ``waves_cap`` params (DL-4).
#: ``unbounded`` leaves every cap ``None`` (the run never HALTs on spend); the
#: named tiers tighten from lenient down to strict so an operator can bound a run
#: by effort, dollars, AND wave count at once. The figures are deliberately round
#: so the cockpit's ``$ used/cap`` + EU block-bar read cleanly; the daemon loop
#: HALTs at the FIRST cap any axis reaches (the ``budget_exhausted`` DL-4 gate).
_BUDGET_CAPS: dict[str, tuple[float | None, float | None, int | None]] = {
    "unbounded": (None, None, None),
    "lenient": (40.0, 80.0, 32),
    "standard": (16.0, 32.0, 12),
    "strict": (4.0, 8.0, 4),
}

#: The ``kclean`` K threshold an armed run uses when its convergence option is
#: ``K-clean rounds`` -- the consecutive-clean-round count the loop stops after.
#: Pinned to the ``fleet.drive`` ``kclean_k`` default (2) so the arm form and the
#: daemon agree on the convergence ceiling; the ``drain`` option ignores it.
_KCLEAN_K: int = 2


class ArmSpec(BaseModel):
    """Typed launch config the operator arms the fleet drive with.

    The dismiss payload of :class:`ArmModal`: the five selected launch-form
    options plus the derived ``fleet.drive`` params (lane width, convergence
    mode, hard-halt toggle). The autopilot pane folds this into the
    ``fleet.drive`` RPC -- the overlay itself holds no arming logic.

    Attributes:
        scope: The drain scope (one of :data:`SCOPE_OPTIONS`).
        budget: The budget-cap tier (one of :data:`BUDGET_OPTIONS`).
        concurrency: The lane width -- the ``fleet.drive`` ``concurrency``
            param (>= 1), derived from the selected concurrency option.
        risk_policy: The risk-policy label (one of :data:`RISK_OPTIONS`).
        hard_halt: Whether a fork hard-halts the whole fleet (derived from the
            selected risk policy).
        convergence: The ``fleet.drive`` convergence mode -- ``drain`` or
            ``kclean`` -- derived from the selected convergence option.
        eu_cap: The cumulative EU spend cap (DL-4), derived from the budget
            tier; ``None`` under the ``unbounded`` tier so the run never HALTs
            on EU spend.
        usd_cap: The cumulative USD spend cap, derived from the budget tier;
            ``None`` under ``unbounded``.
        waves_cap: The claimed-wave count cap, derived from the budget tier;
            ``None`` under ``unbounded``.
        kclean_k: The consecutive-clean-round threshold for ``kclean``
            convergence; ignored under ``drain``.
    """

    model_config = ConfigDict(extra="forbid")
    scope: str
    budget: str
    concurrency: int = Field(ge=1)
    risk_policy: str
    hard_halt: bool
    convergence: str
    eu_cap: float | None = Field(default=None, gt=0.0)
    usd_cap: float | None = Field(default=None, gt=0.0)
    waves_cap: int | None = Field(default=None, ge=1)
    kclean_k: int = Field(default=_KCLEAN_K, ge=1)


def build_arm_spec(
    *,
    scope: str,
    budget: str,
    concurrency_option: str,
    risk_policy: str,
    convergence_option: str,
) -> ArmSpec:
    """Derive the typed :class:`ArmSpec` from the selected launch-form options.

    Maps the human-readable selections onto the typed ``fleet.drive`` params:
    the concurrency option resolves to its integer lane width, the budget tier
    resolves the DL-4 EU / USD / waves spend caps (``unbounded`` leaves them
    ``None``), the risk policy resolves the hard-halt toggle (a ``hard-halt``
    label sets it), and the convergence option resolves the ``drain`` /
    ``kclean`` mode plus the ``kclean`` K threshold. Every derived field rides
    the spec so NONE is dropped before the RPC -- the W02 fix.

    Args:
        scope: The selected drain-scope option.
        budget: The selected budget-cap tier.
        concurrency_option: The selected concurrency option (e.g. ``2 lanes``).
        risk_policy: The selected risk-policy option.
        convergence_option: The selected convergence option.

    Returns:
        The typed launch spec.
    """
    eu_cap, usd_cap, waves_cap = _BUDGET_CAPS.get(budget, (None, None, None))
    return ArmSpec(
        scope=scope,
        budget=budget,
        concurrency=_CONCURRENCY_LANES.get(concurrency_option, 1),
        risk_policy=risk_policy,
        hard_halt="hard-halt" in risk_policy,
        convergence="kclean" if convergence_option == "K-clean rounds" else "drain",
        eu_cap=eu_cap,
        usd_cap=usd_cap,
        waves_cap=waves_cap,
        kclean_k=_KCLEAN_K,
    )


def scope_frontier(scope: str, frontier: list[str]) -> list[str]:
    """Narrow the ready *frontier* to the waves the drain *scope* reaches.

    The arm form's scope option (one of :data:`SCOPE_OPTIONS`) is no longer
    cosmetic (the W02 fix): it filters the ready frontier before the
    ``fleet.drive`` RPC so a narrower scope claims fewer waves. The scope is
    resolved off the frontier ids themselves (each ``P<NN>-I<NN>-W<NN>`` encodes
    its phase + iter via :func:`~eawf.kernel.state.ids.parents_of`), anchored on
    the HEAD of the frontier (the next wave the loop would claim):

    - ``this iter`` keeps only the waves sharing the head wave's iter prefix --
      the narrowest drain, this iter's ready waves alone.
    - ``this phase`` keeps the waves sharing the head wave's phase prefix --
      every ready wave under the current phase.
    - ``cross-repo`` keeps the whole frontier unchanged -- the broadest drain.

    A frontier whose head is not a parseable wave id (defensive) is returned
    unchanged so the arm never silently drops every wave on a malformed id.
    Claim order is preserved (the filter keeps the input order).

    Args:
        scope: The selected drain-scope option (one of :data:`SCOPE_OPTIONS`).
        frontier: The ready ``W<NN>`` wave ids to drain, in claim order.

    Returns:
        The scope-filtered frontier, in claim order; the whole frontier for
        ``cross-repo`` or an unparseable head.
    """
    if scope == "cross-repo" or not frontier:
        return list(frontier)
    from eawf.kernel.state.ids import is_wave_id, parents_of

    head = frontier[0]
    if not is_wave_id(head):
        return list(frontier)
    phase_id, iter_id = parents_of(head)
    # ``this iter`` matches the head wave's iter; ``this phase`` matches its phase.
    target = iter_id if scope == "this iter" else phase_id
    target_index = 1 if scope == "this iter" else 0

    def _in_scope(wid: str) -> bool:
        return is_wave_id(wid) and parents_of(wid)[target_index] == target

    kept = [wid for wid in frontier if _in_scope(wid)]
    logger.debug(
        f"scope_frontier scope={scope!r} target={target!r} kept={len(kept)}/{len(frontier)}"
    )
    return kept


def issue_drive(spec: ArmSpec, frontier: list[str], *, daemon_available: bool) -> str:
    """Issue the ``fleet.drive`` RPC for *spec* and return a cockpit result line.

    Folds EVERY :class:`ArmSpec` field into the ``fleet.drive`` params (the W02
    fix -- no field is dropped): the scope-filtered *frontier*
    (:func:`scope_frontier`) in claim order, the lane concurrency, the
    convergence mode + its ``kclean_k`` threshold, the DL-4 EU / USD / waves
    spend caps, and the hard-halt toggle. The call routes through the
    :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam when the daemon
    is reachable. The line reports the cockpit flipped to ``DRAINING``, or the
    honest unavailable / rejected line rather than a faked arm. An empty
    (scope-filtered) *frontier* is a no-op cancel (defence in depth -- the
    overlay already refuses a dry frontier, so this only fires if the rows
    emptied between open and arm, or the scope filtered every wave out).

    Args:
        spec: The overlay's typed launch config.
        frontier: The ready ``W<NN>`` wave ids to drain, in claim order.
        daemon_available: Whether the host App reports a reachable daemon socket.

    Returns:
        A content-markup result line describing the arm outcome.
    """
    scoped = scope_frontier(spec.scope, frontier)
    if not scoped:
        return f"[$warn]{ARM_CANCELLED}[/]"
    if not daemon_available:
        return f"[$warn]{ARM_NO_DAEMON}[/]"
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    params: dict[str, object] = {
        "frontier": scoped,
        "concurrency": spec.concurrency,
        "convergence": spec.convergence,
        "kclean_k": spec.kclean_k,
        "hard_halt": spec.hard_halt,
    }
    # Only send the spend caps that are armed -- an unbounded tier leaves them
    # unset so the strict ``DriveParams`` (gt=0.0 / ge=1) never sees a None it
    # would otherwise have to allow as an explicit key.
    if spec.eu_cap is not None:
        params["eu_cap"] = spec.eu_cap
    if spec.usd_cap is not None:
        params["usd_cap"] = spec.usd_cap
    if spec.waves_cap is not None:
        params["waves_cap"] = spec.waves_cap
    try:
        with DaemonClient(call_timeout_seconds=30.0) as client:
            client.call(_DRIVE_RPC, params)
    except DaemonRpcError as exc:
        logger.debug(f"issue_drive daemon_rejected message={exc.message!r}")
        return f"[$warn]arm: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"issue_drive daemon_fallback cause={exc!r}")
        return f"[$warn]{ARM_NO_DAEMON}[/]"
    plural = "" if len(scoped) == 1 else "s"
    return f"[$ok]{ARM_DRAINING}[/] [$muted]{len(scoped)} wave{plural} on the frontier[/]"


#: The five launch-form groups, in cursor order: (group id, caption, options).
_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (SCOPE_GROUP_ID, "scope", SCOPE_OPTIONS),
    (BUDGET_GROUP_ID, "budget", BUDGET_OPTIONS),
    (CONCURRENCY_GROUP_ID, "concurrency", CONCURRENCY_OPTIONS),
    (RISK_GROUP_ID, "risk policy", RISK_OPTIONS),
    (CONVERGENCE_GROUP_ID, "convergence", CONVERGENCE_OPTIONS),
)

#: The key-hint footer vocab, mirroring the other reskin overlays: a calm
#: middle-dot-separated chord list under the form.
_KEY_HINT: str = "[ ↑/↓ field · ←/→ change · Enter arm · Esc cancel ]"

#: Close-only hint for an empty-frontier arm card. The card has no editable
#: fields in this state, so the footer must not advertise arming controls.
_CLOSE_ONLY_HINT: str = "[ Enter / Esc close ]"


def _format_cap(value: float | int | None) -> str:
    """Format one budget cap for the arm preview row."""
    if value is None:
        return "--"
    return f"{value:g}"


def render_caps_row(budget: str) -> str:
    """Render the EU / USD / waves caps for *budget*."""
    eu_cap, usd_cap, waves_cap = _BUDGET_CAPS.get(budget, (None, None, None))
    return (
        f"[$muted]caps[/]  EU [$accent]{_format_cap(eu_cap)}[/]  "
        f"$ [$accent]{_format_cap(usd_cap)}[/]  "
        f"waves [$accent]{_format_cap(waves_cap)}[/]"
    )


def render_halt_row(risk_policy: str) -> str:
    """Render the budget-stop behaviour row for *risk_policy*."""
    halt = "hard-halt in-flight lanes" if "hard-halt" in risk_policy else "drain in-flight lanes"
    return f"[$muted]budget stop[/]  [$accent]{halt}[/]"


def render_risk_matrix_rows(risk_policy: str) -> tuple[str, str]:
    """Render the two-row risk matrix for *risk_policy*."""
    if risk_policy == "fork all, hard-halt on fail":
        clean = "fork every close"
        fail = "hard-halt run"
    elif "hard-halt" in risk_policy:
        clean = "auto-close clean"
        fail = "hard-halt run"
    else:
        clean = "auto-close clean"
        fail = "fork failed lane"
    return (
        f"[$muted]risk matrix[/]  clean [$accent]{clean}[/]",
        f"[$muted]risk matrix[/]  fail  [$accent]{fail}[/]",
    )


def render_group_row(caption: str, option: str, *, focused: bool) -> str:
    """Render one launch-form group row: caption + selected option + caret.

    The focused row leads with a ``>`` caret and paints the option in the accent
    colour so the cursor position reads at a glance; an unfocused row is muted.

    Args:
        caption: The group caption (e.g. ``scope``).
        option: The group's currently-selected option label.
        focused: Whether this row is under the cursor.

    Returns:
        A content-markup row string.
    """
    caret = ">" if focused else " "
    colour = "$accent" if focused else "$muted"
    return f"[{colour}]{caret} {caption}[/]  [{colour}]{option}[/]"


class ArmModal(ModalScreen["ArmSpec | None"]):
    """The FA1 fleet-arm launch form (returns an :class:`ArmSpec` on dismiss).

    A vertical stack of five cycle-on-change config groups (scope, budget,
    concurrency, risk policy, convergence). ``↑`` / ``↓`` move the field cursor,
    ``←`` / ``→`` cycle the focused field's option, ``Enter`` submits the typed
    :class:`ArmSpec`, and ``Esc`` cancels (dismisses ``None``). When the ready
    frontier is dry the form refuses to arm: it shows the honest
    :data:`NOTHING_TO_DRAIN` banner and ``Enter`` dismisses ``None`` rather than
    returning a spec, so the autopilot pane never fires a doomed ``fleet.drive``.

    The overlay holds NO arming logic -- it returns a typed config (or ``None``);
    issuing the ``fleet.drive`` RPC + flipping the cockpit to ``DRAINING`` is the
    autopilot pane's job.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ArmModal {
        align: center middle;
    }
    ArmModal > #arm-box {
        width: 90%;
        min-width: 36;
        max-width: 90;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ArmModal .arm-title {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }
    ArmModal .arm-group {
        height: auto;
    }
    ArmModal .arm-preview {
        height: auto;
    }
    ArmModal #arm-empty {
        height: auto;
        color: $warning;
        margin-bottom: 1;
    }
    ArmModal #arm-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``↑`` / ``↓`` move the field cursor; ``←`` / ``→`` cycle the focused
    #: field's option; ``Enter`` arms, ``Esc`` cancels. Vim ``j`` / ``k`` ride
    #: the vertical arrows, ``h`` / ``l`` the horizontal, per the operator keymap.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor(-1)", "up", show=False),
        Binding("down", "cursor(1)", "down", show=False),
        Binding("k", "cursor(-1)", "up", show=False),
        Binding("j", "cursor(1)", "down", show=False),
        Binding("left", "cycle(-1)", "prev", show=False),
        Binding("right", "cycle(1)", "next", show=False),
        Binding("h", "cycle(-1)", "prev", show=False),
        Binding("l", "cycle(1)", "next", show=False),
        Binding("enter", "arm", "arm", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    #: Index of the focused launch-form group (``0`` = scope). ``↑`` / ``↓``
    #: clamp it to the first / last group (no wrap).
    field_index: reactive[int] = reactive(0)

    def __init__(self, *, frontier_empty: bool, frontier_count: int | None = None) -> None:
        """Construct the launch form, seeding each group to its first option.

        Args:
            frontier_empty: Whether the ready frontier is dry. ``True`` shows
                the honest-empty banner and makes ``Enter`` a no-op cancel
                (arming would refuse at the daemon).
            frontier_count: Count of ready frontier waves the overlay will
                drain if armed. Defaults to ``0`` for an empty frontier and
                ``1`` for older populated call sites.
        """
        super().__init__()
        self._frontier_empty = frontier_empty
        self._frontier_count = (
            0 if frontier_empty else (frontier_count if frontier_count is not None else 1)
        )
        #: The selected option index per group, keyed by group id; seeded to the
        #: first option of each group.
        self._selected: dict[str, int] = {group_id: 0 for group_id, _, _ in _GROUPS}

    def compose(self) -> ComposeResult:
        """Yield the title, the (honest-empty banner or) five group rows, and the hint."""
        with Vertical(id="arm-box"):
            yield Static(self._title_line(), classes="arm-title")
            if self._frontier_empty:
                yield Static(f"[$warn]{NOTHING_TO_DRAIN}[/]", id=EMPTY_BANNER_ID)
                yield Static(_CLOSE_ONLY_HINT, id=HINT_ID)
                return
            for index, (group_id, caption, options) in enumerate(_GROUPS):
                yield Static(
                    render_group_row(caption, options[0], focused=index == 0),
                    classes="arm-group",
                    id=group_id,
                )
            yield Static(render_caps_row(BUDGET_OPTIONS[0]), classes="arm-preview", id=CAPS_ROW_ID)
            yield Static(render_halt_row(RISK_OPTIONS[0]), classes="arm-preview", id=HALT_ROW_ID)
            risk_row_1, risk_row_2 = render_risk_matrix_rows(RISK_OPTIONS[0])
            yield Static(risk_row_1, classes="arm-preview", id=RISK_ROW_1_ID)
            yield Static(risk_row_2, classes="arm-preview", id=RISK_ROW_2_ID)
            yield Static(_KEY_HINT, id=HINT_ID)

    def on_mount(self) -> None:
        """Paint the initial focus, then watch for a render-mode flip."""
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._repaint_groups()

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the title sigil when the App's render mode flips."""
        self.query_one(".arm-title", Static).update(self._title_line())

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def _title_line(self) -> str:
        """Render the form title led by the dispatch chrome sigil."""
        sigil = chrome("dispatch", mode=self._render_mode())
        plural = "" if self._frontier_count == 1 else "s"
        return f"[$accent]{sigil} {ARM_TITLE} {self._frontier_count} wave{plural}[/]"

    def watch_field_index(self) -> None:
        """Repaint the group rows when the field cursor moves."""
        if self.is_mounted:
            self._repaint_groups()

    def _repaint_groups(self) -> None:
        """Repaint every group row with its selected option + focus caret."""
        if self._frontier_empty:
            return
        for index, (group_id, caption, options) in enumerate(_GROUPS):
            option = options[self._selected[group_id]]
            row = self.query_one(f"#{group_id}", Static)
            row.update(render_group_row(caption, option, focused=index == self.field_index))
        budget = self._selected_option(BUDGET_GROUP_ID)
        risk_policy = self._selected_option(RISK_GROUP_ID)
        self.query_one(f"#{CAPS_ROW_ID}", Static).update(render_caps_row(budget))
        self.query_one(f"#{HALT_ROW_ID}", Static).update(render_halt_row(risk_policy))
        risk_row_1, risk_row_2 = render_risk_matrix_rows(risk_policy)
        self.query_one(f"#{RISK_ROW_1_ID}", Static).update(risk_row_1)
        self.query_one(f"#{RISK_ROW_2_ID}", Static).update(risk_row_2)

    def action_cursor(self, delta: int) -> None:
        """Move the field cursor by *delta*, clamped to the group range.

        Args:
            delta: ``-1`` (up) or ``1`` (down).
        """
        self.field_index = max(0, min(len(_GROUPS) - 1, self.field_index + delta))

    def action_cycle(self, delta: int) -> None:
        """Cycle the focused group's selected option by *delta* (wrapping).

        Args:
            delta: ``-1`` (previous) or ``1`` (next).
        """
        group_id, _caption, options = _GROUPS[self.field_index]
        self._selected[group_id] = (self._selected[group_id] + delta) % len(options)
        self._repaint_groups()

    def _selected_option(self, group_id: str) -> str:
        """Return the currently-selected option label for *group_id*."""
        _gid, _caption, options = next(group for group in _GROUPS if group[0] == group_id)
        return options[self._selected[group_id]]

    def action_arm(self) -> None:
        """Submit the typed :class:`ArmSpec` -- or no-op cancel on a dry frontier.

        On a non-empty frontier the selected options derive an :class:`ArmSpec`
        (the dismiss payload the autopilot pane folds into ``fleet.drive``). On a
        dry frontier arming is a no-op: arming an empty frontier would refuse at
        the daemon, so the form dismisses ``None`` rather than returning a spec.
        """
        if self._frontier_empty:
            logger.info("arm_modal nothing_to_drain frontier_empty=True")
            self.dismiss(None)
            return
        spec = build_arm_spec(
            scope=self._selected_option(SCOPE_GROUP_ID),
            budget=self._selected_option(BUDGET_GROUP_ID),
            concurrency_option=self._selected_option(CONCURRENCY_GROUP_ID),
            risk_policy=self._selected_option(RISK_GROUP_ID),
            convergence_option=self._selected_option(CONVERGENCE_GROUP_ID),
        )
        logger.info(
            f"arm_modal armed scope={spec.scope!r} concurrency={spec.concurrency} "
            f"convergence={spec.convergence!r} hard_halt={spec.hard_halt}"
        )
        self.dismiss(spec)

    def action_cancel(self) -> None:
        """Dismiss with ``None`` (``Esc`` = cancel, no arm)."""
        logger.debug("arm_modal cancelled")
        self.dismiss(None)


__all__ = [
    "ARM_CANCELLED",
    "ARM_DRAINING",
    "ARM_NO_DAEMON",
    "ARM_TITLE",
    "BUDGET_GROUP_ID",
    "BUDGET_OPTIONS",
    "CAPS_ROW_ID",
    "CONCURRENCY_GROUP_ID",
    "CONCURRENCY_OPTIONS",
    "CONVERGENCE_GROUP_ID",
    "CONVERGENCE_OPTIONS",
    "EMPTY_BANNER_ID",
    "HALT_ROW_ID",
    "HINT_ID",
    "NOTHING_TO_DRAIN",
    "RISK_GROUP_ID",
    "RISK_OPTIONS",
    "RISK_ROW_1_ID",
    "RISK_ROW_2_ID",
    "SCOPE_GROUP_ID",
    "SCOPE_OPTIONS",
    "ArmModal",
    "ArmSpec",
    "build_arm_spec",
    "issue_drive",
    "render_caps_row",
    "render_group_row",
    "render_halt_row",
    "render_risk_matrix_rows",
    "scope_frontier",
]
