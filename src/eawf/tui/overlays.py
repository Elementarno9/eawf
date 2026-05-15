"""Detail overlays for the Eä Rich TUI (P20-I01-W04).

This module owns the five entity-detail overlays operators trigger
from the wave-board (W03) or repo-scope quadrant (W02):

* ``hypothesis`` — :class:`~eawf.state.models.Hypothesis` detail.
* ``decision`` — :class:`~eawf.state.models.Decision` detail.
* ``memory`` — :class:`~eawf.state.models.MemorySummary` detail.
* ``events`` — :class:`~eawf.store.kinds.event.EventPayload` log overlay
  rendered against an in-memory list of events.
* ``dispatch`` — :class:`~eawf.state.models.Wave` dispatch-render
  overlay (DAG edges + budget + criteria) reusing the typed
  :func:`eawf.state.wave_graph.edges` accessor.

Each overlay is a one-frame :class:`rich.layout.Layout` carrying the
shared header chassis (``Eä`` brand outside-left of the breadcrumb;
see ``feedback_tui_branding``) and a Panel body rendering the typed
record. Overlays do NOT spin a :class:`rich.live.Live` loop — the
caller (wave-board or quadrant) is responsible for composing the
overlay into its tick.

Keymap (verb-prefixed scheme)
-----------------------------

Overlay shortcuts follow a verb-prefixed convention so the keymap
stays consistent as new overlays land:

    <verb><object>

The single verb is ``o`` (open). The objects map one capital letter
each to the five overlay kinds:

* ``oH`` — open Hypothesis overlay (``OVERLAY_KEY_HYPOTHESIS``).
* ``oD`` — open Decision overlay (``OVERLAY_KEY_DECISION``).
* ``oM`` — open Memory overlay (``OVERLAY_KEY_MEMORY``).
* ``oE`` — open Events overlay (``OVERLAY_KEY_EVENTS``).
* ``oR`` — open dispatch Render overlay (``OVERLAY_KEY_DISPATCH``).

Future overlay additions MUST extend the registry below and reuse the
``o<letter>`` prefix so the scheme stays one-shot for new readers.

Single dispatch
---------------

``open_overlay(overlay_kind, state, target_id)`` is the one entry the
caller uses. The function looks up the matching builder, validates the
target id against the relevant state mapping, and returns the
:class:`rich.console.RenderableType` (a :class:`Layout`).
"""

from __future__ import annotations

import logging
from typing import Literal

from rich.console import RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from eawf.state.models import (
    Decision,
    Hypothesis,
    MemorySummary,
    State,
    Wave,
)
from eawf.state.wave_graph import edges as wave_edges
from eawf.store.kinds.event import EventPayload
from eawf.tui.layout import build_brand_text, build_breadcrumb

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Overlay-kind enumeration + verb-prefixed keymap
# ---------------------------------------------------------------------------


#: Literal type for the public ``overlay_kind`` parameter. Adding a new
#: overlay = extend this Literal *and* the dispatch table at the bottom
#: of the module *and* the keymap registry below.
OverlayKind = Literal["hypothesis", "decision", "memory", "events", "dispatch"]


#: Verb-prefixed shortcut for the hypothesis overlay (``open Hypothesis``).
OVERLAY_KEY_HYPOTHESIS: str = "oH"

#: Verb-prefixed shortcut for the decision overlay (``open Decision``).
OVERLAY_KEY_DECISION: str = "oD"

#: Verb-prefixed shortcut for the memory overlay (``open Memory``).
OVERLAY_KEY_MEMORY: str = "oM"

#: Verb-prefixed shortcut for the events overlay (``open Events``).
OVERLAY_KEY_EVENTS: str = "oE"

#: Verb-prefixed shortcut for the dispatch-render overlay (``open Render``).
OVERLAY_KEY_DISPATCH: str = "oR"


#: Registry: shortcut -> overlay kind. Documented here so the caller
#: can wire keys into the wave-board / quadrant tick loop without
#: re-hardcoding the strings.
OVERLAY_KEYMAP: dict[str, OverlayKind] = {
    OVERLAY_KEY_HYPOTHESIS: "hypothesis",
    OVERLAY_KEY_DECISION: "decision",
    OVERLAY_KEY_MEMORY: "memory",
    OVERLAY_KEY_EVENTS: "events",
    OVERLAY_KEY_DISPATCH: "dispatch",
}


#: Tuple of known overlay kinds — useful for validation and tests.
KNOWN_OVERLAY_KINDS: tuple[OverlayKind, ...] = (
    "hypothesis",
    "decision",
    "memory",
    "events",
    "dispatch",
)


# ---------------------------------------------------------------------------
# Shared chassis: header + frame composition
# ---------------------------------------------------------------------------


def _build_overlay_header(state: State, *, overlay_title: str) -> Panel:
    """Header strip — brand + breadcrumb + overlay title suffix.

    Reuses :func:`eawf.tui.layout.build_brand_text` /
    :func:`eawf.tui.layout.build_breadcrumb` so the brand is byte-identical
    to the wave-board (W03) and quadrant (W02) headers.
    """
    breadcrumb = build_breadcrumb(state.model_dump(mode="json"))
    text = build_brand_text(breadcrumb)
    text.append(f"  | overlay: {overlay_title}", style="dim")
    return Panel(text, title=None, border_style="dim")


def _build_overlay_layout(state: State, *, overlay_title: str, body: Panel) -> Layout:
    """Compose header + body into a one-frame :class:`Layout`.

    The footer is intentionally omitted from the overlay: the caller
    (wave-board / quadrant) keeps its own footer visible behind the
    overlay so the operator's keymap context never disappears.
    """
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
    )
    layout["header"].update(_build_overlay_header(state, overlay_title=overlay_title))
    layout["body"].update(body)
    return layout


# ---------------------------------------------------------------------------
# Per-overlay builders
# ---------------------------------------------------------------------------


def build_hypothesis_overlay(hypothesis: Hypothesis) -> Panel:
    """Build the hypothesis-detail Panel.

    Args:
        hypothesis: Typed :class:`Hypothesis` record from state.

    Returns:
        :class:`Panel` titled ``hypothesis`` showing id, scope, status,
        verdict, the hypothesis text, and the confirm / reject
        thresholds.
    """
    lines: list[str] = [
        f"id:        {hypothesis.id}",
        f"scope:     {hypothesis.scope_id}",
        f"status:    {hypothesis.status.value}",
        f"verdict:   {hypothesis.verdict.value if hypothesis.verdict else '-'}",
        f"audit:     {hypothesis.audit_id or '-'}",
        f"source:    {hypothesis.source_artifact_id or '-'}",
        "",
        "text:",
        f"  {hypothesis.text}",
        "",
        f"metric:    {hypothesis.metric}",
        f"confirm:   {hypothesis.confirm}",
        f"reject:    {hypothesis.reject}",
    ]
    return Panel(Text("\n".join(lines)), title="hypothesis", border_style="cyan")


def build_decision_overlay(decision: Decision) -> Panel:
    """Build the decision-detail Panel.

    Args:
        decision: Typed :class:`Decision` record from state.

    Returns:
        :class:`Panel` titled ``decision`` showing id, scope, status,
        the summary + rationale prose, and any alternatives.
    """
    lines: list[str] = [
        f"id:            {decision.id}",
        f"scope:         {decision.scope_id}",
        f"status:        {decision.status.value}",
        f"superseded_by: {decision.superseded_by or '-'}",
        "",
        "summary:",
        f"  {decision.summary}",
        "",
        "rationale:",
        f"  {decision.rationale}",
        "",
        "alternatives:",
    ]
    if decision.alternatives:
        lines.extend(f"  - {alt}" for alt in decision.alternatives)
    else:
        lines.append("  -")
    return Panel(Text("\n".join(lines)), title="decision", border_style="cyan")


def build_memory_overlay(memory: MemorySummary) -> Panel:
    """Build the memory-detail Panel.

    Args:
        memory: Typed :class:`MemorySummary` record from state.

    Returns:
        :class:`Panel` titled ``memory`` showing id, scope, tier,
        confidence, status, review-due date, store record link, and
        any promoted artifact link.
    """
    review_due_str = memory.review_due.isoformat() if memory.review_due else "-"
    lines: list[str] = [
        f"id:            {memory.id}",
        f"scope:         {memory.scope_id}",
        f"tier:          {memory.tier.value}",
        f"confidence:    {memory.confidence.value}",
        f"status:        {memory.status.value}",
        f"review_due:    {review_due_str}",
        f"store_record:  {memory.store_record_id}",
        f"promoted:      {memory.promoted_to_artifact_id or '-'}",
        "",
        "summary:",
        f"  {memory.summary}",
    ]
    return Panel(Text("\n".join(lines)), title="memory", border_style="cyan")


def build_events_overlay(events: list[EventPayload]) -> Panel:
    """Build the events-log Panel.

    Renders newest-last so the operator's eye lands on the most recent
    activity at the bottom of the scroll. Each row carries the event
    type, actor, command, and status — enough for a quick "what just
    happened" scan without leaving the TUI.

    Args:
        events: List of :class:`EventPayload` records (already filtered
            to the scope the caller cares about).

    Returns:
        :class:`Panel` titled ``events`` showing the count followed by
        one row per event. Empty list renders a placeholder.
    """
    lines: list[str] = [f"events ({len(events)} shown):"]
    if not events:
        lines.append("  (no events to show)")
    else:
        for event in events:
            # ISO 8601 with the trailing ``+00:00`` trimmed; the events
            # overlay always renders UTC so the offset adds no signal.
            ts = event.timestamp.isoformat().replace("+00:00", "Z")
            lines.append(f"  [{ts}] {event.event_type:<11} {event.actor:<10} status={event.status}")
            lines.append(f"    cmd: {event.command}")
    return Panel(Text("\n".join(lines)), title="events", border_style="cyan")


def build_dispatch_overlay(wave: Wave, *, state: State) -> Panel:
    """Build the dispatch-render Panel for a wave.

    Reuses :func:`eawf.state.wave_graph.edges` so the DAG view here is
    byte-identical to the wave-board (W03) detail pane. The dispatch
    overlay is meant to mirror what the operator would see when they
    are about to claim / dispatch a wave — id, deps, blocked-by,
    budget, success criteria.

    Args:
        wave: Typed :class:`Wave` record to render.
        state: Validated :class:`State` document used to walk the
            typed DAG accessor.

    Returns:
        :class:`Panel` titled ``dispatch`` with the dispatch-ready
        summary block.
    """
    edge_view = wave_edges(wave.id, state)
    deps_str = ", ".join(edge_view.deps) if edge_view.deps else "-"
    blocks_str = ", ".join(edge_view.blocks) if edge_view.blocks else "-"
    blocked_by_str = ", ".join(edge_view.blocked_by) if edge_view.blocked_by else "-"
    if wave.token_budget is None:
        budget_str = f"- / - (consumed {wave.tokens_consumed})" if wave.tokens_consumed else "-"
    else:
        budget_str = f"{wave.tokens_consumed} / {wave.token_budget}"
    lines: list[str] = [
        f"wave:        {wave.id}",
        f"title:       {wave.title}",
        f"iter:        {wave.iter_id}",
        f"status:      {wave.status.value}",
        f"deps:        {deps_str}",
        f"blocks:      {blocks_str}",
        f"blocked_by:  {blocked_by_str}",
        f"budget:      {budget_str}",
        f"role:        {wave.agent_role.value if wave.agent_role else '-'}",
        f"effort:      {wave.effort_bucket.value if wave.effort_bucket else '-'}",
        "criteria:",
    ]
    if wave.success_criteria:
        lines.extend(f"  - {c}" for c in wave.success_criteria)
    else:
        lines.append("  -")
    return Panel(Text("\n".join(lines)), title="dispatch", border_style="cyan")


# ---------------------------------------------------------------------------
# Single dispatch entry
# ---------------------------------------------------------------------------


def _resolve_hypothesis(state: State, target_id: str) -> Hypothesis:
    """Look up a hypothesis by id; raise :class:`KeyError` on miss."""
    bucket = state.hypotheses or {}
    record = bucket.get(target_id)
    if record is None:
        raise KeyError(f"unknown hypothesis: {target_id!r}")
    return record


def _resolve_decision(state: State, target_id: str) -> Decision:
    """Look up a decision by id; raise :class:`KeyError` on miss."""
    bucket = state.decisions or {}
    record = bucket.get(target_id)
    if record is None:
        raise KeyError(f"unknown decision: {target_id!r}")
    return record


def _resolve_memory(state: State, target_id: str) -> MemorySummary:
    """Look up a memory summary by id; raise :class:`KeyError` on miss."""
    bucket = state.memory_index or {}
    record = bucket.get(target_id)
    if record is None:
        raise KeyError(f"unknown memory: {target_id!r}")
    return record


def _resolve_wave(state: State, target_id: str) -> Wave:
    """Look up a wave by id; raise :class:`KeyError` on miss."""
    record = state.waves.get(target_id)
    if record is None:
        raise KeyError(f"unknown wave: {target_id!r}")
    return record


def open_overlay(
    overlay_kind: OverlayKind,
    state: State,
    target_id: str,
    *,
    events: list[EventPayload] | None = None,
) -> RenderableType:
    """Open the overlay matching *overlay_kind* for *target_id*.

    This is the one-shot dispatch entry the wave-board and quadrant
    call into. It resolves the target id against the relevant state
    mapping, builds the per-overlay :class:`Panel`, and wraps it in a
    header-bearing :class:`Layout`.

    The ``events`` overlay is special: events live in the on-disk
    event-store, not in :class:`State`. Callers pass the already-loaded
    event list via the optional *events* keyword; *target_id* is then
    treated as a free-form filter label (e.g. ``"recent"`` /
    ``"P20-I01-W04"``) that appears in the overlay title for context.

    Args:
        overlay_kind: One of :data:`KNOWN_OVERLAY_KINDS`.
        state: Validated :class:`State` document.
        target_id: Record id (or filter label for ``events``).
        events: Optional list of events for the ``events`` overlay;
            ignored for every other kind.

    Returns:
        :class:`rich.console.RenderableType` (a :class:`Layout`) ready
        to be composed into the parent surface.

    Raises:
        ValueError: when *overlay_kind* is not a known overlay.
        TypeError: when *target_id* is not a string.
        KeyError: when *target_id* is not present in the relevant
            state mapping (hypothesis / decision / memory / dispatch).
    """
    if not isinstance(target_id, str):
        raise TypeError(f"target_id must be str, got {type(target_id).__name__}")
    if overlay_kind not in KNOWN_OVERLAY_KINDS:
        raise ValueError(f"unknown overlay_kind: {overlay_kind!r}")
    logger.info(f"open_overlay overlay_kind={overlay_kind} target_id={target_id!r}")
    if overlay_kind == "hypothesis":
        record = _resolve_hypothesis(state, target_id)
        body = build_hypothesis_overlay(record)
        title = f"hypothesis {record.id}"
    elif overlay_kind == "decision":
        decision = _resolve_decision(state, target_id)
        body = build_decision_overlay(decision)
        title = f"decision {decision.id}"
    elif overlay_kind == "memory":
        memory = _resolve_memory(state, target_id)
        body = build_memory_overlay(memory)
        title = f"memory {memory.id}"
    elif overlay_kind == "events":
        body = build_events_overlay(events or [])
        title = f"events {target_id}" if target_id else "events"
    else:  # overlay_kind == "dispatch"
        wave = _resolve_wave(state, target_id)
        body = build_dispatch_overlay(wave, state=state)
        title = f"dispatch {wave.id}"
    return _build_overlay_layout(state, overlay_title=title, body=body)


__all__ = [
    "KNOWN_OVERLAY_KINDS",
    "OVERLAY_KEYMAP",
    "OVERLAY_KEY_DECISION",
    "OVERLAY_KEY_DISPATCH",
    "OVERLAY_KEY_EVENTS",
    "OVERLAY_KEY_HYPOTHESIS",
    "OVERLAY_KEY_MEMORY",
    "OverlayKind",
    "build_decision_overlay",
    "build_dispatch_overlay",
    "build_events_overlay",
    "build_hypothesis_overlay",
    "build_memory_overlay",
    "open_overlay",
]
