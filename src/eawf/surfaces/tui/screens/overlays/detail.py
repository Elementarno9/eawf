"""``DetailModal`` — tabbed detail card for a selected entity.

The drill-in overlay opened when the operator presses ``Enter`` on a row:
the widgets emit a selection message
(:class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable.RowActivated`
carrying a backlog-item id,
:class:`~eawf.surfaces.tui.widgets.roadmap_tree.RoadmapTree.WaveSelected`
carrying a wave id), the shared
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` routes the message here, and this
modal renders the resolved entity's detail in a tabbed, scrollable card.

The card body is split across up to five cosmic-terminal tabs —
``overview`` / ``criteria`` / ``gates`` / ``evidence`` / ``runtime`` —
each carrying a chrome-glyph mnemonic from the reskin sigil vocabulary
(``glyph`` / ``chrome`` in
:mod:`~eawf.surfaces.tui.widgets.sigils`). ``Tab`` / ``Shift+Tab`` cycle
the tabs, the single-letter keys ``o`` / ``c`` / ``g`` / ``v`` / ``r``
jump straight to a tab, and the arrow keys keep their native scroll
behaviour inside the focused pane (they are *not* rebound to
tab-switching). The ``overview`` tab is ALWAYS present (it carries the
entity identity); the rest are built only when their section has data for
the resolved entity, so a wave with no gates shows no ``gates`` tab rather
than an empty one and a hotkey for an absent tab no-ops. The ``runtime``
tab is honest-empty: a wave with no runtime telemetry shows the shared
:data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel rather than
a fabricated ``0.00/0.00`` bar. This wave (P30-I02-W24) lands the chassis;
later iters (I06 data, I04 runtime) fill the criteria / gates / evidence /
runtime panes with their typed projections.

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TabbedContent, TabPane

from eawf.kernel.spec.common import GRANDFATHERED_KIND, CriterionSpec, tier_label
from eawf.kernel.spec.intent import IntentBrief
from eawf.observability.telemetry.store import metrics_db_path, open_store
from eawf.surfaces.render.link_wrap import linkify_text
from eawf.surfaces.render.narrative import (
    NarrativeNotFoundError,
    build_narrative,
    render_narrative_bundle,
)
from eawf.surfaces.tui.screens.overlays.reference import tooltip_for_text
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.eu_bar import (
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    RenderMode,
    render_completion_bar,
)
from eawf.surfaces.tui.widgets.sigils import Sigil
from eawf.workflow.agent_report.rollup import (
    AgentReportRow,
    PerWaveAttemptRollup,
    error_kind_by_attempt_from_store,
    iter_agent_reports,
    per_wave_attempt_rollup,
)

if TYPE_CHECKING:
    from eawf.kernel.state.models import State, Wave

logger = logging.getLogger(__name__)

#: Tab id → ``(label_text, glyph_resolver)`` in cosmic-terminal cycle
#: order. ``label_text`` is the human word; ``glyph_resolver`` returns the
#: chrome / sigil mark for the active render mode, prepended to the word
#: so the pane label reads e.g. the overview triple-bar mark then
#: ``overview`` (unicode) / ``= overview`` (ascii). The overview tab is
#: always built; the rest follow the ``criteria`` / ``gates`` /
#: ``evidence`` / ``runtime`` chassis order and appear only when their
#: section is non-empty.
#:
#: The marks are sourced from the single-home sigil vocabulary
#: (:mod:`~eawf.surfaces.tui.widgets.sigils`): ``overview`` and ``gates``
#: and ``runtime`` are :func:`~eawf.surfaces.tui.widgets.sigils.chrome`
#: roles; ``evidence`` reuses the closed lifecycle
#: :func:`~eawf.surfaces.tui.widgets.sigils.glyph`; ``criteria`` uses a
#: plain right-pointing marker (no chrome role exists for it).
_TAB_LABEL_TEXT: dict[str, str] = {
    "overview": "overview",
    "criteria": "criteria",
    "gates": "gates",
    "evidence": "evidence",
    "runtime": "runtime",
}

#: The unicode / ascii right-pointing marker prefixed to the ``criteria``
#: tab label (no chrome role exists for it, so it is spelled out here).
#: The unicode column is written with a ``\uXXXX`` escape so the source
#: stays ASCII-clean (matching the sigils-module convention); the rendered
#: mark is a black right-pointing small triangle.
_CRITERIA_MARKER: tuple[str, str] = ("\u25b8", ">")


def _tab_glyph(tab_id: str, *, mode: RenderMode) -> str:
    """Return the chrome / sigil mark prefixed to *tab_id*'s pane label.

    Routes each tab to the single-home sigil vocabulary so the chassis
    never invents a glyph: ``overview`` / ``gates`` / ``runtime`` are
    :func:`~eawf.surfaces.tui.widgets.sigils.chrome` roles, ``evidence``
    reuses the closed lifecycle
    :func:`~eawf.surfaces.tui.widgets.sigils.glyph`, and ``criteria`` uses
    the local :data:`_CRITERIA_MARKER` (no chrome role exists for it).

    Args:
        tab_id: One of the five chassis tab ids.
        mode: The App's resolved render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The single-cell glyph string for *tab_id* in the resolved column.

    Raises:
        KeyError: If *tab_id* is not one of the five chassis tab ids.
    """
    if tab_id == "evidence":
        return sigils.glyph(Sigil.CLOSED, mode=mode)
    if tab_id == "criteria":
        unicode_marker, ascii_marker = _CRITERIA_MARKER
        return ascii_marker if mode == sigils.ASCII_MODE else unicode_marker
    chrome_role = {"overview": "overview", "gates": "gate", "runtime": "runtime"}[tab_id]
    return sigils.chrome(chrome_role, mode=mode)


def tab_label(tab_id: str, *, mode: RenderMode) -> str:
    """Return the full pane label (glyph + word) for *tab_id* in *mode*.

    Args:
        tab_id: One of the five chassis tab ids.
        mode: The App's resolved render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The ``"<glyph> <word>"`` pane label, e.g. the overview mark
        followed by ``"overview"``.

    Raises:
        KeyError: If *tab_id* is not one of the five chassis tab ids.
    """
    return f"{_tab_glyph(tab_id, mode=mode)} {_TAB_LABEL_TEXT[tab_id]}"


#: Lifecycle-status string -> the :class:`~eawf.surfaces.tui.widgets.sigils.Sigil`
#: whose glyph prefixes the overview ``status`` row. The entity kinds
#: (wave / iter / phase / backlog) span more status words than the closed
#: six-member :class:`Sigil` enum, so the planning / active / open /
#: archived / deferred / abandoned strings fold onto the nearest lifecycle
#: shape rather than crashing on an unmapped key.
_STATUS_SIGIL: dict[str, Sigil] = {
    "pending": Sigil.PENDING,
    "planned": Sigil.PENDING,
    "open": Sigil.PENDING,
    "claimed": Sigil.CLAIMED,
    "in_progress": Sigil.RUNNING,
    "active": Sigil.RUNNING,
    "running": Sigil.RUNNING,
    "closed": Sigil.CLOSED,
    "failed": Sigil.FAILED,
    "abandoned": Sigil.FAILED,
    "archived": Sigil.FAILED,
    "deferred": Sigil.FAILED,
}


def _status_with_sigil(status_value: str, *, mode: RenderMode) -> str:
    """Return the overview ``status`` value prefixed with its sigil glyph.

    Maps *status_value* onto a lifecycle :class:`Sigil` (via
    :data:`_STATUS_SIGIL`) and prepends that sigil's glyph for the active
    render *mode*, so the overview reads e.g. the closed filled-circle then
    ``closed`` rather than a bare ``closed`` word. An unmapped status word
    (a status enum that drifted past the table) degrades to the bare word
    so the drill-in seam stays total.

    Args:
        status_value: The lifecycle-status string off the resolved entity.
        mode: The active render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The ``"<glyph> <status>"`` string, or the bare status word when the
        status has no mapped sigil.
    """
    sigil = _STATUS_SIGIL.get(status_value)
    if sigil is None:
        return status_value
    return f"{sigils.glyph(sigil, mode=mode)} {status_value}"


@dataclass(frozen=True)
class DetailCard:
    """A resolved detail card: a title plus the five chassis section groups.

    The card carries one row group per cosmic-terminal tab. ``rows`` is the
    canonical ``overview`` field group — always populated (every card
    carries an identity); the remaining groups are populated only when the
    entity has data for them, and the modal builds a tab only for a
    non-empty group. ``detail_markdown`` is the optional Markdown body for
    the ``overview`` tab; wave cards use it for the NarrativeBundle
    preview, while non-wave cards render the regular ``rows`` group.

    The ``criteria`` group carries the full typed criterion projection: a
    text row per criterion plus, for an authored (non-grandfathered)
    criterion, its oracle tier label, ``evidence_kind``, and
    ``measurable_signal``; a grandfathered legacy criterion shows the text
    plus a grandfathered marker and no tier badge. The ``gates`` /
    ``evidence`` / ``runtime`` groups are the chassis seams: ``evidence``
    carries the wave's attempt rollup + dispatch history, ``runtime``
    carries the size + honest-empty EU / token rows, and ``gates`` stays
    empty (so a wave with no gates shows no gates tab).

    Attributes:
        title: The card heading (e.g. ``wave P26-I01-W19`` /
            ``iter P26-I01`` / ``phase P26`` / ``backlog B042``).
        rows: The ``overview`` group — ordered ``(label, value)`` pairs.
        criteria: The ``criteria`` group — the typed criterion projection
            (text row plus tier label / evidence_kind / measurable_signal
            for an authored criterion; text + grandfathered marker and no
            tier badge for a legacy criterion).
        gates: The ``gates`` group — gate-pack rows (I06 fills; empty now
            so a wave with no gates renders no gates tab).
        evidence: The ``evidence`` group — the wave's attempt rollup +
            dispatch-history rows (I06 folds in the report rollup).
        runtime: The ``runtime`` group — size + honest-empty EU / token
            rows (I04 fills real runtime telemetry). A no-runtime wave's
            EU / token rows are the shared
            :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel,
            never a fabricated ``0.00/0.00`` bar.
        detail_markdown: Optional Markdown body for the ``overview`` tab.
            When ``None``, the ``overview`` tab renders ``rows`` as aligned
            field rows.
    """

    title: str
    rows: tuple[tuple[str, str], ...]
    criteria: tuple[tuple[str, str], ...] = ()
    gates: tuple[tuple[str, str], ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    runtime: tuple[tuple[str, str], ...] = ()
    detail_markdown: str | None = None


def _intent_rows(intent: IntentBrief | None) -> list[tuple[str, str]]:
    """Project an :class:`IntentBrief` (or ``None``) into detail-card rows.

    Emits the W24-audited fields (``problem`` / ``desired_outcome``
    plus the optional ``planned_steps`` / ``risks`` /
    ``priority_rationale``) so the operator sees the structured intent
    every time.

    Args:
        intent: The entity's :attr:`IntentBrief` (or ``None`` when the
            entity has not been wired with one).

    Returns:
        Ordered ``(label, value)`` rows. Empty list when *intent* is
        ``None``; otherwise the two required rows plus a row for each
        populated optional field.
    """
    if intent is None:
        return []
    rows: list[tuple[str, str]] = [
        ("problem", intent.problem),
        ("desired outcome", intent.desired_outcome),
    ]
    if intent.planned_steps:
        rows.append(("planned steps", "; ".join(intent.planned_steps)))
    if intent.risks:
        rows.append(("risks", "; ".join(intent.risks)))
    if intent.priority_rationale is not None:
        rows.append(("priority rationale", intent.priority_rationale))
    return rows


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


def _wave_runtime(wave: Wave) -> tuple[tuple[str, str], ...]:
    """Build the ``runtime`` tab rows for a wave (honest-empty until I04).

    A wave's only populated progress signal is its effort bucket (shown as
    the plain bucket label, e.g. ``M``). The EU and token rows are the
    shared :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel
    rather than a fabricated ``0.00/0.00`` bar, because estimates /
    actuals / token telemetry are unpopulated scaffolding I04 fills. The
    runtime tab therefore stays honest: a no-runtime wave reads "no data",
    never a manufactured zero.

    Args:
        wave: The resolved wave.

    Returns:
        Ordered runtime ``(label, value)`` rows.
    """
    rows: list[tuple[str, str]] = []
    if wave.effort_bucket is not None:
        rows.append(("size", wave.effort_bucket.value))
    rows.append(("eu", EMPTY_STATE))
    rows.append(("tokens", EMPTY_STATE))
    return tuple(rows)


def _completion_runtime(closed: int, total: int) -> tuple[tuple[str, str], ...]:
    """Build the iter / phase ``runtime`` rows from child-wave counts.

    Args:
        closed: Count of closed child waves.
        total: Total child-wave count.

    Returns:
        Ordered runtime ``(label, value)`` rows: a real completion bar plus
        honest-empty EU / token sentinel rows (no estimation data is
        populated, so the rows read "no data" rather than ``0.00/0.00``).
    """
    return (
        ("completion", render_completion_bar(closed, total)),
        ("eu", EMPTY_STATE),
        ("tokens", EMPTY_STATE),
    )


def _criteria_rows(criteria: Iterable[CriterionSpec]) -> tuple[tuple[str, str], ...]:
    """Project a wave's typed criteria into criteria-tab ``(label, value)`` rows.

    Each criterion contributes its ``.text`` row first, then differs by
    whether it is an authored typed criterion or a grandfathered legacy
    string:

    - An authored (non-grandfathered) criterion surfaces the full typed
      shape: the oracle tier label (:func:`~eawf.kernel.spec.common.tier_label`,
      omitted only when the tier is still unpopulated), the
      ``evidence_kind``, and the ``measurable_signal``.
    - A grandfathered criterion (``kind == GRANDFATHERED_KIND`` -- a legacy
      string the ``1.6 -> 1.7`` migration wrapped) surfaces the text plus a
      ``grandfathered`` marker and NO tier badge: it carries no authored
      tier, so a fabricated badge would be a lie.

    Args:
        criteria: The wave's typed criterion rows.

    Returns:
        Ordered ``(label, value)`` rows for the criteria tab.
    """
    rows: list[tuple[str, str]] = []
    for criterion in criteria:
        rows.append(("criterion", criterion.text))
        if criterion.kind == GRANDFATHERED_KIND:
            rows.append(("grandfathered", "legacy criterion (no typed tier)"))
            continue
        if criterion.oracle_tier is not None:
            rows.append(("tier", tier_label(criterion.oracle_tier)))
        rows.append(("evidence", criterion.evidence_kind))
        rows.append(("signal", criterion.measurable_signal))
    return tuple(rows)


def _wave_card(
    state: State,
    wave_id: str,
    *,
    reports: Iterable[AgentReportRow] = (),
    error_kind_by_attempt: Mapping[int, Iterable[str]] | None = None,
) -> DetailCard | None:
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
    rows.extend(_intent_rows(wave.intent))
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

    # The criteria tab renders the full typed criterion: the legacy text
    # row plus, for an authored (non-grandfathered) criterion, the oracle
    # tier label, the evidence_kind, and the measurable_signal. A
    # grandfathered criterion (a legacy string the 1.6->1.7 migration
    # wrapped) shows the text plus a grandfathered marker and NO fabricated
    # tier badge -- it has no authored tier to surface.
    criteria: list[tuple[str, str]] = list(_criteria_rows(wave.success_criteria))

    # The evidence tab folds in the attempt rollup plus the dispatch
    # history (I06 layers the typed report rollup on top of this seam).
    attempt_rollup = per_wave_attempt_rollup(
        wave,
        reports=reports,
        error_kind_by_attempt=error_kind_by_attempt,
    )
    evidence: list[tuple[str, str]] = list(_attempt_rollup_rows(attempt_rollup))
    for ann in wave.dispatch_history:
        runtime = ann.runtime_to or ann.runtime_from or "—"
        evidence.append((f"attempt {ann.attempt}", f"{ann.note.value} ({runtime})"))

    return DetailCard(
        title=f"wave {wave.id}",
        rows=tuple(rows),
        criteria=tuple(criteria),
        evidence=tuple(evidence),
        runtime=_wave_runtime(wave),
        detail_markdown=_wave_narrative_preview(state, wave, reports=reports),
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


def _wave_narrative_preview(
    state: State,
    wave: Wave,
    *,
    reports: Iterable[AgentReportRow] = (),
) -> str:
    """Render the wave's own NarrativeBundle preview for the ``d`` tab.

    Dispatches the wave id through :func:`build_narrative`, which fans
    out to the wave-specific bundle (post-W55: the wave bundle quotes
    the wave's :class:`IntentBrief` + commit + claim attempts + the
    latest :class:`ActualSummary` rather than re-rendering the parent
    phase rollup). An unresolved wave degrades to a short note so the
    drill-in seam stays total.

    Args:
        state: The bound state.
        wave: The wave whose own narrative to preview.
        reports: Optional agent-report rows scoped to the wave, used by
            the wave builder to count blocked-attempt verdicts.

    Returns:
        The rendered Markdown narrative, or a fallback note when the
        wave id cannot be resolved through the narrative builder.
    """
    try:
        bundle = build_narrative(state, wave.id, reports=reports)
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
    rows.extend(_intent_rows(it.intent))
    # No standalone history tab in the five-tab chassis; the lifecycle
    # timestamps fold into the overview group beside the status row.
    rows.append(("opened", _fmt_dt(it.opened_at)))
    rows.append(("closed", _fmt_dt(it.closed_at)))
    return DetailCard(
        title=f"iter {it.id}",
        rows=tuple(rows),
        runtime=_completion_runtime(closed, total),
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
    rows.extend(_intent_rows(phase.intent))
    # No standalone history tab in the five-tab chassis; the lifecycle
    # timestamps fold into the overview group beside the status row.
    rows.append(("opened", _fmt_dt(phase.opened_at)))
    rows.append(("closed", _fmt_dt(phase.closed_at)))
    return DetailCard(
        title=f"phase {phase.id}",
        rows=tuple(rows),
        runtime=_completion_runtime(closed, total),
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
    rows.extend(_intent_rows(item.intent))
    rows.extend(
        [
            ("priority", item.priority.value),
            ("status", item.status.value),
        ]
    )
    if item.resolution is not None:
        rows.append(("resolution", item.resolution))
    return DetailCard(title=f"backlog {item.id}", rows=tuple(rows))


def resolve_detail(
    state: State | None,
    selection_id: str,
    *,
    reports: Iterable[AgentReportRow] = (),
    error_kind_by_attempt: Mapping[int, Iterable[str]] | None = None,
    state_path: Path | None = None,
) -> DetailCard:
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
        if selection_id in state.waves and state_path is not None:
            reports = _report_rows_for_wave(state_path, selection_id)
            error_kind_by_attempt = _error_kind_by_attempt_for_wave(
                state,
                selection_id,
                state_path,
            )
        card = (
            _wave_card(
                state,
                selection_id,
                reports=reports,
                error_kind_by_attempt=error_kind_by_attempt,
            )
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


def _report_rows_for_wave(state_path: Path, wave_id: str) -> tuple[AgentReportRow, ...]:
    """Load report rows for *wave_id* from role report stores."""
    rows = [
        *iter_agent_reports(state_path, base_id=wave_id),
        *iter_agent_reports(state_path, scope_id=wave_id),
    ]
    deduped: dict[tuple[str, str], AgentReportRow] = {}
    for row in rows:
        deduped[(row.store_kind, row.envelope.id)] = row
    return tuple(
        sorted(
            deduped.values(),
            key=lambda row: (row.envelope.created_at, row.envelope.id),
        )
    )


def _error_kind_by_attempt_for_wave(
    state: State,
    wave_id: str,
    state_path: Path,
) -> dict[int, tuple[str, ...]]:
    """Load telemetry error-kind rows for *wave_id* from local metrics DB."""
    wave = state.waves.get(wave_id)
    if wave is None:
        return {}
    db_path = metrics_db_path(state_path)
    if not db_path.is_file():
        return {}
    store = open_store("sqlite", db_path)
    try:
        return error_kind_by_attempt_from_store(wave, store)
    except Exception as exc:
        logger.debug(f"_error_kind_by_attempt_for_wave fallback wave={wave_id!r} cause={exc!r}")
        return {}
    finally:
        store.close()


class DetailModal(ModalScreen[None]):
    """Tabbed, scrollable detail card for a row-selected entity (Esc to close).

    Built with a pre-resolved :class:`DetailCard`; the host screen
    resolves the card from ``app.state`` via :func:`resolve_detail` when
    it routes the selection message. The modal owns only the presentation,
    the ``Tab`` / ``Shift+Tab`` tab cycle, the single-letter tab hotkeys
    (``o`` / ``c`` / ``g`` / ``v`` / ``r`` for overview / criteria / gates
    / evidence / runtime), and the ``Esc`` close binding. The arrow keys
    keep their native per-pane scroll behaviour — they are deliberately not
    bound here.
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
    #: single-letter keys jump straight to one of the five chassis tabs.
    #: The arrow keys are intentionally absent so they keep scrolling the
    #: focused pane rather than switching tabs.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("tab", "next_tab", "next tab", show=False),
        Binding("shift+tab", "prev_tab", "prev tab", show=False),
        Binding("o", "show_tab('overview')", "overview", show=False),
        Binding("c", "show_tab('criteria')", "criteria", show=False),
        Binding("g", "show_tab('gates')", "gates", show=False),
        Binding("v", "show_tab('evidence')", "evidence", show=False),
        Binding("r", "show_tab('runtime')", "runtime", show=False),
    ]

    def __init__(
        self,
        card: DetailCard,
        *,
        state: State | None = None,
        entity_id: str | None = None,
    ) -> None:
        """Construct the modal for a pre-resolved card.

        Args:
            card: The detail card to render (built by the host screen from
                the selection id + the bound state).
            state: Optional bound state used for hover previews on
                clickable refs found in row values.
            entity_id: The originating entity id (the selection id the card
                was resolved from), used by the row-drill path to suppress a
                duplicate push of the entity already on the modal top. ``None``
                for direct constructions that opt out of the entity dedup.
        """
        super().__init__()
        self._card = card
        self._state = state
        self._entity_id = entity_id
        self._tab_ids = self._present_tabs(card)

    @property
    def entity_id(self) -> str | None:
        """Return the originating entity id, or ``None`` when not drill-sourced.

        The row-drill path (:meth:`~eawf.surfaces.tui.scopes.ScopeScreen._open_detail`)
        reads this off the top-of-stack modal to suppress a duplicate push of
        the same entity; a card constructed without an ``entity_id`` (a direct
        push) carries ``None`` and so never deduplicates.
        """
        return self._entity_id

    @property
    def dedupe_key(self) -> str | None:
        """Return the modal's entity identity for the push-stack dedup.

        The :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` chokepoint reads
        this off the new modal and the current top-of-stack overlay: when both
        carry the same non-``None`` key, the new push is suppressed so
        re-choosing the entity already on top is a no-op. A card built without
        an ``entity_id`` carries ``None`` here and so stays stackable.
        """
        return self._entity_id

    def _enrich_from_app(self) -> None:
        """Refresh wave cards with store-backed report/error rows when mounted."""
        if self._state is None or not self._card.title.startswith("wave "):
            return
        try:
            state_path = getattr(self.app, "_state_path", None)
        except RuntimeError:
            return
        if not isinstance(state_path, Path):
            return
        wave_id = self._card.title.removeprefix("wave ").strip()
        if not wave_id:
            return
        self._card = resolve_detail(self._state, wave_id, state_path=state_path)
        self._tab_ids = self._present_tabs(self._card)

    @staticmethod
    def _present_tabs(card: DetailCard) -> tuple[str, ...]:
        """Return the ordered tab ids that have data for *card*.

        The ``overview`` tab is ALWAYS present (every card carries an
        identity); the ``criteria`` / ``gates`` / ``evidence`` / ``runtime``
        tabs appear only when their section is non-empty, so a wave with no
        gates renders no gates tab. Order follows the chassis sequence.

        Args:
            card: The resolved card.

        Returns:
            The ordered tab ids to build panes for.
        """
        present: list[str] = ["overview"]
        if card.criteria:
            present.append("criteria")
        if card.gates:
            present.append("gates")
        if card.evidence:
            present.append("evidence")
        if card.runtime:
            present.append("runtime")
        return tuple(present)

    def _section_rows(self, tab_id: str) -> tuple[tuple[str, str], ...]:
        """Return the ``(label, value)`` rows for the *tab_id* section.

        Args:
            tab_id: One of the chassis tab ids (``overview`` / ``criteria``
                / ``gates`` / ``evidence`` / ``runtime``).

        Returns:
            The matching section's rows.
        """
        if tab_id == "criteria":
            return self._card.criteria
        if tab_id == "gates":
            return self._card.gates
        if tab_id == "evidence":
            return self._card.evidence
        if tab_id == "runtime":
            return self._card.runtime
        return self._card.rows

    def _render_mode(self) -> RenderMode:
        """Return the App's resolved render mode, defaulting when unbound.

        Reads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` so the pane
        labels pick the right chrome-glyph column. A bare harness whose host
        App carries no ``render_mode`` (a direct construction outside the
        full app) falls back to the shared default.

        Returns:
            The active render mode (``"unicode"`` / ``"ascii"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def compose(self) -> ComposeResult:
        """Yield the scrollable, tabbed card: title, tab panes, close hint.

        Within each row-group pane the labels are space-padded to that
        group's widest label so the ``label: value`` colons line up in one
        column (the same mechanism
        :class:`~eawf.surfaces.tui.widgets.status_pane.StatusPane` uses for
        its counter block). Each pane label carries its chrome-glyph
        mnemonic for the active render mode. The wave ``overview`` pane
        renders the NarrativeBundle preview as Markdown.
        """
        self._enrich_from_app()
        mode = self._render_mode()
        with VerticalScroll(id="detail-card"):
            yield Static(self._card.title, classes="detail-title")
            with TabbedContent(initial="detail-tab-overview"):
                for tab_id in self._tab_ids:
                    label = tab_label(tab_id, mode=mode)
                    with TabPane(label, id=f"detail-tab-{tab_id}"):
                        yield from self._compose_pane(tab_id, mode=mode)
            yield Static(
                "[ Tab/Shift+Tab cycle · Esc close ]",
                classes="detail-hint",
            )

    def _compose_pane(self, tab_id: str, *, mode: RenderMode) -> ComposeResult:
        """Yield the body widgets for one tab pane.

        Args:
            tab_id: The tab whose body to render.
            mode: The active render mode, threaded through so the overview
                ``status`` row prepends the matching lifecycle sigil glyph.

        Yields:
            The pane's child widgets — aligned ``label: value`` rows for a
            row-group tab, or a rendered Markdown block for the ``overview``
            tab when the card supplies one.
        """
        if tab_id == "overview" and self._card.detail_markdown is not None:
            yield Markdown(self._card.detail_markdown)
            return
        rows = self._section_rows(tab_id)
        label_width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            padded = f"{label}:".ljust(label_width + 1)
            display = _status_with_sigil(value, mode=mode) if label == "status" else value
            row = Static(
                f"[$accent]{escape(padded)}[/] {linkify_text(display)}",
                classes="detail-row",
            )
            row.tooltip = tooltip_for_text(self._state, value)
            yield row

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

        Bound to the single-letter tab hotkeys (``o`` / ``c`` / ``g`` /
        ``v`` / ``r`` for overview / criteria / gates / evidence /
        runtime). A key for a tab the card does not carry (e.g. ``g`` on a
        gate-less wave) is silently ignored so the binding stays harmless on
        every card shape.

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
    "tab_label",
]
