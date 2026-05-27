"""``DetailModal`` — tabbed detail card for a selected entity.

The drill-in overlay opened when the operator presses ``Enter`` on a row:
the widgets emit a selection message
(:class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable.RowActivated`
carrying a backlog-item id,
:class:`~eawf.surfaces.tui.widgets.roadmap_tree.RoadmapTree.WaveSelected`
carrying a wave id), the shared
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` routes the message here, and this
modal renders the resolved entity's detail in a tabbed, scrollable card.

The card body is split across up to four tabs — ``h`` history, ``d``
detail, ``m`` metrics, ``e`` events — mirroring the per-overlay tab set
the C06 brief reserved. ``Tab`` / ``Shift+Tab`` cycle the tabs, the
single-letter keys ``h`` / ``d`` / ``m`` / ``e`` jump straight to a tab, and
the arrow keys keep their native scroll behaviour inside the focused pane
(they are *not* rebound to tab-switching). Only tabs that have data for
the resolved entity are built, so an entity with no dispatch history shows
no ``e`` tab rather than an empty one; a hotkey for an absent tab no-ops.

Entity resolution is a pure function (:func:`resolve_detail`) that takes
the reactive :class:`~eawf.kernel.state.models.State` and the selection id and
returns a typed :class:`DetailCard` (title + per-tab section rows). It
resolves waves, iters, phases, and backlog items; an unknown id yields a
total fallback card so the drill-in seam never crashes when the state and
a widget row briefly disagree (e.g. mid daemon-push). Keeping the
formatting pure means the rendered detail is unit-testable without
mounting Textual, and the modal stays a thin view over it. Construct the
modal with a pre-built :class:`DetailCard` (the host screen builds it from
``app.state``) so the overlay never reaches back into App state itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TabbedContent, TabPane

from eawf.surfaces.render.narrative import (
    NarrativeNotFoundError,
    build_narrative,
    render_narrative_bundle,
)
from eawf.surfaces.tui.widgets.eu_bar import (
    render_completion_bar,
    render_eu_bar_plain,
)
from eawf.workflow.agent_report.rollup import PerWaveAttemptRollup, per_wave_attempt_rollup

if TYPE_CHECKING:
    from eawf.kernel.state.models import State, Wave

logger = logging.getLogger(__name__)

#: Tab id → human label, in cycle order. The modal builds a
#: :class:`~textual.widgets.TabPane` per non-empty section keyed by these
#: ids; the labels carry the single-letter mnemonic from the C06 brief.
_TAB_LABELS: dict[str, str] = {
    "h": "h history",
    "d": "d detail",
    "m": "m metrics",
    "e": "e events",
}


@dataclass(frozen=True)
class DetailCard:
    """A resolved detail card: a title plus per-tab section rows.

    The card carries one row group per overlay tab. ``rows`` is the
    canonical ``d`` (detail) field group — always populated; the remaining
    groups are populated only when the entity has data for them, and the
    modal builds a tab only for a non-empty group. ``detail_markdown`` is
    the optional Markdown body for the ``d`` tab; wave cards use it for
    the phase NarrativeBundle preview, while non-wave cards render the
    regular ``rows`` group.

    Attributes:
        title: The card heading (e.g. ``wave P26-I01-W19`` /
            ``iter P26-I01`` / ``phase P26`` / ``backlog B042``).
        rows: The ``d`` detail group — ordered ``(label, value)`` pairs.
        metrics: The ``m`` group — bar rows (completion / size / EU /
            token), ``(label, value)`` pairs.
        history: The ``h`` group — lifecycle rows (status, timestamps).
        events: The ``e`` group — recent activity rows (e.g. a wave's
            dispatch history), ``(label, value)`` pairs.
        detail_markdown: Optional Markdown body for the ``d`` tab. When
            ``None``, the ``d`` tab renders ``rows`` as aligned field rows.
    """

    title: str
    rows: tuple[tuple[str, str], ...]
    metrics: tuple[tuple[str, str], ...] = ()
    history: tuple[tuple[str, str], ...] = ()
    events: tuple[tuple[str, str], ...] = ()
    detail_markdown: str | None = None


def _fmt_dt(value: object) -> str:
    """Format a datetime-ish *value* as an ISO string, or ``"—"`` when unset.

    Args:
        value: A ``datetime`` (or ``None``) read off a state model.

    Returns:
        The ISO-8601 string, or an em dash when *value* is ``None``.
    """
    if value is None:
        return "—"
    return str(value)


def render_file_tree(paths: list[str]) -> str:
    """Render *paths* (file globs / paths) as an indented directory tree.

    Builds a trie over the ``/``-split path segments and renders it with a
    two-space indent per level, collapsing single-child directory chains
    into one segment (``src/eawf/`` rather than ``src`` → ``eawf``) so glob
    scopes like ``src/eawf/dispatch/**`` stay compact while shared prefixes
    group under one parent. Pure (no I/O) so the resolver stays unit-testable
    without mounting Textual.

    Args:
        paths: The wave's ``file_scopes`` (path globs / files).

    Returns:
        The tree as a newline-joined block (no trailing newline); an empty
        *paths* yields ``""``.
    """
    trie: dict[str, Any] = {}
    for path in paths:
        node = trie
        for segment in (seg for seg in path.split("/") if seg):
            node = node.setdefault(segment, {})
    lines: list[str] = []
    _emit_tree(trie, depth=0, lines=lines)
    return "\n".join(lines)


def _emit_tree(node: dict[str, Any], *, depth: int, lines: list[str]) -> None:
    """Append the indented tree rows for *node* (collapsing single-child dirs).

    Args:
        node: The current trie level (segment → child level).
        depth: The current indent depth (two spaces per level).
        lines: The accumulator the rendered rows are appended to.
    """
    for name in sorted(node):
        child = node[name]
        label = name
        # Collapse a single-child directory chain (a/ → b/ → c) into one
        # ``a/b/c`` label so a deep glob path renders as one compact row.
        while len(child) == 1:
            (only,) = child
            label = f"{label}/{only}"
            child = child[only]
        suffix = "/" if child else ""
        lines.append(f"{'  ' * depth}{label}{suffix}")
        if child:
            _emit_tree(child, depth=depth + 1, lines=lines)


def _wave_metrics(wave: Wave) -> tuple[tuple[str, str], ...]:
    """Build the ``m`` (metrics) rows for a wave.

    A wave's only populated progress signal is its effort bucket (shown as
    the plain bucket label, e.g. ``M``); EU and token rows surface the
    shared empty-state sentinel because estimates / actuals / token
    telemetry are unpopulated scaffolding.

    Args:
        wave: The resolved wave.

    Returns:
        Ordered metric ``(label, value)`` rows.
    """
    rows: list[tuple[str, str]] = []
    if wave.effort_bucket is not None:
        rows.append(("size", wave.effort_bucket.value))
    rows.append(("eu", render_eu_bar_plain(0.0, 0.0)))
    rows.append(("tokens", render_eu_bar_plain(float(wave.tokens_consumed), 0.0)))
    return tuple(rows)


def _completion_metrics(closed: int, total: int) -> tuple[tuple[str, str], ...]:
    """Build the ``m`` rows for an iter / phase from child-wave counts.

    Args:
        closed: Count of closed child waves.
        total: Total child-wave count.

    Returns:
        Ordered metric ``(label, value)`` rows: a real completion bar plus
        empty-state EU / token rows (no estimation data is populated).
    """
    return (
        ("completion", render_completion_bar(closed, total)),
        ("eu", render_eu_bar_plain(0.0, 0.0)),
        ("tokens", render_eu_bar_plain(0.0, 0.0)),
    )


def _wave_card(state: State, wave_id: str) -> DetailCard | None:
    """Build a :class:`DetailCard` for the wave *wave_id*, or ``None``.

    Args:
        state: The bound state to resolve the wave from.
        wave_id: The selected wave id.

    Returns:
        The card, or ``None`` when the id is not a known wave.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    rows: list[tuple[str, str]] = [
        ("id", wave.id),
        ("iter", wave.iter_id),
        ("title", wave.title),
        ("status", wave.status.value),
    ]
    if wave.intent is not None:
        rows.append(("intent", wave.intent.goal))
    if wave.agent_role is not None:
        rows.append(("role", wave.agent_role.value))
    if wave.effort_bucket is not None:
        rows.append(("effort", wave.effort_bucket.value))
    if wave.deps:
        rows.append(("deps", ", ".join(wave.deps)))
    if wave.file_scopes:
        # Render the scopes as a file tree on its own lines under the label
        # (a leading newline drops the tree below "files:", each row indented
        # two cells to nest under it) rather than a flat comma-joined list.
        tree = render_file_tree(wave.file_scopes)
        indented = "\n".join(f"  {line}" for line in tree.split("\n"))
        rows.append(("files", f"\n{indented}"))
    for criterion in wave.success_criteria:
        rows.append(("criterion", criterion))
    attempt_rollup = per_wave_attempt_rollup(wave)
    rows.extend(_attempt_rollup_rows(attempt_rollup))

    history: list[tuple[str, str]] = [
        ("status", wave.status.value),
        ("opened", _fmt_dt(wave.opened_at)),
        ("closed", _fmt_dt(wave.closed_at)),
    ]
    if wave.commit is not None:
        history.append(("commit", wave.commit))

    events: list[tuple[str, str]] = []
    for ann in wave.dispatch_history:
        runtime = ann.runtime_to or ann.runtime_from or "—"
        events.append((f"attempt {ann.attempt}", f"{ann.note.value} ({runtime})"))

    return DetailCard(
        title=f"wave {wave.id}",
        rows=tuple(rows),
        metrics=_wave_metrics(wave),
        history=tuple(history),
        events=tuple(events),
        detail_markdown=_wave_narrative_preview(state, wave),
    )


def _attempt_rollup_rows(rollup: PerWaveAttemptRollup) -> tuple[tuple[str, str], ...]:
    """Build detail-tab rows for a wave's per-attempt timeline.

    Args:
        rollup: The per-wave attempt rollup.

    Returns:
        Rows appended to the wave ``d`` tab.
    """
    return (
        ("attempts", _attempt_summary(rollup)),
        ("error kinds", _error_kind_breakdown(rollup)),
        ("attempt timeline", _attempt_timeline_table(rollup)),
    )


def _attempt_summary(rollup: PerWaveAttemptRollup) -> str:
    """Return compact attempt/retry/blocked/token summary text."""
    return (
        f"{_count_label(rollup.attempt_count, 'attempt')}, "
        f"{_count_label(rollup.retry_count, 'retry', plural='retries')}, "
        f"{rollup.blocked_count} blocked, "
        f"{_count_label(rollup.token_total, 'token')}"
    )


def _count_label(count: int, singular: str, *, plural: str | None = None) -> str:
    """Return a count plus singular/plural noun."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _error_kind_breakdown(rollup: PerWaveAttemptRollup) -> str:
    """Return error-kind breakdown text for the rollup."""
    if not rollup.error_kind_breakdown:
        return "none"
    return ", ".join(f"{kind}={count}" for kind, count in rollup.error_kind_breakdown.items())


def _attempt_timeline_table(rollup: PerWaveAttemptRollup) -> str:
    """Render the 8-column per-attempt timeline table."""
    if not rollup.attempts:
        return "no attempts recorded"
    columns = ("att", "runtime", "started", "ended", "exit", "retry", "blocked", "tokens")
    raw_rows = [
        (
            str(row.attempt),
            row.runtime,
            row.started,
            row.ended,
            row.exit_status,
            row.retry,
            row.blocked,
            row.tokens,
        )
        for row in rollup.attempts
    ]
    widths = [len(value) for value in columns]
    for raw_row in raw_rows:
        widths = [max(width, len(value)) for width, value in zip(widths, raw_row, strict=True)]
    rendered = [_format_attempt_table_row(columns, widths)]
    rendered.extend(_format_attempt_table_row(row, widths) for row in raw_rows)
    return "\n" + "\n".join(f"  {line}" for line in rendered)


def _format_attempt_table_row(row: tuple[str, ...], widths: list[int]) -> str:
    """Format one attempt-table row with padded columns."""
    cells = [value.ljust(width) for value, width in zip(row, widths, strict=True)]
    return "  ".join(cells)


def _wave_narrative_preview(state: State, wave: Wave) -> str:
    """Render the wave's phase NarrativeBundle for the ``d`` tab.

    The narrative builder targets a phase id. A broken wave → iter → phase
    chain degrades to a short note rather than propagating so the drill-in
    seam stays total.

    Args:
        state: The bound state.
        wave: The wave whose parent phase narrative to preview.

    Returns:
        The rendered Markdown narrative, or a fallback note when the scope
        chain cannot be resolved.
    """
    it = state.iters.get(wave.iter_id)
    if it is None:
        logger.info(f"_wave_narrative_preview unresolved iter={wave.iter_id!r} wave={wave.id!r}")
        return "narrative preview unavailable (scope chain unresolved)"
    try:
        bundle = build_narrative(state, it.phase_id)
    except NarrativeNotFoundError as exc:
        logger.info(f"_wave_narrative_preview unresolved wave={wave.id!r} reason={exc}")
        return "narrative preview unavailable (scope chain unresolved)"
    return render_narrative_bundle(bundle)


def _iter_card(state: State, iter_id: str) -> DetailCard | None:
    """Build a :class:`DetailCard` for the iter *iter_id*, or ``None``.

    Args:
        state: The bound state to resolve the iter from.
        iter_id: The selected iter id.

    Returns:
        The card, or ``None`` when the id is not a known iter.
    """
    it = state.iters.get(iter_id)
    if it is None:
        return None
    closed, total = _wave_completion(state, it.wave_ids)
    rows: list[tuple[str, str]] = [
        ("id", it.id),
        ("phase", it.phase_id),
        ("title", it.title),
        ("status", it.status.value),
        ("waves", f"{closed}/{total} closed"),
    ]
    if it.intent is not None:
        rows.append(("intent", it.intent.goal))
    history: list[tuple[str, str]] = [
        ("status", it.status.value),
        ("opened", _fmt_dt(it.opened_at)),
        ("closed", _fmt_dt(it.closed_at)),
    ]
    return DetailCard(
        title=f"iter {it.id}",
        rows=tuple(rows),
        metrics=_completion_metrics(closed, total),
        history=tuple(history),
    )


def _phase_card(state: State, phase_id: str) -> DetailCard | None:
    """Build a :class:`DetailCard` for the phase *phase_id*, or ``None``.

    Phase completion aggregates the closed-wave ratio across every wave of
    every child iter, matching the iter / phase completion convention.

    Args:
        state: The bound state to resolve the phase from.
        phase_id: The selected phase id.

    Returns:
        The card, or ``None`` when the id is not a known phase.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        return None
    wave_ids: list[str] = []
    for iter_id in phase.iter_ids:
        it = state.iters.get(iter_id)
        if it is not None:
            wave_ids.extend(it.wave_ids)
    closed, total = _wave_completion(state, wave_ids)
    rows: list[tuple[str, str]] = [
        ("id", phase.id),
        ("scope", phase.scope_id),
        ("title", phase.title),
        ("status", phase.status.value),
        ("iters", str(len(phase.iter_ids))),
        ("waves", f"{closed}/{total} closed"),
    ]
    if phase.intent is not None:
        rows.append(("intent", phase.intent.goal))
    history: list[tuple[str, str]] = [
        ("status", phase.status.value),
        ("opened", _fmt_dt(phase.opened_at)),
        ("closed", _fmt_dt(phase.closed_at)),
    ]
    return DetailCard(
        title=f"phase {phase.id}",
        rows=tuple(rows),
        metrics=_completion_metrics(closed, total),
        history=tuple(history),
    )


def _wave_completion(state: State, wave_ids: list[str]) -> tuple[int, int]:
    """Return ``(closed, total)`` over *wave_ids* against the state table.

    Args:
        state: The bound state holding the wave table.
        wave_ids: The child-wave ids to tally.

    Returns:
        A ``(closed, total)`` pair; *total* is the count of *wave_ids* that
        resolve to a known wave, *closed* the subset with a CLOSED status.
    """
    from eawf.kernel.state.enums import WaveStatus

    total = 0
    closed = 0
    for wave_id in wave_ids:
        wave = state.waves.get(wave_id)
        if wave is None:
            continue
        total += 1
        if wave.status is WaveStatus.CLOSED:
            closed += 1
    return closed, total


def _backlog_card(state: State, item_id: str) -> DetailCard | None:
    """Build a :class:`DetailCard` for the backlog item *item_id*, or ``None``.

    Args:
        state: The bound state to resolve the item from.
        item_id: The selected backlog item id.

    Returns:
        The card, or ``None`` when the id is not a known backlog item.
    """
    if state.backlog is None:
        return None
    item = state.backlog.get(item_id)
    if item is None:
        return None
    rows: list[tuple[str, str]] = [
        ("id", item.id),
        ("title", item.title),
    ]
    if item.description is not None:
        rows.append(("description", item.description))
    if item.intent is not None:
        rows.append(("intent", item.intent.goal))
    rows.extend(
        [
            ("priority", item.priority.value),
            ("status", item.status.value),
        ]
    )
    if item.resolution is not None:
        rows.append(("resolution", item.resolution))
    return DetailCard(title=f"backlog {item.id}", rows=tuple(rows))


def resolve_detail(state: State | None, selection_id: str) -> DetailCard:
    """Resolve *selection_id* to a :class:`DetailCard` from *state*.

    Tries the wave table, then iters, then phases, then the backlog. An
    unresolvable id (or a ``None`` state) yields a fallback card naming the
    id so the operator sees *something* rather than a crash — the drill-in
    seam must stay total even when the state and the widget row briefly
    disagree (e.g. mid daemon-push).

    Args:
        state: The bound state, or ``None`` when no state is loaded.
        selection_id: The id carried by the selection message.

    Returns:
        The resolved detail card, or a fallback card for an unknown id.
    """
    if state is not None:
        card = (
            _wave_card(state, selection_id)
            or _iter_card(state, selection_id)
            or _phase_card(state, selection_id)
            or _backlog_card(state, selection_id)
        )
        if card is not None:
            return card
    return DetailCard(
        title=f"detail {selection_id}",
        rows=(("id", selection_id), ("note", "no detail available")),
    )


class DetailModal(ModalScreen[None]):
    """Tabbed, scrollable detail card for a row-selected entity (Esc to close).

    Built with a pre-resolved :class:`DetailCard`; the host screen
    resolves the card from ``app.state`` via :func:`resolve_detail` when
    it routes the selection message. The modal owns only the presentation,
    the ``Tab`` / ``Shift+Tab`` tab cycle, the single-letter tab hotkeys
    (``h`` / ``d`` / ``m`` / ``e``), and the ``Esc`` close
    binding. The arrow keys keep their native per-pane scroll behaviour —
    they are deliberately not bound here.
    """

    DEFAULT_CSS: ClassVar[str] = """
    DetailModal {
        align: center middle;
    }
    DetailModal > #detail-card {
        width: 80%;
        max-width: 120;
        height: auto;
        max-height: 85%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    DetailModal .detail-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    DetailModal .detail-row {
        height: auto;
    }
    DetailModal .detail-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    DetailModal TabPane {
        padding: 0;
    }
    """

    #: ``Esc`` closes; ``Tab`` / ``Shift+Tab`` cycle the body tabs; the
    #: single-letter keys jump straight to a tab.
    #: The arrow keys are intentionally absent so they keep scrolling the
    #: focused pane rather than switching tabs.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("tab", "next_tab", "next tab", show=False),
        Binding("shift+tab", "prev_tab", "prev tab", show=False),
        Binding("h", "show_tab('h')", "history", show=False),
        Binding("d", "show_tab('d')", "detail", show=False),
        Binding("m", "show_tab('m')", "metrics", show=False),
        Binding("e", "show_tab('e')", "events", show=False),
    ]

    def __init__(self, card: DetailCard) -> None:
        """Construct the modal for a pre-resolved card.

        Args:
            card: The detail card to render (built by the host screen from
                the selection id + the bound state).
        """
        super().__init__()
        self._card = card
        self._tab_ids = self._present_tabs(card)

    @staticmethod
    def _present_tabs(card: DetailCard) -> tuple[str, ...]:
        """Return the ordered tab ids that have data for *card*.

        The ``d`` detail tab is always present (every card carries field
        rows); the rest appear only when their section is non-empty. Order
        follows the ``h / d / m / e`` brief sequence.

        Args:
            card: The resolved card.

        Returns:
            The ordered, deduplicated tab ids to build panes for.
        """
        present: list[str] = []
        if card.history:
            present.append("h")
        present.append("d")
        if card.metrics:
            present.append("m")
        if card.events:
            present.append("e")
        return tuple(present)

    def _section_rows(self, tab_id: str) -> tuple[tuple[str, str], ...]:
        """Return the ``(label, value)`` rows for the *tab_id* section.

        Args:
            tab_id: One of the row-group tab ids (``h`` / ``d`` / ``m`` /
                ``e``).

        Returns:
            The matching section's rows.
        """
        if tab_id == "h":
            return self._card.history
        if tab_id == "m":
            return self._card.metrics
        if tab_id == "e":
            return self._card.events
        return self._card.rows

    def compose(self) -> ComposeResult:
        """Yield the scrollable, tabbed card: title, tab panes, close hint.

        Within each row-group pane the labels are space-padded to that
        group's widest label so the ``label: value`` colons line up in one
        column (the same mechanism
        :class:`~eawf.surfaces.tui.widgets.status_pane.StatusPane` uses for its
        counter block). Wave ``d`` panes render the NarrativeBundle
        preview as Markdown.
        """
        with VerticalScroll(id="detail-card"):
            yield Static(self._card.title, classes="detail-title")
            with TabbedContent(initial="detail-tab-d"):
                for tab_id in self._tab_ids:
                    with TabPane(_TAB_LABELS[tab_id], id=f"detail-tab-{tab_id}"):
                        yield from self._compose_pane(tab_id)
            yield Static(
                "[ Tab/Shift+Tab cycle · Esc close ]",
                classes="detail-hint",
            )

    def _compose_pane(self, tab_id: str) -> ComposeResult:
        """Yield the body widgets for one tab pane.

        Args:
            tab_id: The tab whose body to render.

        Yields:
            The pane's child widgets — aligned ``label: value`` rows for a
            row-group tab, or a rendered Markdown block for the ``d`` tab
            when the card supplies one.
        """
        if tab_id == "d" and self._card.detail_markdown is not None:
            yield Markdown(self._card.detail_markdown)
            return
        rows = self._section_rows(tab_id)
        label_width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            padded = f"{label}:".ljust(label_width + 1)
            yield Static(f"[$accent]{padded}[/] {escape(value)}", classes="detail-row")

    def _cycle_tab(self, step: int) -> None:
        """Move the active tab by *step* positions, wrapping around.

        Args:
            step: ``+1`` for the next tab, ``-1`` for the previous.
        """
        if len(self._tab_ids) <= 1:
            return
        tabs = self.query_one(TabbedContent)
        active = tabs.active
        try:
            idx = self._tab_ids.index(active.removeprefix("detail-tab-"))
        except ValueError:
            idx = 0
        nxt = self._tab_ids[(idx + step) % len(self._tab_ids)]
        tabs.active = f"detail-tab-{nxt}"

    def action_show_tab(self, tab_id: str) -> None:
        """Jump straight to the *tab_id* pane, or no-op when it is absent.

        Bound to the single-letter tab hotkeys (``h`` / ``d`` / ``m`` /
        ``e``). A key for a tab the card does not carry
        (e.g. ``e`` on an event-less card) is silently ignored so the
        binding stays harmless on every card shape.

        Args:
            tab_id: The target tab id (one of :attr:`_tab_ids`).
        """
        if tab_id not in self._tab_ids:
            return
        self.query_one(TabbedContent).active = f"detail-tab-{tab_id}"

    def action_next_tab(self) -> None:
        """Cycle to the next body tab (``Tab``)."""
        self._cycle_tab(1)

    def action_prev_tab(self) -> None:
        """Cycle to the previous body tab (``Shift+Tab``)."""
        self._cycle_tab(-1)

    def action_close(self) -> None:
        """Dismiss the detail modal (``Esc``)."""
        self.dismiss(None)


__all__ = [
    "DetailCard",
    "DetailModal",
    "render_file_tree",
    "resolve_detail",
]
