"""``DetailModal`` -- tabbed detail card for a selected entity.

The drill-in overlay opened when the operator presses ``Enter`` on a row: a
widget emits a selection message (a backlog-item or wave id), the shared
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` routes it here, and this modal
renders the resolved entity in a tabbed, scrollable card.

The body is split across the cosmic-terminal tabs --
``overview`` / ``criteria`` / ``gates`` / ``evidence`` / ``metrics`` /
``cost`` / ``history`` -- each with a chrome-glyph mnemonic from the reskin
sigil vocabulary. ``Tab`` / ``Shift+Tab`` cycle the tabs; the single-letter
keys ``o`` / ``c`` / ``g`` / ``v`` / ``m`` / ``$`` / ``h`` jump to one, and
the arrow keys keep their native scroll. A ``wave`` card renders the full
five-tab chassis always (overview / criteria / gates / evidence / metrics) --
an empty section shows an honest-empty notice rather than hiding the tab (the
"render the tabs now, fill later" directive) -- while the ``cost`` tab stays
data-gated and a non-wave card (phase / iter / backlog / incident peek)
builds only the groups it populates. An ``incident`` card carries its
chronological event timeline on its own ``history`` tab (designer ruling
A3-a), and its ``evidence`` group keeps a one-line link pointing at it.
Honest absence is first-class: the ``metrics`` tab shows the
:data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel for an actual
that has not landed rather than a fabricated zero (the effort-bucket EU
estimate + the session-derived runtime-EU actual + the actual consumed tokens
paint as they arrive), and the ``cost`` tab renders "no metered sessions yet"
(with an em-dash + inert marker for an unbillable attempt).

Entity resolution is a pure function (:func:`resolve_detail`) over the
reactive :class:`~eawf.kernel.state.models.State` returning a typed
:class:`DetailCard`; an unknown id yields a total fallback card so the seam
never crashes mid daemon-push. Purity keeps the rendered detail unit-testable
without mounting Textual; the modal is a thin view built from a pre-resolved
card and never reaches back into App state itself.
"""

# noqa: EAWF010 at-cap detail chassis; W15 added the always-present five-tab
# honest-empty seam (a bounded render fix); extract-module split deferred.
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TabbedContent, TabPane

from eawf.kernel.spec.common import (
    GRANDFATHERED_KIND,
    CriterionSpec,
    GateSpec,
    tier_label,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    BacklogStatus,
    CloseAttemptStatus,
    IncidentStatus,
    MeasurementStatus,
    StoreKind,
    WaveStatus,
)
from eawf.observability.telemetry.join import (
    DEFAULT_EU_MINUTES,
    WaveSessionRollup,
    _duration_ms_to_eu,
)
from eawf.observability.telemetry.store import metrics_db_path, open_store
from eawf.surfaces.render.link_wrap import PreMarkedText, linkify_text
from eawf.surfaces.render.narrative import (
    NarrativeNotFoundError,
    build_narrative,
    render_narrative_bundle,
)
from eawf.surfaces.render.units import format_compact_utc, format_tokens
from eawf.surfaces.tui.screens.overlays.detail_attempts import attempt_rollup_rows
from eawf.surfaces.tui.screens.overlays.detail_cost import (
    wave_cost_rollup_for_wave,
    wave_cost_rows,
)
from eawf.surfaces.tui.screens.overlays.detail_incident import (
    incident_timeline_rows,
    load_incident_timeline,
)
from eawf.surfaces.tui.screens.overlays.reference import tooltip_for_text
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.eu_bar import (
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    RenderMode,
    render_completion_bar,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, status_sigil
from eawf.workflow.agent_report.rollup import (
    AgentReportRow,
    error_kind_by_attempt_from_store,
    iter_agent_reports,
    per_wave_attempt_rollup,
)
from eawf.workflow.estimation.buckets import BUCKET_EU

if TYPE_CHECKING:
    from eawf.kernel.state.models import SessionAttempt, State, Wave

logger = logging.getLogger(__name__)

#: Tab id → ``(label_text, glyph_resolver)`` in cosmic-terminal cycle
#: order. ``label_text`` is the human word; ``glyph_resolver`` returns the
#: chrome / sigil mark for the active render mode, prepended to the word
#: so the pane label reads e.g. the overview triple-bar mark then
#: ``overview`` (unicode) / ``= overview`` (ascii). The overview tab is
#: always built; the rest follow the ``criteria`` / ``gates`` /
#: ``evidence`` / ``metrics`` chassis order and appear only when their
#: section is non-empty.
#:
#: The marks are sourced from the single-home sigil vocabulary
#: (:mod:`~eawf.surfaces.tui.widgets.sigils`): every tab marker --
#: ``overview`` / ``gates`` / ``metrics`` / ``criteria`` / ``cost`` /
#: ``history`` -- is a :func:`~eawf.surfaces.tui.widgets.sigils.chrome` role,
#: and ``evidence`` reuses the closed lifecycle
#: :func:`~eawf.surfaces.tui.widgets.sigils.glyph`.
_TAB_LABEL_TEXT: dict[str, str] = {
    "overview": "overview",
    "criteria": "criteria",
    "gates": "gates",
    "evidence": "evidence",
    "metrics": "metrics",
    "cost": "cost",
    "history": "history",
}

#: Each chassis tab id -> its
#: :func:`~eawf.surfaces.tui.widgets.sigils.chrome` role. ``evidence`` is the
#: one tab that reuses a lifecycle
#: :func:`~eawf.surfaces.tui.widgets.sigils.glyph` (the closed circle) rather
#: than a chrome role, so it is handled separately; every other tab marker --
#: including the ``criteria`` / ``cost`` / ``history`` markers now folded out
#: of this module into the single-home chrome vocabulary --
#: resolves through a chrome role so the chassis invents no glyph of its own.
_TAB_CHROME_ROLE: dict[str, str] = {
    "overview": "overview",
    "gates": "gate",
    "metrics": "metrics",
    "criteria": "criteria",
    "cost": "cost",
    "history": "history",
}


def _tab_glyph(tab_id: str, *, mode: RenderMode) -> str:
    """Return the chrome / sigil mark prefixed to *tab_id*'s pane label.

    Routes each tab to the single-home sigil vocabulary so the chassis
    never invents a glyph: every tab marker -- ``overview`` / ``gates`` /
    ``metrics`` and the ``criteria`` / ``cost`` / ``history`` markers folded
    into :data:`~eawf.surfaces.tui.widgets.sigils._CHROME` -- resolves through
    a :func:`~eawf.surfaces.tui.widgets.sigils.chrome` role
    (:data:`_TAB_CHROME_ROLE`); only ``evidence`` reuses the closed lifecycle
    :func:`~eawf.surfaces.tui.widgets.sigils.glyph`.

    Args:
        tab_id: One of the chassis tab ids.
        mode: The App's resolved render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The single-cell glyph string for *tab_id* in the resolved column.

    Raises:
        KeyError: If *tab_id* is not one of the chassis tab ids.
    """
    if tab_id == "evidence":
        return sigils.glyph(Sigil.CLOSED, mode=mode)
    return sigils.chrome(_TAB_CHROME_ROLE[tab_id], mode=mode)


def tab_label(tab_id: str, *, mode: RenderMode) -> str:
    """Return the full pane label (glyph + word) for *tab_id* in *mode*.

    Args:
        tab_id: One of the chassis tab ids.
        mode: The App's resolved render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The ``"<glyph> <word>"`` pane label, e.g. the overview mark
        followed by ``"overview"``.

    Raises:
        KeyError: If *tab_id* is not one of the chassis tab ids.
    """
    return f"{_tab_glyph(tab_id, mode=mode)} {_TAB_LABEL_TEXT[tab_id]}"


#: An :class:`~eawf.kernel.state.enums.IncidentStatus` member -> the covered
#: lifecycle-status member whose ratified
#: :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` glyph it borrows.
#: ``IncidentStatus`` is the one detail-card status enum the single-home
#: :data:`~eawf.surfaces.tui.widgets.sigils._EXTENDED` table does not cover,
#: so an incident status is first folded onto its nearest covered lifecycle
#: shape (open -> the OPEN ring, mitigated -> the in-progress diamond,
#: resolved -> the CLOSED circle, wont-fix -> the DEFERRED withheld slash)
#: before it is routed through the canonical resolver. Every other detail-card
#: status (wave / iter / phase / backlog) is covered directly and needs no fold.
_INCIDENT_STATUS_SIGIL_KEY: dict[IncidentStatus, BacklogStatus | WaveStatus] = {
    IncidentStatus.OPEN: BacklogStatus.OPEN,
    IncidentStatus.MITIGATED: WaveStatus.IN_PROGRESS,
    IncidentStatus.RESOLVED: BacklogStatus.CLOSED,
    IncidentStatus.WONT_FIX: BacklogStatus.DEFERRED,
}


def _status_with_sigil(status: object, *, mode: RenderMode) -> str:
    """Return the ``status`` value prefixed with its ratified sigil glyph.

    Routes *status* (a lifecycle-status enum member off the resolved entity)
    through the single-home
    :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` resolver, so the
    overview / pill reads e.g. the closed filled-circle then ``closed`` rather
    than a bare ``closed`` word -- and the glyph is the SAME ratified mark the
    roadmap tree and status pane render, never an ad-hoc parallel mapping. The
    one status enum the resolver's table does not cover --
    :class:`~eawf.kernel.state.enums.IncidentStatus` -- is first folded onto its
    nearest covered lifecycle member (via :data:`_INCIDENT_STATUS_SIGIL_KEY`) so
    an incident card routes through the same resolver as every other card kind.

    Args:
        status: The lifecycle-status enum member off the resolved entity.
        mode: The active render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The ``"<glyph> <status>"`` string, with the glyph resolved via
        :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil`.
    """
    resolved_key = (
        _INCIDENT_STATUS_SIGIL_KEY.get(status) if isinstance(status, IncidentStatus) else status
    )
    glyph = status_sigil(resolved_key).render(mode=mode)
    word = status.value if isinstance(status, Enum) else str(status)
    return f"{glyph} {word}"


@dataclass(frozen=True)
class DetailCard:
    """A resolved detail card: a title plus the six chassis section groups.

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
    plus a grandfathered marker and no tier badge. The ``gates`` group
    carries the wave's typed gate rows grouped under their owning criterion
    id, each naming the gate's check kind; a wave with no gates leaves the
    group empty so the modal builds no gates tab. The ``evidence`` /
    ``metrics`` groups are the remaining chassis seams: ``evidence`` carries
    the wave's attempt rollup + dispatch history and ``metrics`` carries the
    size + the runtime-EU actual against its effort-bucket estimate + the
    actual consumed tokens.

    Attributes:
        title: The card heading (e.g. ``wave P26-I01-W19`` /
            ``iter P26-I01`` / ``phase P26`` / ``backlog B042``).
        rows: The ``overview`` group — ordered ``(label, value)`` pairs.
        criteria: The ``criteria`` group — the typed criterion projection
            (text row plus tier label / evidence_kind / measurable_signal
            for an authored criterion; text + grandfathered marker and no
            tier badge for a legacy criterion).
        gates: The ``gates`` group — the wave's typed gate rows grouped
            under a ``criterion`` header per distinct criterion id, each
            ``gate`` row naming the gate's check kind. Empty for a wave with
            no gates, so the modal renders no gates tab (absent, not empty).
        evidence: The ``evidence`` group — the wave's auditor verdict, the
            attempt rollup, and the provenance / dispatch-history rows (the
            ``claimed`` work-start row plus one row per dispatch annotation,
            each appending any runtime-switch reason).
        metrics: The ``metrics`` group — the effort-bucket size, the
            session-derived runtime-EU actual, the effort-bucket EU estimate
            it is measured against, and the actual consumed tokens read from
            the wave's session rollup (the same source the ``cost`` tab uses).
            A wave with no ended sessions renders the shared
            :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel for
            the EU actual, and a wave with no metered token rollup renders it
            for the token row, so an honest absence stays distinguishable from
            a measured zero — the EU estimate still shows whenever a bucket is
            set, since it is a plan figure independent of runtime.
        cost: The ``cost`` group — the per-attempt cost columns (attempt,
            model, in/out tokens, cache create/read tokens, priced cost, EU)
            plus an aggregate cost bar, joined from the wave's metered
            telemetry sessions. A wave whose attempts joined no metered
            session carries a single honest "no metered sessions yet" row,
            and an attempt the pricing snapshot could not bill renders an
            em-dash plus an inert un-billed marker. Empty for a wave with no
            session attempts, so the modal renders no cost tab.
        history: The ``history`` group — the entity's chronological event
            timeline. Populated for an incident card from its store-loaded
            timeline rows (one ``event`` row per recorded entry, oldest-first);
            the incident's ``evidence`` group keeps a one-line link pointing
            here (designer ruling A3-a). Empty for every other card kind, so
            the modal renders no history tab.
        detail_markdown: Optional Markdown body for the ``overview`` tab.
            When ``None``, the ``overview`` tab renders ``rows`` as aligned
            field rows.
    """

    title: str
    rows: tuple[tuple[str, str], ...]
    criteria: tuple[tuple[str, str], ...] = ()
    gates: tuple[tuple[str, str], ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    metrics: tuple[tuple[str, str], ...] = ()
    cost: tuple[tuple[str, str], ...] = ()
    history: tuple[tuple[str, str], ...] = ()
    detail_markdown: str | None = None
    #: The entity kind the card resolves. ``"wave"`` cards render the full
    #: five-tab chassis (overview / criteria / gates / evidence / metrics)
    #: unconditionally -- honest-empty until the data lands, the "render the
    #: tabs now, fill later" reskin directive -- while every other kind keeps
    #: only the groups it populates.
    kind: str = "entity"
    #: The overview glance: a compact ``(label, value)`` quad summarising a wave
    #: -- criteria bound, gate count, evidence records, metrics EU -- rendered
    #: above the narrative so the overview opens as a scannable glance (the
    #: reskin's overview intent) rather than a prose dump. Empty on non-wave cards.
    glance: tuple[tuple[str, str], ...] = ()
    #: The dependency-path segments (the wave's deps, then the wave itself, then
    #: the waves it blocks) for the overview mini-DAG card. Empty when the wave
    #: has no deps and nothing depends on it.
    dep_segments: tuple[str, ...] = ()
    #: The wave status word rendered as the overview status pill; empty on a
    #: non-wave card (which carries no pill).
    status_pill: str = ""
    #: The resolved entity's lifecycle-status ENUM member (not its ``.value``
    #: word), carried so the overview ``status`` row + the wave status pill
    #: resolve their glyph through the canonical
    #: :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` resolver at render
    #: time. ``None`` only on the total fallback card (an unknown id), which
    #: carries no status row to sigil-prefix.
    status_enum: object | None = None


#: Honest-empty notice per always-present wave tab, rendered when the tab's
#: section has no data yet -- the seam stays visible but reads honestly as
#: not-yet-bound rather than a blank pane (the "render the tabs now, fill later"
#: reskin directive). The ``overview`` tab is never empty (every card has an
#: identity), so it carries no entry.
_EMPTY_TAB_NOTICE: dict[str, str] = {
    "criteria": "no criteria bound yet",
    "gates": "no gates declared yet",
    "evidence": "no evidence recorded yet",
    "metrics": "no metrics captured yet",
    "cost": "no metered sessions yet",
    "history": "no timeline events recorded",
}


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
    """Format a datetime-ish *value* compactly, or ``"—"`` when unset.

    Datetimes ride the shared compact-UTC formatter
    (:func:`~eawf.surfaces.render.units.format_compact_utc`) so operator-facing
    detail rows show ``YYYY-MM-DD HH:MM:SS`` — no microseconds, no ``+00:00``
    offset.

    Args:
        value: A ``datetime`` (or ``None``) read off a state model.

    Returns:
        The compact UTC string, or an em dash when *value* is ``None``.
    """
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return format_compact_utc(value)
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


def _session_duration_ms(session: SessionAttempt) -> int | None:
    """Return one :class:`SessionAttempt`'s wall-clock duration in milliseconds.

    The attempt's elapsed runtime is the span between its ``started_at`` and
    ``ended_at`` stamps. A session still running (``ended_at is None``) has no
    completed span, so it contributes nothing -- the caller folds the honest
    absence into the EU sentinel rather than counting it as a zero.

    Args:
        session: The runtime subprocess attempt.

    Returns:
        The whole-millisecond span, or ``None`` when the attempt has not
        ended (no completed runtime to measure).
    """
    if session.ended_at is None:
        return None
    return int((session.ended_at - session.started_at).total_seconds() * 1000.0)


def _wave_runtime_eu(wave: Wave) -> float | None:
    """Derive the wave's runtime effort-unit total from its session attempts.

    Sums each ended :class:`SessionAttempt`'s wall-clock span and routes the
    millisecond total through the canonical
    :func:`~eawf.observability.telemetry.join._duration_ms_to_eu` math (one EU
    per :data:`~eawf.observability.telemetry.join.DEFAULT_EU_MINUTES` minutes),
    so the metrics tab quotes the same EU unit the close-time telemetry
    rollup does. A wave with no ended sessions yields ``None`` so the caller
    surfaces the honest-absence sentinel rather than a measured zero.

    Args:
        wave: The resolved wave.

    Returns:
        The summed runtime effort units, or ``None`` when no session attempt
        has ended (no measured runtime to convert).
    """
    durations = [
        ms
        for ms in (_session_duration_ms(session) for session in wave.sessions.values())
        if ms is not None
    ]
    if not durations:
        return None
    return _duration_ms_to_eu(sum(durations), eu_minutes=DEFAULT_EU_MINUTES)


def _wave_metrics(wave: Wave, cost_rollup: WaveSessionRollup | None) -> tuple[tuple[str, str], ...]:
    """Build the ``metrics`` tab rows for a wave: estimate-vs-actual signals.

    Surfaces the wave's effort + spend against its plan estimate:

    - ``size`` is the plain effort bucket label (e.g. ``L``) when set.
    - ``eu`` is the ACTUAL runtime effort units derived from the wave's ended
      :class:`~eawf.kernel.state.models.SessionAttempt` spans -- the honest
      :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel when no
      session has ended.
    - ``estimate`` is the effort-bucket EU estimate
      (:data:`~eawf.workflow.estimation.buckets.BUCKET_EU`) the actual is
      measured against. It is a plan figure independent of runtime, so it
      shows whenever a bucket is set -- even before any session lands.
    - ``tokens`` is the ACTUAL consumed tokens summed off the wave's session
      rollup (the same rollup the ``cost`` tab prices), with the token budget
      appended when one is set. This replaces the former budget-vs-consumed
      bar over the unpopulated
      :attr:`~eawf.kernel.state.models.Wave.tokens_consumed` counter, which
      always read empty while the cost tab already showed real session tokens.

    The two actual rows stay honest about absence: no ended session leaves the
    EU actual the sentinel, and no metered rollup (or a rollup that summed to
    zero tokens) leaves the token row the sentinel -- distinguishable from a
    measured zero, and never a fabricated bar.

    Args:
        wave: The resolved wave.
        cost_rollup: The wave's joined session rollup, or ``None`` when no
            metered session joined -- the token row then reads the sentinel.

    Returns:
        Ordered metrics ``(label, value)`` rows.
    """
    rows: list[tuple[str, str]] = []
    if wave.effort_bucket is not None:
        rows.append(("size", wave.effort_bucket.value))
    runtime_eu = _wave_runtime_eu(wave)
    rows.append(
        (
            "eu",
            (
                "unavailable — no ended runtime attempt"
                if runtime_eu is None
                else f"{runtime_eu:.2f} EU"
            ),
        )
    )
    if wave.effort_bucket is not None:
        rows.append(("estimate", f"{BUCKET_EU[wave.effort_bucket]:.2f} EU"))
    rollup_consumed = (
        None
        if cost_rollup is None
        else cost_rollup.input_tokens
        + cost_rollup.output_tokens
        + cost_rollup.cache_read_tokens
        + cost_rollup.cache_write_tokens
    )
    observed_usage = [
        session
        for session in wave.sessions.values()
        if session.measurement_status is MeasurementStatus.USAGE_OBSERVED
    ]
    if rollup_consumed is not None and rollup_consumed > 0:
        consumed: int | None = rollup_consumed
    elif observed_usage:
        consumed = sum(
            (session.input_tokens or 0)
            + (session.output_tokens or 0)
            + (session.cache_creation_input_tokens or 0)
            + (session.cache_read_input_tokens or 0)
            for session in observed_usage
        )
    else:
        consumed = None
    if consumed is None:
        tokens_cell = f"unavailable — {_measurement_unavailable_reason(wave)}"
    elif wave.token_budget is not None:
        tokens_cell = f"{format_tokens(consumed)} / {format_tokens(wave.token_budget)} budget"
    else:
        tokens_cell = format_tokens(consumed)
    rows.append(("tokens", tokens_cell))
    measured_costs = [
        session.cost_usd for session in observed_usage if session.cost_usd is not None
    ]
    if cost_rollup is not None and cost_rollup.cost_usd > 0:
        cost_cell = f"${cost_rollup.cost_usd:.4f}"
    elif measured_costs:
        cost_cell = f"${sum(measured_costs):.4f}"
    else:
        cost_cell = f"unavailable — {_measurement_unavailable_reason(wave)}"
    rows.append(("cost", cost_cell))
    return tuple(rows)


def _measurement_unavailable_reason(wave: Wave) -> str:
    """Return latest persisted reason why usage evidence is unavailable."""
    sessions = sorted(wave.sessions.values(), key=lambda row: (row.started_at, row.attempt))
    for session in reversed(sessions):
        if session.measurement_reason is not None:
            return session.measurement_reason
    if sessions:
        return sessions[-1].measurement_status.value
    return "no runtime measurement recorded"


def _completion_metrics(closed: int, total: int) -> tuple[tuple[str, str], ...]:
    """Build the iter / phase ``metrics`` rows from child-wave counts.

    Args:
        closed: Count of closed child waves.
        total: Total child-wave count.

    Returns:
        Ordered metrics ``(label, value)`` rows: a real completion bar plus
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


def _gate_rows(gates: Iterable[GateSpec]) -> tuple[tuple[str, str], ...]:
    """Project a wave's typed gates into gates-tab ``(label, value)`` rows.

    The gates are grouped under their owning criterion: each distinct
    :attr:`~eawf.kernel.spec.common.GateSpec.criterion_id` emits one
    ``criterion`` header row carrying the criterion id, followed by a
    ``gate`` row per gate naming that gate's
    :attr:`~eawf.kernel.spec.common.GateSpec.kind`. The groups follow the
    criterion's first-seen order and gates keep their authored order within
    each group, so every gate sharing a criterion id renders under one
    header even when the input interleaves criteria.

    An empty *gates* yields ``()`` so the caller leaves
    :attr:`DetailCard.gates` empty and the modal builds no gates tab (a
    wave with no gates shows no gates section, never an empty one).

    Args:
        gates: The wave's typed gate rows.

    Returns:
        Ordered ``(label, value)`` rows for the gates tab, grouped under a
        ``criterion`` header per distinct criterion id; ``()`` when *gates*
        is empty.
    """
    grouped: dict[str, list[str]] = {}
    for gate in gates:
        grouped.setdefault(gate.criterion_id, []).append(gate.kind)
    rows: list[tuple[str, str]] = []
    for criterion_id, kinds in grouped.items():
        rows.append(("criterion", criterion_id))
        rows.extend(("gate", kind) for kind in kinds)
    return tuple(rows)


#: The store-kind string an auditor report row carries, matched against
#: :attr:`~eawf.workflow.agent_report.rollup.AgentReportRow.store_kind` so
#: the verdict section reads the auditor store only (not the executor /
#: reviewer / planner stores threaded through the same ``reports`` list).
_AUDITOR_STORE_KIND: str = StoreKind.AUDITOR_REPORT.value


def _latest_auditor_verdict(
    reports: Iterable[AgentReportRow],
) -> AgentReportRow | None:
    """Return the most-recent auditor-store report row, or ``None``.

    Scans *reports* (already sorted oldest-first by the caller) for rows
    whose :attr:`~eawf.workflow.agent_report.rollup.AgentReportRow.store_kind`
    is the auditor store, and returns the last one so the latest auditor
    pass wins. Returns ``None`` when no auditor row is present, so the
    caller renders the honest "no verdict recorded" line rather than a
    fabricated pass.

    Args:
        reports: The wave's loaded report rows (every role's store), in
            oldest-first order.

    Returns:
        The latest auditor-store report row, or ``None`` when the wave has
        no auditor report.
    """
    latest: AgentReportRow | None = None
    for row in reports:
        if row.store_kind == _AUDITOR_STORE_KIND:
            latest = row
    return latest


def _dispatch_history_rows(wave: Wave) -> tuple[tuple[str, str], ...]:
    """Build the evidence-tab provenance + dispatch-history rows for *wave*.

    Surfaces two honest provenance facts the evidence tab folds in:

    - A ``claimed`` row carrying the wave's :attr:`~eawf.kernel.state.models.Wave.claimed_at`
      work-start time, formatted via :func:`_fmt_dt`. An unclaimed wave
      (``claimed_at is None``) renders the em-dash sentinel, never a
      fabricated start time -- a wave that has not been claimed has no
      work-start fact to elapse from.
    - One row per :class:`~eawf.kernel.state.models.DispatchAnnotation` in
      :attr:`~eawf.kernel.state.models.Wave.dispatch_history`: the row value
      names the transition note plus its runtime, and when the annotation
      carries a runtime-switch reason that reason is appended so an operator
      sees why a runtime swap happened.

    Args:
        wave: The resolved wave.

    Returns:
        Ordered ``(label, value)`` rows: the ``claimed`` row first, then one
        ``attempt N`` row per dispatch annotation.
    """
    rows: list[tuple[str, str]] = [("claimed", _fmt_dt(wave.claimed_at))]
    for ann in wave.dispatch_history:
        runtime = ann.runtime_to or ann.runtime_from or "—"
        value = f"{ann.note.value} ({runtime})"
        if ann.reason is not None:
            value = f"{value} — {ann.reason}"
        rows.append((f"dispatch {ann.attempt}", value))
    return tuple(rows)


def _report_only_rows(
    reports: Iterable[AgentReportRow],
    observed_attempts: frozenset[int],
) -> tuple[tuple[str, str], ...]:
    """Render report-only evidence as one headed table.

    A typed report can exist without a matching runtime attempt. These rows
    carry report evidence, but no runtime-derived measurements. Keep those
    two facts in separate columns so the Evidence tab stays scannable and
    never implies that missing telemetry invalidates the report itself.
    """
    columns = ("report", "role", "verdict", "evidence", "metrics")
    report_rows: list[tuple[str, ...]] = []
    for report in reports:
        header = report.payload.header
        if header.attempt in observed_attempts:
            continue
        report_rows.append(
            (
                str(header.attempt),
                header.role.value,
                report.payload.body.verdict.value,
                "report-only",
                "metrics unavailable",
            )
        )
    if not report_rows:
        return ()
    widths = [len(column) for column in columns]
    for report_row in report_rows:
        widths = [max(width, len(value)) for width, value in zip(widths, report_row, strict=True)]
    table_header = f"[$accent][b]{escape(_format_report_table_row(columns, widths))}[/][/]"
    rendered = [table_header]
    rendered.extend(escape(_format_report_table_row(row, widths)) for row in report_rows)
    table = PreMarkedText("\n" + "\n".join(f"  {line}" for line in rendered))
    return (("reports", table),)


def _format_report_table_row(row: tuple[str, ...], widths: list[int]) -> str:
    """Format one report-only table row with padded columns."""
    cells = [value.ljust(width) for value, width in zip(row, widths, strict=True)]
    return "  ".join(cells)


def _close_attempt_rows(state: State, wave_id: str) -> tuple[tuple[str, str], ...]:
    """Render latest durable close-worker progress without changing wave status."""
    attempts = [row for row in state.close_attempts.values() if row.wave_id == wave_id]
    if not attempts:
        return ()
    latest = max(attempts, key=lambda row: (row.generation, row.requested_at, row.id))
    anchor = latest.started_at or latest.requested_at
    stop = latest.terminal_at or latest.updated_at
    elapsed = max(0.0, (stop - anchor).total_seconds())
    rows: list[tuple[str, str]] = [
        ("close stage", latest.status.value),
        ("close elapsed", _format_elapsed(elapsed)),
        (
            "close gates",
            f"{len(latest.gate_receipt_ids)}/{len(latest.required_gate_ids)} receipts",
        ),
    ]
    if latest.audit_requirement.value != "none":
        rows.append(
            (
                "close audit",
                latest.audit_report_id or f"{latest.audit_requirement.value} · pending",
            )
        )
    if latest.required_operator_actions:
        rows.append(
            (
                "close action required",
                " / ".join(action.value for action in latest.required_operator_actions),
            )
        )
    if latest.status is CloseAttemptStatus.STALE and latest.invalidation_causes:
        rows.append(("close stale", "; ".join(latest.invalidation_causes)))
    if latest.status is CloseAttemptStatus.CANCELLED:
        rows.append(("close cancel", latest.failure_kind or "cancelled"))
    elif latest.failure_kind is not None:
        rows.append(("close failure", latest.failure_kind))
    if latest.failure_detail_ref is not None:
        rows.append(("close diagnostic", latest.failure_detail_ref))
    return tuple(rows)


def _format_elapsed(seconds: float) -> str:
    """Return compact elapsed time for close-worker progress."""
    rounded = int(seconds)
    minutes, secs = divmod(rounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _auditor_verdict_rows(
    reports: Iterable[AgentReportRow],
) -> tuple[tuple[str, str], ...]:
    """Build the evidence-tab auditor verdict + reason rows.

    Surfaces the latest auditor report's
    :attr:`~eawf.kernel.state.enums.AgentReportVerdict` and its reason text
    (the report body's ``summary``). A wave with no auditor report row
    renders a single honest "no verdict recorded" line -- never a
    fabricated pass badge.

    Args:
        reports: The wave's loaded report rows (every role's store), in
            oldest-first order.

    Returns:
        Ordered ``(label, value)`` rows: a ``verdict`` row plus a
        ``reason`` row when an auditor report exists, otherwise a single
        ``verdict`` row carrying the honest no-verdict sentinel.
    """
    latest = _latest_auditor_verdict(reports)
    if latest is None:
        return (("verdict", "no verdict recorded"),)
    body = latest.payload.body
    return (
        ("verdict", body.verdict.value),
        ("reason", body.summary),
    )


def _wave_card(
    state: State,
    wave_id: str,
    *,
    reports: Iterable[AgentReportRow] = (),
    error_kind_by_attempt: Mapping[int, Iterable[str]] | None = None,
    cost_rollup: WaveSessionRollup | None = None,
) -> DetailCard | None:
    """Build a :class:`DetailCard` for the wave *wave_id*, or ``None``.

    Args:
        state: The bound state to resolve the wave from.
        wave_id: The selected wave id.
        reports: The wave's loaded agent-report rows (every role's store).
        error_kind_by_attempt: Per-attempt telemetry error kinds.
        cost_rollup: The wave's joined per-attempt cost rollup, or ``None``
            when no telemetry DB is reachable. ``None`` against a wave that
            still carries session attempts surfaces the honest "no metered
            sessions yet" cost line (the attempts exist but none priced).

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

    # The gates tab projects the wave's typed gate rows, grouped under
    # their owning criterion id and naming each gate's kind. A wave with no
    # gates leaves the group empty so the modal builds no gates tab (the
    # section header is absent, not an empty section).
    gates: list[tuple[str, str]] = list(_gate_rows(wave.gates))

    # The evidence tab folds in the auditor verdict, the attempt rollup,
    # and the dispatch history. The verdict + reason lead so the operator
    # sees the audit outcome first; a wave with no auditor report row shows
    # the honest "no verdict recorded" line rather than a fabricated pass.
    # ``reports`` is materialized first because it is scanned twice (the
    # verdict lookup plus the rollup) and may arrive as a one-shot iterable.
    report_rows = tuple(reports)
    attempt_rollup = per_wave_attempt_rollup(
        wave,
        reports=report_rows,
        error_kind_by_attempt=error_kind_by_attempt,
    )
    evidence: list[tuple[str, str]] = list(_auditor_verdict_rows(report_rows))
    observed_attempts = frozenset(wave.sessions)
    evidence.extend(attempt_rollup_rows(attempt_rollup, observed_attempts=observed_attempts))
    evidence.extend(_report_only_rows(report_rows, observed_attempts))
    # Provenance + dispatch history: the claimed-at work-start fact (em-dash
    # sentinel when unclaimed, never a fabricated start time) plus one row
    # per dispatch annotation, appending any runtime-switch reason so the
    # operator sees why a runtime swap happened.
    evidence.extend(_dispatch_history_rows(wave))
    evidence.extend(_close_attempt_rows(state, wave.id))

    # The cost tab joins each session attempt back to its priced telemetry
    # session and renders the per-attempt cost columns + an aggregate cost
    # bar. The group is built only when the wave carries session attempts;
    # a wave with attempts but no joined telemetry surfaces the honest "no
    # metered sessions yet" line rather than an empty cost tab.
    cost: tuple[tuple[str, str], ...] = wave_cost_rows(wave, cost_rollup)

    metrics_rows = _wave_metrics(wave, cost_rollup)
    glance, dep_segments = _wave_glance(
        state, wave, report_rows=report_rows, metrics_rows=metrics_rows
    )
    return DetailCard(
        title=f"wave {wave.id}",
        rows=tuple(rows),
        criteria=tuple(criteria),
        gates=tuple(gates),
        evidence=tuple(evidence),
        metrics=metrics_rows,
        cost=cost,
        detail_markdown=_wave_narrative_preview(state, wave, reports=report_rows),
        kind="wave",
        glance=glance,
        dep_segments=dep_segments,
        status_pill=wave.status.value,
        status_enum=wave.status,
    )


def _short_wave_id(wave_id: str) -> str:
    """Return the short tail of a wave id (``P30-I04-W06`` -> ``W06``)."""
    return wave_id.rsplit("-", 1)[-1]


def _wave_glance(
    state: State,
    wave: Wave,
    *,
    report_rows: tuple[AgentReportRow, ...],
    metrics_rows: tuple[tuple[str, str], ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Compute the overview glance quad + dependency-path segments for a wave.

    The glance is the scannable summary the reskin overview leads with: criteria
    bound-vs-total, gate count, evidence record count, and the metrics EU actual
    (the honest ``EMPTY_STATE`` sentinel when no runtime landed). A criterion
    counts as *bound* when it carries a gate or an oracle tier. The dependency
    path is the wave's deps, then the wave itself, then the waves whose deps name
    it (its blocks) -- empty when the wave is unconnected.

    Args:
        state: The bound state, scanned for the reverse-dep blocks.
        wave: The wave being resolved.
        report_rows: The wave's agent-report rows (the evidence count).
        metrics_rows: The already-resolved metrics rows (the EU value source).

    Returns:
        A ``(glance, dep_segments)`` pair; ``dep_segments`` is empty when the
        wave has no deps and nothing depends on it.
    """
    total = len(wave.success_criteria)
    bound = sum(1 for c in wave.success_criteria if c.gate_ids or c.oracle_tier is not None)
    eu_value = next((value for label, value in metrics_rows if label == "eu"), EMPTY_STATE)
    glance: tuple[tuple[str, str], ...] = (
        ("criteria", f"{bound}/{total} bound" if total else "none"),
        ("gates", str(len(wave.gates)) if wave.gates else "none"),
        ("evidence", f"{len(report_rows)} records" if report_rows else "none"),
        ("metrics", eu_value),
    )
    blocks = tuple(other.id for other in state.waves.values() if wave.id in other.deps)
    if not wave.deps and not blocks:
        return glance, ()
    segments = (
        *(_short_wave_id(dep) for dep in wave.deps),
        _short_wave_id(wave.id),
        *(_short_wave_id(block) for block in blocks),
    )
    return glance, segments


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
        metrics=_completion_metrics(closed, total),
        status_enum=it.status,
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
        metrics=_completion_metrics(closed, total),
        status_enum=phase.status,
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
    return DetailCard(title=f"backlog {item.id}", rows=tuple(rows), status_enum=item.status)


#: The one-line pointer the incident ``evidence`` group carries to its
#: dedicated ``history`` tab (designer ruling A3-a placed the chronological
#: timeline on a History tab; the evidence group keeps this link rather than
#: re-rendering the timeline rows inline). The ``h`` hotkey jumps straight to
#: the History tab.
_HISTORY_LINK_LINE: str = "see the History tab (h) for the chronological event timeline"


def _incident_card(
    state: State,
    incident_id: str,
    *,
    state_path: Path | None = None,
) -> DetailCard | None:
    """Build a :class:`DetailCard` for the incident *incident_id*, or ``None``.

    The overview group carries the incident metadata (severity / status /
    cause + the lifecycle open + close stamps). The chronological timeline of
    recorded events lives in the ``incident.jsonl`` store rather than on the
    state record, so the ``history`` group is populated from the store-loaded
    timeline when a ``state_path`` is given (designer ruling A3-a placed the
    timeline on its own History tab); the ``evidence`` group then carries a
    single one-line link pointing at it. An incident whose store has no
    recorded event renders the honest-empty line on the History tab, never a
    fabricated entry.

    Args:
        state: The bound state to resolve the incident from.
        incident_id: The selected incident id.
        state_path: When set, the chronological timeline is loaded from the
            local ``incident.jsonl`` store onto the History tab; ``None``
            leaves both the history and evidence groups empty so the modal
            builds neither tab.

    Returns:
        The card, or ``None`` when the id is not a known incident.
    """
    if state.incidents is None:
        return None
    incident = state.incidents.get(incident_id)
    if incident is None:
        return None
    rows: list[tuple[str, str]] = [
        ("id", incident.id),
        ("scope", incident.scope_id),
        ("title", incident.title),
        ("status", incident.status.value),
        ("severity", incident.severity.value),
        ("cause", incident.cause.value),
    ]
    if incident.root_cause is not None:
        rows.append(("root cause", incident.root_cause))
    rows.append(("opened", _fmt_dt(incident.opened_at)))
    rows.append(("closed", _fmt_dt(incident.closed_at)))
    # The chronological event timeline lives in the JSONL store, not the state
    # record; load it onto the dedicated History tab (designer ruling A3-a).
    # The evidence group keeps only a one-line link pointing at the History
    # tab rather than re-rendering the timeline rows inline. With no
    # ``state_path`` both groups stay empty (the modal builds neither tab).
    history: tuple[tuple[str, str], ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    if state_path is not None:
        history = incident_timeline_rows(load_incident_timeline(state_path, incident_id))
        evidence = (("timeline", _HISTORY_LINK_LINE),)
    return DetailCard(
        title=f"incident {incident.id}",
        rows=tuple(rows),
        evidence=evidence,
        history=history,
        status_enum=incident.status,
    )


def resolve_detail(
    state: State | None,
    selection_id: str,
    *,
    reports: Iterable[AgentReportRow] = (),
    error_kind_by_attempt: Mapping[int, Iterable[str]] | None = None,
    cost_rollup: WaveSessionRollup | None = None,
    state_path: Path | None = None,
) -> DetailCard:
    """Resolve *selection_id* to a :class:`DetailCard` from *state*.

    Tries the wave table, then iters, phases, the backlog, then incidents. An
    unresolvable id (or a ``None`` state) yields a fallback card naming the
    id so the operator sees *something* rather than a crash — the drill-in
    seam must stay total even when the state and the widget row briefly
    disagree (e.g. mid daemon-push).

    Args:
        state: The bound state, or ``None`` when no state is loaded.
        selection_id: The id carried by the selection message.
        reports: Pre-loaded agent-report rows; ignored for a wave id when a
            ``state_path`` is given (the store load wins).
        error_kind_by_attempt: Pre-loaded per-attempt error kinds; ignored
            for a wave id when a ``state_path`` is given.
        cost_rollup: Pre-joined per-attempt cost rollup; ignored for a wave
            id when a ``state_path`` is given (the store join wins).
        state_path: When set and the id is a wave, the store-backed report /
            error / cost rows are loaded from the local telemetry DB; when the
            id is an incident, its chronological timeline is loaded from the
            local ``incident.jsonl`` store.

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
            cost_rollup = wave_cost_rollup_for_wave(state, selection_id, state_path)
        card = (
            _wave_card(
                state,
                selection_id,
                reports=reports,
                error_kind_by_attempt=error_kind_by_attempt,
                cost_rollup=cost_rollup,
            )
            or _iter_card(state, selection_id)
            or _phase_card(state, selection_id)
            or _backlog_card(state, selection_id)
            or _incident_card(state, selection_id, state_path=state_path)
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
    (``o`` / ``c`` / ``g`` / ``v`` / ``m`` / ``$`` / ``h`` for overview /
    criteria / gates / evidence / metrics / cost / history), and the ``Esc``
    close binding. The arrow keys keep their native per-pane scroll behaviour
    — they are deliberately not bound here.
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
        border: round $accent;
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
    DetailModal .detail-pill {
        width: auto;
        height: 1;
        text-style: bold;
        margin-bottom: 1;
    }
    DetailModal .detail-dep-card {
        border: round $accent;
        padding: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    DetailModal .detail-glance {
        height: auto;
        color: $muted;
        margin-bottom: 1;
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
    #: single-letter keys jump straight to one of the chassis tabs (the
    #: ``$`` key jumps to the cost tab, the ``h`` key to the history tab).
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
        Binding("m", "show_tab('metrics')", "metrics", show=False),
        Binding("dollar_sign", "show_tab('cost')", "cost", show=False),
        Binding("h", "show_tab('history')", "history", show=False),
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

    #: Title prefixes whose cards re-resolve store-backed rows on mount: a
    #: wave reloads its report / error / cost rows, an incident its timeline.
    _ENRICH_PREFIXES: ClassVar[tuple[str, ...]] = ("wave ", "incident ")

    def _enrich_from_app(self) -> None:
        """Refresh store-backed wave / incident cards when the modal mounts."""
        if self._state is None:
            return
        prefix = next(
            (p for p in self._ENRICH_PREFIXES if self._card.title.startswith(p)),
            None,
        )
        if prefix is None:
            return
        try:
            state_path = getattr(self.app, "_state_path", None)
        except RuntimeError:
            return
        if not isinstance(state_path, Path):
            return
        entity_id = self._card.title.removeprefix(prefix).strip()
        if not entity_id:
            return
        self._card = resolve_detail(self._state, entity_id, state_path=state_path)
        self._tab_ids = self._present_tabs(self._card)

    @staticmethod
    def _present_tabs(card: DetailCard) -> tuple[str, ...]:
        """Return the ordered tab ids to build panes for *card*.

        The ``overview`` tab is ALWAYS present (every card carries an
        identity). A ``wave`` card additionally renders the full five-tab
        chassis -- ``criteria`` / ``gates`` / ``evidence`` / ``metrics`` are
        ALWAYS present, honest-empty until their data lands (the "render the
        tabs now, fill later" reskin directive), so a wave with no gates yet
        still shows the gates tab as an empty-but-present seam; the ``cost``
        tab stays data-gated (it is not one of the canonical five). Every
        non-wave card (phase / iter / backlog peek) keeps only the groups it
        populates, so an entity with no such section renders no empty tab.
        Order follows the chassis sequence.

        Args:
            card: The resolved card.

        Returns:
            The ordered tab ids to build panes for.
        """
        present: list[str] = ["overview"]
        if card.kind == "wave":
            # Wave detail renders the full five-tab chassis now: criteria /
            # gates / evidence / metrics are ALWAYS present, honest-empty until
            # their data lands ("render the tabs now, fill later"). The cost tab
            # stays data-gated -- it is not one of the canonical five.
            present.extend(["criteria", "gates", "evidence", "metrics"])
            if card.cost:
                present.append("cost")
            return tuple(present)
        # Non-wave cards (phase / iter / backlog / incident peeks) reuse only
        # the groups they populate, so an entity with no such section renders
        # no empty tab. The ``history`` tab carries an incident's chronological
        # event timeline (designer ruling A3-a); it is built only when the card
        # populates the group.
        if card.criteria:
            present.append("criteria")
        if card.gates:
            present.append("gates")
        if card.evidence:
            present.append("evidence")
        if card.metrics:
            present.append("metrics")
        if card.cost:
            present.append("cost")
        if card.history:
            present.append("history")
        return tuple(present)

    def _section_rows(self, tab_id: str) -> tuple[tuple[str, str], ...]:
        """Return the ``(label, value)`` rows for the *tab_id* section.

        Args:
            tab_id: One of the chassis tab ids (``overview`` / ``criteria``
                / ``gates`` / ``evidence`` / ``metrics`` / ``cost`` /
                ``history``).

        Returns:
            The matching section's rows.
        """
        if tab_id == "criteria":
            return self._card.criteria
        if tab_id == "gates":
            return self._card.gates
        if tab_id == "evidence":
            return self._card.evidence
        if tab_id == "metrics":
            return self._card.metrics
        if tab_id == "cost":
            return self._card.cost
        if tab_id == "history":
            return self._card.history
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

    def _compose_glance(self, *, mode: RenderMode) -> ComposeResult:
        """Yield the wave overview glance: status pill + dep-path card + quad.

        The reskin overview opens with a scannable glance above the narrative: a
        status pill, a dependency-path mini-DAG card (deps to this to blocks,
        the wave itself bold), and a counts quad (criteria bound / gates /
        evidence records / metrics EU).
        """
        card = self._card
        if card.status_pill:
            pill = (
                _status_with_sigil(card.status_enum, mode=mode)
                if card.status_enum is not None
                else card.status_pill
            )
            yield Static(
                f"[reverse $accent] {escape(pill)} [/]",
                classes="detail-pill",
            )
        if card.dep_segments:
            sep = f" {sigils.chrome('dispatch', mode=mode)} "
            this_id = _short_wave_id(card.title.split(" ", 1)[-1])
            path = sep.join(
                f"[b]{escape(seg)}[/b]" if seg == this_id else escape(seg)
                for seg in card.dep_segments
            )
            yield Static(
                f"[$accent]DEPENDENCY PATH[/]  [$muted]deps{sep}this{sep}blocks[/]\n{path}",
                classes="detail-dep-card",
            )
        if card.glance:
            line = "    ".join(
                f"[$muted]{escape(label)}[/] {escape(value)}" for label, value in card.glance
            )
            yield Static(line, classes="detail-glance")

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
            if self._card.kind == "wave":
                yield from self._compose_glance(mode=mode)
            yield Markdown(self._card.detail_markdown)
            return
        rows = self._section_rows(tab_id)
        if not rows:
            # An always-present wave tab whose data has not landed yet renders an
            # honest-empty notice rather than a blank pane (the "render the tabs
            # now, fill later" directive keeps the seam visible + honest).
            yield Static(
                f"[$muted]{_EMPTY_TAB_NOTICE.get(tab_id, 'nothing here yet')}[/]",
                classes="detail-row",
            )
            return
        label_width = max((len(label) for label, _ in rows), default=0)
        status_enum = self._card.status_enum
        for label, value in rows:
            padded = f"{label}:".ljust(label_width + 1)
            display = (
                _status_with_sigil(status_enum, mode=mode)
                if label == "status" and status_enum is not None
                else value
            )
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
        ``v`` / ``m`` / ``$`` / ``h`` for overview / criteria / gates /
        evidence / metrics / cost / history). A key for a tab the card does
        not carry (e.g. ``g`` on a gate-less wave, ``$`` on a wave with no
        session attempts, or ``h`` on a non-incident card) is silently
        ignored so the binding stays harmless on every card shape.

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
