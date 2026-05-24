"""``StatusPane`` — current-scope status summary widget (widget catalog).

A :class:`~textual.widgets.Static` composite that surfaces the current
scope's lifecycle counters — project / phase / iter / wave counts, audit
count, open worktrees, and blocked (failed) waves — alongside an EFFORT
block (consumed/estimate EU, signed variance %, an EU/day velocity
sparkline, and an ETA) and a GATES block (live ``audit_check_*`` N/M
progress collapsing to the verdict). Rendered as a live pane that watches
the reactive :class:`~eawf.kernel.state.models.State`.

The pane groups its rendered lines into four labelled sections —
LIFECYCLE / EFFORT / GATES / DISPATCH — each built by a dedicated pure
function so the line set stays unit-testable without mounting the widget.
The DISPATCH band (NOW / NEXT / WAIT) is the live dispatch frontier: NOW
lists the active waves (pulsing dot + agent role + token-burn bar), NEXT
the next ready batch (capped to ``planning.max_parallel_waves``), and
WAIT the PENDING waves blocked by a running wave (with ``← dep`` edge
labels). Its frontier derives from :func:`build_dispatch_slice` (pure)
and its running dot pulses on a ``TICK_PULSE`` (~2 Hz) timer reusing the
heartbeat glyph fade.

The pane is driven by the host :class:`~eawf.tui.app.EaApp` reactive
``state``: on mount it seeds from ``app.state`` and registers a watcher so
daemon-pushed revisions repaint it; standalone tests assign :attr:`state`
directly. Bar glyphs honour :attr:`eawf.tui.app.EaApp.render_mode` so a
single Braille ↔ ASCII flip rerenders the pane.

Counter and series derivation live in pure functions
(:func:`summary_counts`, :func:`build_velocity_eu_per_day`) so the numbers
are unit-testable without mounting the widget. Colours, where used,
resolve against the ``theme.tcss`` palette vars — never hardcoded hex.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict
from textual.reactive import reactive
from textual.widgets import Static

from eawf.estimation.buckets import wave_estimate_eu
from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.state.enums import (
    AuditStatus,
    IterStatus,
    PhaseStatus,
    WaveStatus,
    WorktreeStatus,
)
from eawf.kernel.state.wave_graph import blocked_by
from eawf.tui.widgets.eu_bar import (
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    RenderMode,
    render_completion_bar,
    render_eu_bar_plain,
)
from eawf.tui.widgets.heartbeat import PULSE_INTERVAL_S, pulse_glyph
from eawf.tui.widgets.markup import escape_markup, style_labeled_line
from eawf.tui.widgets.variance_tile import render_variance_plain

if TYPE_CHECKING:
    from eawf.kernel.state.models import Audit, State, Wave

logger = logging.getLogger(__name__)

#: Placeholder shown when a pointer (phase / iter) is unset.
DASH: str = "—"

#: The four section header literals, recognised by ``_repaint`` so each
#: gets the bold-accent span whether it lands in a single- or two-column
#: cell.
_SECTION_HEADERS: frozenset[str] = frozenset({"LIFECYCLE", "EFFORT", "GATES", "DISPATCH"})

#: The DISPATCH band's sub-labels (the leading token of a NOW / NEXT /
#: WAIT row). Recognised by ``_style_cell`` so the band label gets the
#: accent tint the ``label:`` metric rows carry, while its wave-id body
#: stays plain.
_DISPATCH_BANDS: frozenset[str] = frozenset({"NOW", "NEXT", "WAIT"})

#: Project-code fallback when no project record is loaded.
DEFAULT_PROJECT_CODE: str = "EAWF"

#: Default trailing window (days) summed by :func:`build_velocity_eu_per_day`.
VELOCITY_WINDOW_DAYS: int = 7

#: Spark glyphs for the velocity sparkline, low-to-high. Index 0 renders a
#: zero-EU day; the top index renders the busiest day in the window.
_SPARK_GLYPHS: tuple[str, ...] = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")

#: ASCII fallback spark glyphs (``ui.glyphs=ascii`` / no Braille coverage).
_SPARK_ASCII: tuple[str, ...] = (".", ":", "-", "=", "+", "*", "#", "@")

#: Per-check glyphs for the GATES N/M progress, by reported state.
GATE_PASS: str = "✓"
GATE_FAIL: str = "✗"
GATE_RUNNING: str = "⏳"
GATE_PENDING: str = "—"

#: ASCII fallbacks for the GATES per-check glyphs.
_GATE_ASCII: dict[str, str] = {
    GATE_PASS: "P",
    GATE_FAIL: "x",
    GATE_RUNNING: "~",
    GATE_PENDING: "-",
}


def _active_phase_id(state: State) -> str | None:
    """Resolve the id of the phase whose waves the pane should count.

    Prefers the ``current.phase_id`` pointer (the operator's focused
    phase) but only when the pointed-to phase is itself ACTIVE — a stale
    pointer at a closed/archived phase would otherwise mis-scope the live
    counts, so it falls through to the scan below. Failing that, returns
    the single phase whose ``status`` is ACTIVE so a state with an active
    phase but an unset pointer still scopes correctly. Returns ``None``
    when no phase is active — the wave counters then read zero rather than
    counting archived/closed-phase leftovers.
    """
    pointer = state.current.phase_id
    if (
        pointer is not None
        and pointer in state.phases
        and state.phases[pointer].status is PhaseStatus.ACTIVE
    ):
        return pointer
    for phase_id, phase in state.phases.items():
        if phase.status is PhaseStatus.ACTIVE:
            return phase_id
    return None


def _active_iter_id(state: State) -> str | None:
    """Resolve the id of the iter whose effort/gates the pane should read.

    Prefers the ``current.iter_id`` pointer when it belongs to the active
    phase; otherwise returns the single ACTIVE iter under that phase.
    Returns ``None`` when no iter is in scope (the EFFORT / GATES blocks
    then render their empty-state sentinels).
    """
    active_phase_id = _active_phase_id(state)
    if active_phase_id is None:
        return None
    pointer = state.current.iter_id
    if (
        pointer is not None
        and pointer in state.iters
        and state.iters[pointer].phase_id == active_phase_id
    ):
        return pointer
    for iter_id, it in state.iters.items():
        if it.phase_id == active_phase_id and it.status is IterStatus.ACTIVE:
            return iter_id
    return None


#: Counter keys surfaced by :func:`summary_counts`, in render order.
_COUNT_KEYS: tuple[str, ...] = (
    "phases_active",
    "iters_active",
    "waves_pending",
    "waves_in_progress",
    "waves_closed",
    "waves_total",
    "waves_failed",
    "audits_running",
    "audits_total",
    "worktrees_active",
)


def _count_status(items: Iterable[Any], status: object) -> int:
    """Return the number of *items* whose ``.status`` is *status* (identity match)."""
    return sum(1 for item in items if item.status is status)


def summary_counts(state: State | None) -> dict[str, int]:
    """Tally the lifecycle counters the status pane surfaces.

    All keys are present and zero for a ``None`` / empty state so the pane
    renders a deterministic frame before any roadmap activity.

    The wave counters (``waves_pending`` / ``waves_in_progress`` /
    ``waves_failed``) are scoped to the **active phase** (resolved via
    :func:`_active_phase_id`): only waves whose iter belongs to that
    phase are tallied. Archived/closed-phase waves left in a non-terminal
    status (e.g. zombie PENDING rows under a dropped phase) therefore do
    not inflate the live counts.

    Args:
        state: The bound state, or ``None``.

    Returns:
        A dict with keys ``phases_active`` / ``iters_active`` /
        ``waves_pending`` / ``waves_in_progress`` / ``waves_closed`` /
        ``waves_total`` / ``waves_failed`` / ``audits_running`` /
        ``audits_total`` / ``worktrees_active``.
    """
    if state is None:
        return dict.fromkeys(_COUNT_KEYS, 0)
    active_phase_id = _active_phase_id(state)
    active_iter_ids = {iid for iid, it in state.iters.items() if it.phase_id == active_phase_id}
    scoped_waves = [w for w in state.waves.values() if w.iter_id in active_iter_ids]
    audits = (state.audits or {}).values()
    worktrees = (state.worktrees or {}).values()
    return {
        "phases_active": _count_status(state.phases.values(), PhaseStatus.ACTIVE),
        "iters_active": _count_status(state.iters.values(), IterStatus.ACTIVE),
        "waves_pending": _count_status(scoped_waves, WaveStatus.PENDING),
        "waves_in_progress": _count_status(scoped_waves, WaveStatus.IN_PROGRESS),
        "waves_closed": _count_status(scoped_waves, WaveStatus.CLOSED),
        "waves_total": len(scoped_waves),
        "waves_failed": _count_status(scoped_waves, WaveStatus.FAILED),
        "audits_running": _count_status(audits, AuditStatus.RUNNING),
        "audits_total": len(state.audits or {}),
        "worktrees_active": _count_status(worktrees, WorktreeStatus.ACTIVE),
    }


def _active_phase_waves(state: State) -> list[Wave]:
    """Return the waves under the active phase's iters (any status).

    Resolves the active phase (:func:`_active_phase_id`), the iter ids that
    belong to it, and every wave whose ``iter_id`` lands in that set. The
    list is the live denominator population for the EFFORT block: it counts
    PENDING / PLANNED waves too, so adding a wave grows the bucket-aggregate
    estimate even before the wave is claimed.

    Args:
        state: The bound state.

    Returns:
        The active phase's waves, or an empty list when no phase is active.
    """
    phase_id = _active_phase_id(state)
    if phase_id is None:
        return []
    iter_ids = {iid for iid, it in state.iters.items() if it.phase_id == phase_id}
    return [w for w in state.waves.values() if w.iter_id in iter_ids]


def _scoped_scope_ids(state: State) -> set[str]:
    """Return the scope ids (active phase's iters + their waves) effort attaches to.

    EU actuals are keyed by ``scope_id`` — either an iter id or one of its
    wave ids. The set lets :func:`_effort_eu` and
    :func:`build_velocity_eu_per_day` pick only the rows belonging to the
    active phase's subtree, never a sibling phase's leftovers. Scoping to
    the phase (not a single iter) matches the LIFECYCLE block so the EFFORT,
    velocity, and ETA signals span every iter of the live phase.
    """
    phase_id = _active_phase_id(state)
    if phase_id is None:
        return set()
    iter_ids = {iid for iid, it in state.iters.items() if it.phase_id == phase_id}
    wave_ids = {wid for wid, w in state.waves.items() if w.iter_id in iter_ids}
    return iter_ids | wave_ids


def _effort_eu(state: State | None) -> tuple[float, float]:
    """Return the active **phase**'s ``(consumed_eu, estimate_eu)`` pair.

    The numerator sums :attr:`~eawf.kernel.state.models.ActualSummary.elapsed_eu`
    over the actuals whose ``scope_id`` is one of the active phase's waves.
    The denominator is a **live** bucket-aggregate:
    ``Σ wave_estimate_eu(w)`` over every active-phase wave regardless of
    status, so PENDING / PLANNED waves count and the estimate grows the
    moment a wave is added — it does not wait for the claim-time estimate
    bucket to be seeded. A wave with no ``effort_bucket`` contributes ``0``
    (:func:`~eawf.estimation.buckets.wave_estimate_eu` returns ``0`` when
    the bucket is unset), so an all-unbucketed phase yields a ``0.0``
    estimate; the caller treats a non-positive estimate as the empty state.

    Args:
        state: The bound state, or ``None``.

    Returns:
        ``(consumed_eu, estimate_eu)``.
    """
    if state is None:
        return 0.0, 0.0
    phase_waves = _active_phase_waves(state)
    if not phase_waves:
        return 0.0, 0.0
    phase_wave_ids = {w.id for w in phase_waves}
    consumed = sum(
        a.elapsed_eu for a in (state.actuals or {}).values() if a.scope_id in phase_wave_ids
    )
    estimate = sum(wave_estimate_eu(w) for w in phase_waves)
    return consumed, estimate


def _variance_pct(consumed_eu: float, estimate_eu: float) -> float | None:
    """Return the signed ``(consumed - estimate) / estimate * 100`` variance.

    Returns ``None`` when *estimate_eu* is non-positive (no baseline to
    measure against — the EFFORT block then shows the empty state).

    Args:
        consumed_eu: Effort units consumed so far.
        estimate_eu: Total estimated effort units.

    Returns:
        The signed variance percentage, or ``None`` when unmeasurable.
    """
    if estimate_eu <= 0:
        return None
    return (consumed_eu - estimate_eu) / estimate_eu * 100.0


def build_velocity_eu_per_day(
    state: State | None, *, days: int = VELOCITY_WINDOW_DAYS
) -> list[float]:
    """Return the per-day EU burn over the trailing *days*-day window.

    Buckets the active phase's actuals by their ``updated_at`` calendar day
    and returns one EU sum per day, oldest-first, for exactly *days*
    entries ending today (UTC). A day with no actual contributes ``0.0``.
    The window anchor is the most recent ``updated_at`` among the in-scope
    actuals (so a fixture dated in the past still lands inside its own
    window) — falling back to today when none is present.

    The series is the EU/day signal the EFFORT sparkline renders;
    returning a fixed-length list keeps the sparkline a deterministic
    *days*-glyph run regardless of how sparse the actuals are.

    Args:
        state: The bound state, or ``None``.
        days: Window length in days (must be ``>= 1``).

    Returns:
        A list of *days* EU sums, oldest-first. All-zero when no in-scope
        actual carries a positive ``elapsed_eu``.

    Raises:
        ValueError: When *days* is less than 1.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days!r}")
    if state is None:
        return [0.0] * days
    scope_ids = _scoped_scope_ids(state)
    in_scope = [a for a in (state.actuals or {}).values() if a.scope_id in scope_ids]
    if not in_scope:
        return [0.0] * days
    per_day: defaultdict[date, float] = defaultdict(float)
    for actual in in_scope:
        per_day[actual.updated_at.date()] += actual.elapsed_eu
    anchor = max(per_day)
    start = anchor - timedelta(days=days - 1)
    return [per_day.get(start + timedelta(days=offset), 0.0) for offset in range(days)]


def _render_sparkline(series: list[float], *, mode: RenderMode) -> str:
    """Render a per-day EU *series* as a sparkline, or the empty state.

    Each value maps onto a spark glyph scaled against the window's peak so
    the busiest day reaches the top glyph and zero days render the floor
    glyph. An all-zero series (no burn in the window) renders
    :data:`EMPTY_STATE` rather than a flat fake bar.

    Args:
        series: The per-day EU sums, oldest-first.
        mode: Active render mode (``"braille"`` or ``"ascii"``); selects
            the Unicode block-element glyphs vs the ASCII fallback.

    Returns:
        The sparkline glyph run, or :data:`EMPTY_STATE` when the series
        carries no positive burn.
    """
    peak = max(series, default=0.0)
    if peak <= 0:
        return EMPTY_STATE
    glyphs = _SPARK_ASCII if mode == "ascii" else _SPARK_GLYPHS
    top = len(glyphs) - 1
    cells = [glyphs[min(top, int(value / peak * top + 0.5))] for value in series]
    return "".join(cells)


def _eta_line(consumed_eu: float, estimate_eu: float, series: list[float]) -> str:
    """Return the projected finish date from the current burn rate.

    Projects the remaining EU (``estimate - consumed``) against the mean
    per-day burn over the window's non-zero days. Returns :data:`DASH`
    when there is no remaining work, no estimate, or no burn to project
    against.

    Args:
        consumed_eu: Effort units consumed so far.
        estimate_eu: Total estimated effort units.
        series: The per-day EU sums driving the burn-rate estimate.

    Returns:
        An ISO ``YYYY-MM-DD`` date string, or :data:`DASH` when the finish
        date is unprojectable.
    """
    remaining = estimate_eu - consumed_eu
    if estimate_eu <= 0 or remaining <= 0:
        return DASH
    active_days = [value for value in series if value > 0]
    if not active_days:
        return DASH
    rate = sum(active_days) / len(active_days)
    if rate <= 0:
        return DASH
    days_left = int(remaining / rate + 0.5)
    return (date.today() + timedelta(days=days_left)).isoformat()


def _lifecycle_lines(state: State | None, *, mode: RenderMode) -> list[str]:
    """Build the LIFECYCLE section lines (the lifecycle counter block).

    Mirrors the ``eawf status`` summary: project / phase / iter pointers,
    the active-phase wave counters, a completion bar, the audit / worktree
    counters, then a blocked line when any wave has failed.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``); selects
            the completion bar's glyph set.

    Returns:
        The ordered LIFECYCLE lines (no section header).
    """
    counts = summary_counts(state)
    project = DEFAULT_PROJECT_CODE
    phase = DASH
    iter_id = DASH
    if state is not None:
        if state.project is not None:
            project = state.project.code
        phase = state.current.phase_id or DASH
        iter_id = state.current.iter_id or DASH
    progress = render_completion_bar(counts["waves_closed"], counts["waves_total"], mode=mode)
    lines = [
        f"project:   {project}",
        f"phase:     {phase}",
        f"iter:      {iter_id}",
        f"waves:     {counts['waves_in_progress']} active · {counts['waves_pending']} pending",
        f"progress:  {progress}",
        f"audits:    {counts['audits_running']} running · {counts['audits_total']} total",
        f"worktrees: {counts['worktrees_active']} active",
    ]
    blocked = counts["waves_failed"]
    if blocked:
        lines.append(f"blocked:   {blocked} failed wave(s)")
    return lines


def _effort_lines(state: State | None, *, mode: RenderMode) -> list[str]:
    """Build the EFFORT section lines (EU burn, variance, velocity, ETA).

    Shows consumed/estimate EU as a bar, the signed variance %, an EU/day
    velocity sparkline, and an ETA from the current burn. Every metric
    falls back to its empty-state sentinel (``— no data`` / ``—``) when no
    estimate or actual is in scope — never a fabricated 0 % bar.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        The ordered EFFORT lines (no section header).
    """
    consumed, estimate = _effort_eu(state)
    effort = render_eu_bar_plain(consumed, estimate, mode=mode)
    if estimate > 0:
        effort = f"{consumed:.1f}/{estimate:.1f}  {effort}"
    variance = render_variance_plain(_variance_pct(consumed, estimate))
    series = build_velocity_eu_per_day(state)
    velocity = _render_sparkline(series, mode=mode)
    eta = _eta_line(consumed, estimate, series)
    return [
        f"effort:    {effort}",
        f"variance:  {variance}",
        f"velocity:  {velocity}",
        f"eta:       {eta}",
    ]


def _gate_glyph(passed: object, *, mode: RenderMode) -> str:
    """Return the GATES per-check glyph for a check's ``passed`` value.

    ``True`` → pass, ``False`` → fail, ``None`` → still running; anything
    else (a missing key on a stray payload) → pending. ASCII mode swaps
    the Unicode glyphs for their plain fallbacks.

    Args:
        passed: The check's ``passed`` flag (``True`` / ``False`` /
            ``None``).
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        The single-character glyph for the check's reported state.
    """
    if passed is True:
        glyph = GATE_PASS
    elif passed is False:
        glyph = GATE_FAIL
    elif passed is None:
        glyph = GATE_RUNNING
    else:
        glyph = GATE_PENDING
    return _GATE_ASCII[glyph] if mode == "ascii" else glyph


def _check_passed(check: object) -> object:
    """Extract a check's ``passed`` flag from a dict or attribute payload.

    ``check_results`` is typed ``list[Any]``; rows arrive as dicts
    (``{"name", "passed", "details"}``) or typed objects. Returns the
    ``passed`` value, or ``None`` when it is absent (treated as running).
    """
    if isinstance(check, dict):
        return check.get("passed")
    return getattr(check, "passed", None)


def _gate_lines(state: State | None, *, mode: RenderMode) -> list[str]:
    """Build the GATES section lines (live audit N/M progress or verdict).

    Reads the active iter's audit (via ``iter.audit_id``): while it runs,
    the ``gate:`` line shows ``N/M`` reported checks with one per-check
    glyph each (``✓ ✗ ⏳ —``); once a verdict lands it collapses to
    ``A<id> <verdict>``. With no audit in scope the block renders the
    empty state.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        The ordered GATES lines (no section header).
    """
    audit = _active_audit(state)
    if audit is None:
        return [f"gate:      {EMPTY_STATE}"]
    if audit.verdict is not None:
        return [f"gate:      {audit.id} {audit.verdict.value}"]
    checks = list(audit.check_results)
    total = len(checks)
    done = sum(1 for c in checks if _check_passed(c) in (True, False))
    glyphs = " ".join(_gate_glyph(_check_passed(c), mode=mode) for c in checks)
    tally = f"{done}/{total}" if total else EMPTY_STATE
    suffix = f"  {glyphs}" if glyphs else ""
    return [f"gate:      {tally}{suffix}"]


def _active_audit(state: State | None) -> Audit | None:
    """Return the audit attached to the active iter, or ``None``.

    Resolves ``iter.audit_id`` for the active iter (:func:`_active_iter_id`)
    against ``state.audits``. Returns ``None`` when no iter is in scope,
    the iter carries no audit, or the audit id is dangling.
    """
    if state is None:
        return None
    iter_id = _active_iter_id(state)
    if iter_id is None:
        return None
    audit_id = state.iters[iter_id].audit_id
    if audit_id is None:
        return None
    return (state.audits or {}).get(audit_id)


#: Default NEXT-batch cap when no layered config overrides it — sourced
#: from the built-in ``planning.max_parallel_waves`` so the band never
#: hardcodes the window width.
DEFAULT_MAX_PARALLEL_WAVES: int = BUILT_IN_DEFAULTS["planning"]["max_parallel_waves"]

#: Empty-state line for the DISPATCH band's NOW section.
DISPATCH_IDLE: str = "idle (no active waves)"


class DispatchSlice(BaseModel):
    """Typed live dispatch frontier: what runs NOW / dispatches NEXT / WAITs.

    The pure render source for the status pane's DISPATCH band. ``now`` is
    the active-wave pointer set (CLAIMED + IN_PROGRESS); ``next`` is the
    ready-to-claim batch (deps CLOSED), ordered by ``W##`` and capped to
    ``planning.max_parallel_waves``; ``wait`` pairs each PENDING wave that
    is blocked by a currently-active wave with its first blocker id.

    ``next_overflow`` is the count of ready waves beyond the cap (the
    band's ``+N more`` suffix); it is ``0`` when every ready wave fits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    now: tuple[str, ...]
    next: tuple[str, ...]
    wait: tuple[tuple[str, str | None], ...]
    next_overflow: int


def _wave_index(wave_id: str) -> int | None:
    """Return a wave id's trailing ``W##`` integer, or ``None``.

    Mirrors the suffix parse :func:`eawf.lifecycle.wave._lower_w_sibling_pending`
    uses for the monotonic claim gate, re-derived here so the band stays a
    pure read with no dependency on a private lifecycle internal. The index
    is the NEXT batch's monotonic sort key: the ready frontier is ordered
    lowest-``W##``-first so the cap selects the waves the operator dispatches
    first, matching the lower-sibling claim order.

    Args:
        wave_id: Wave id (e.g. ``"P01-I01-W03"``).

    Returns:
        The integer after the trailing ``W``, or ``None`` when the id has
        no ``W##`` suffix (a malformed or non-wave id).
    """
    suffix = wave_id.split("-")[-1]
    if suffix.startswith("W") and suffix[1:].isdigit():
        return int(suffix[1:])
    return None


def build_dispatch_slice(
    state: State | None,
    *,
    scope: str | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL_WAVES,
) -> DispatchSlice:
    """Compute the live NOW / NEXT / WAIT dispatch frontier from *state*.

    Pure — unit-testable without mounting the widget. Derivation per the
    tui-richer-views design §8.2:

    * **NOW** — ``state.current.active_wave_ids`` (the CLAIMED +
      IN_PROGRESS pointer set), in pointer order.
    * **NEXT** — PENDING waves whose live :func:`blocked_by` view is empty
      (all deps CLOSED): the ready frontier. Ordered by ``W##`` (the
      monotonic claim order — lowest dispatches first) and truncated to
      *max_parallel*; the whole ready frontier is parallel-dispatchable
      via ``--out-of-order``, so the cap selects the front of the order
      rather than dropping higher siblings. Ready waves beyond the cap
      drive ``next_overflow`` (the ``+N more`` suffix).
    * **WAIT** — PENDING waves whose live blocked-by set intersects NOW,
      paired with the first blocking active wave id (the ``← dep`` edge
      label); the edge is ``None`` when a dangling-dep lookup leaves no
      resolvable blocker.

    NEXT and WAIT are scoped to *scope* (an iter id) when given, else to
    the active iter (:func:`_active_iter_id`); NOW is always the global
    active-wave pointer set. A :func:`blocked_by` lookup that hits a
    dangling dep degrades gracefully (the dep is dropped from the live
    view, never raised) so the band keeps rendering.

    Args:
        state: The bound state, or ``None`` (yields an empty slice).
        scope: Iter id scoping NEXT / WAIT; defaults to the active iter.
        max_parallel: NEXT-batch cap (``planning.max_parallel_waves``).

    Returns:
        A :class:`DispatchSlice` with ``now`` / ``next`` / ``wait`` /
        ``next_overflow`` populated.
    """
    if state is None:
        return DispatchSlice(now=(), next=(), wait=(), next_overflow=0)
    now = tuple(state.current.active_wave_ids)
    now_set = set(now)
    iter_id = scope if scope is not None else _active_iter_id(state)
    pending = [
        w
        for wid, w in state.waves.items()
        if w.status == WaveStatus.PENDING and (iter_id is None or w.iter_id == iter_id)
    ]
    ready: list[Wave] = []
    waiting: list[tuple[str, str | None]] = []
    for wave in pending:
        live_blockers = blocked_by(wave.id, state)
        if not live_blockers:
            ready.append(wave)
            continue
        active_blockers = [b for b in live_blockers if b in now_set]
        if active_blockers:
            waiting.append((wave.id, min(active_blockers)))
    ready.sort(key=lambda w: (_wave_index(w.id) is None, _wave_index(w.id) or 0, w.id))
    capped = ready[: max(max_parallel, 0)]
    overflow = len(ready) - len(capped)
    waiting.sort(
        key=lambda pair: (_wave_index(pair[0]) is None, _wave_index(pair[0]) or 0, pair[0])
    )
    return DispatchSlice(
        now=now,
        next=tuple(w.id for w in capped),
        wait=tuple(waiting),
        next_overflow=overflow,
    )


def _now_row(wave_id: str, state: State, *, dot: str, mode: RenderMode) -> str:
    """Render a single NOW row: pulsing dot + agent role + token-burn bar.

    The token-burn bar reads ``Wave.tokens_consumed`` / ``Wave.token_budget``
    live; a wave with no budget shows :data:`EMPTY_STATE` (the §8.9 live
    accrual wave populates the budget). A missing wave (dangling pointer)
    still renders its id so a NOW pointer drift never blanks the row.

    Args:
        wave_id: The active wave id to render.
        state: The bound state.
        dot: The pre-rendered pulse glyph for this frame.
        mode: Active render mode (``"braille"`` or ``"ascii"``).

    Returns:
        The rendered NOW row text.
    """
    wave = state.waves.get(wave_id)
    short = wave_id.split("-")[-1]
    if wave is None:
        return f"  {dot} {short}"
    role = wave.agent_role.value if wave.agent_role is not None else DASH
    burn = render_eu_bar_plain(wave.tokens_consumed, wave.token_budget or 0, mode=mode)
    return f"  {dot} {short}  {role}  {burn}"


def _dispatch_lines(
    state: State | None,
    *,
    mode: RenderMode,
    lit: bool = True,
    paused: bool = False,
) -> list[str]:
    """Build the DISPATCH section lines (NOW / NEXT / WAIT frontier).

    NOW renders one row per active wave (pulsing dot + agent role + live
    token-burn bar), collapsing to :data:`DISPATCH_IDLE` when no wave is
    active. NEXT and WAIT collapse inline: NEXT lists the ready batch
    (with a ``+N more`` suffix past the cap), WAIT lists each blocked wave
    with its ``← dep`` edge label. The running dot fades through the pulse
    glyph pair; ASCII mode or a paused pulse renders a static dot.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``).
        lit: Pulse phase for this frame (``True`` bright, ``False`` dim).
        paused: ``True`` when the pulse is paused (SUSPEND) — forces a
            static dot regardless of *lit*.

    Returns:
        The ordered DISPATCH lines (no section header).
    """
    slice_ = build_dispatch_slice(state)
    dot = pulse_glyph(lit, mode="ascii" if (mode == "ascii" or paused) else "braille")
    lines: list[str] = ["NOW"]
    if not slice_.now or state is None:
        lines.append(f"  {DISPATCH_IDLE}")
    else:
        lines.extend(_now_row(wid, state, dot=dot, mode=mode) for wid in slice_.now)
    next_short = " ".join(wid.split("-")[-1] for wid in slice_.next)
    next_body = next_short or DASH
    if slice_.next_overflow:
        next_body = f"{next_body} +{slice_.next_overflow} more"
    lines.append(f"NEXT  {next_body}")
    if slice_.wait:
        wait_parts = []
        for wave_id, blocker in slice_.wait:
            short = wave_id.split("-")[-1]
            if blocker is not None:
                wait_parts.append(f"{short}←{blocker.split('-')[-1]}")
            else:
                wait_parts.append(short)
        wait_body = " ".join(wait_parts)
    else:
        wait_body = DASH
    lines.append(f"WAIT  {wait_body}")
    return lines


def build_status_lines(
    state: State | None,
    *,
    mode: RenderMode = DEFAULT_RENDER_MODE,
    pulse_lit: bool = True,
    pulse_paused: bool = False,
) -> list[str]:
    """Build the status pane's grouped text lines from *state*.

    Pure render source — unit-testable without mounting the widget. The
    lines are grouped into four labelled sections, each headed by its
    name and built by a dedicated section builder:

    * ``LIFECYCLE`` — the project / phase / iter pointers, wave counters,
      completion bar, audit / worktree counters, blocked line.
    * ``EFFORT`` — consumed/estimate EU, signed variance %, an EU/day
      velocity sparkline, and an ETA from the current burn.
    * ``GATES`` — the active audit's live ``N/M`` check progress with
      per-check glyphs, collapsing to the verdict on completion.
    * ``DISPATCH`` — the live NOW / NEXT / WAIT dispatch frontier: active
      waves (pulsing dot + agent role + token burn), the next ready batch
      (capped to ``planning.max_parallel_waves``), and PENDING waves
      blocked by a running wave (with ``← dep`` edge labels).

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``) threaded
            from :attr:`eawf.tui.app.EaApp.render_mode`.
        pulse_lit: DISPATCH running-dot pulse phase for this frame.
        pulse_paused: ``True`` when the pulse is paused (SUSPEND) — the
            running dot renders static.

    Returns:
        The ordered list of plain-text lines, section headers included.
    """
    lines = ["LIFECYCLE", *_lifecycle_lines(state, mode=mode)]
    lines += ["", "EFFORT", *_effort_lines(state, mode=mode)]
    lines += ["", "GATES", *_gate_lines(state, mode=mode)]
    lines += [
        "",
        "DISPATCH",
        *_dispatch_lines(state, mode=mode, lit=pulse_lit, paused=pulse_paused),
    ]
    return lines


#: Inter-column gap between the two status columns (cells). Two spaces
#: read as a clear column break without wasting width.
COLUMN_GAP: int = 2

#: Pad the left column to this width before appending the right column.
#: Sized to the widest LIFECYCLE / GATES line that lands on the left
#: (``waves:     N active · N pending`` ≈ 30 cells) plus headroom for the
#: EU/burn bars EFFORT can grow, so a left-cell line never bleeds into the
#: right column. ``_repaint`` re-splits each two-column row at this offset
#: to recover the per-cell tokens for header / blocked styling.
LEFT_COLUMN_WIDTH: int = 36

#: Minimum pane content width (cells) at which the four sections lay out
#: in two columns. Below it the pane stays single-column (the repo-scope
#: quadrant, ≈ 56 cells of inner content, sits above this — but the
#: *content* the pane measures excludes the bordered-pane chrome, so the
#: narrow quadrant path keys off the live measurement, not this constant).
#: Set to ``LEFT_COLUMN_WIDTH`` + ``COLUMN_GAP`` + a readable right column
#: (≈ 36 cells) so both columns clear their widest line before the layout
#: flips; the repo quadrant's measured content (≈ 54) stays below it and
#: keeps the single-column render byte-identical to today's.
TWO_COLUMN_THRESHOLD: int = LEFT_COLUMN_WIDTH + COLUMN_GAP + 36


def _section_blocks(
    state: State | None,
    *,
    mode: RenderMode,
    pulse_lit: bool,
    pulse_paused: bool,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return the four labelled section blocks (header + lines each).

    Each block is a self-contained list whose first entry is the section
    header literal so the two-column layout can pair whole sections and
    ``_repaint`` keeps detecting the header at the start of its cell.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``).
        pulse_lit: DISPATCH running-dot pulse phase for this frame.
        pulse_paused: ``True`` when the pulse is paused (SUSPEND).

    Returns:
        ``(lifecycle, effort, gates, dispatch)`` blocks, each a list of
        plain-text lines headed by its section name.
    """
    lifecycle = ["LIFECYCLE", *_lifecycle_lines(state, mode=mode)]
    effort = ["EFFORT", *_effort_lines(state, mode=mode)]
    gates = ["GATES", *_gate_lines(state, mode=mode)]
    dispatch = ["DISPATCH", *_dispatch_lines(state, mode=mode, lit=pulse_lit, paused=pulse_paused)]
    return lifecycle, effort, gates, dispatch


def _stack(*blocks: list[str]) -> list[str]:
    """Stack section *blocks* into one column with a blank line between each."""
    out: list[str] = []
    for block in blocks:
        if out:
            out.append("")
        out.extend(block)
    return out


def _two_columns(left: list[str], right: list[str]) -> list[str]:
    """Lay two stacked columns side by side, padding the shorter to match.

    Each left line is padded to :data:`LEFT_COLUMN_WIDTH` then the
    :data:`COLUMN_GAP` and the right line are appended; a row past one
    column's height pads that side with blanks so the surviving column
    keeps rendering. Trailing whitespace on a right-blank row is stripped
    so a short right column never trails padding into the snapshot.

    Args:
        left: The left column's stacked lines.
        right: The right column's stacked lines.

    Returns:
        The composed two-column rows, one per ``max(len(left), len(right))``.
    """
    rows: list[str] = []
    height = max(len(left), len(right))
    pad = " " * (LEFT_COLUMN_WIDTH + COLUMN_GAP)
    for index in range(height):
        left_cell = left[index] if index < len(left) else ""
        right_cell = right[index] if index < len(right) else ""
        if right_cell:
            rows.append(f"{left_cell.ljust(LEFT_COLUMN_WIDTH)}{' ' * COLUMN_GAP}{right_cell}")
        else:
            rows.append(left_cell if left_cell else pad.rstrip())
    return rows


def build_status_columns(
    state: State | None,
    *,
    mode: RenderMode = DEFAULT_RENDER_MODE,
    pulse_lit: bool = True,
    pulse_paused: bool = False,
    width: int,
) -> list[str]:
    """Build the status pane's lines, two-column when *width* allows.

    Pure render source — unit-testable without mounting the widget. When
    *width* is at or above :data:`TWO_COLUMN_THRESHOLD` the four sections
    lay out in two balanced columns — LIFECYCLE + GATES on the left,
    EFFORT + DISPATCH on the right — so a wide pane stops wasting space on
    one tall column. Below the threshold (the narrow repo-scope quadrant)
    it returns exactly :func:`build_status_lines`'s single-column flat
    list, byte-identical, so the narrow render is unchanged.

    Each two-column row pads its left cell to :data:`LEFT_COLUMN_WIDTH`;
    ``_repaint`` re-splits the row at that offset to recover the two cells
    and apply the header / blocked styling per cell rather than per line.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``) threaded
            from :attr:`eawf.tui.app.EaApp.render_mode`.
        pulse_lit: DISPATCH running-dot pulse phase for this frame.
        pulse_paused: ``True`` when the pulse is paused (SUSPEND).
        width: The live pane content width in cells; ``< 1`` (pre-layout)
            falls back to the single column.

    Returns:
        The ordered plain-text rows — two-column when *width* allows, else
        the single-column flat list.
    """
    if width < TWO_COLUMN_THRESHOLD:
        return build_status_lines(state, mode=mode, pulse_lit=pulse_lit, pulse_paused=pulse_paused)
    lifecycle, effort, gates, dispatch = _section_blocks(
        state, mode=mode, pulse_lit=pulse_lit, pulse_paused=pulse_paused
    )
    left = _stack(lifecycle, gates)
    right = _stack(effort, dispatch)
    return _two_columns(left, right)


class StatusPane(Static):
    """Live current-scope status summary pane.

    Watches the host app's reactive ``state`` (seeded on mount) and
    repaints the grouped LIFECYCLE / EFFORT / GATES / DISPATCH block on
    every revision. The DISPATCH band's running dot pulses on a separate
    ``TICK_PULSE`` (~2 Hz) timer that advances only the dot phase — no
    frontier recompute or data refetch rides the pulse. The pulse pauses
    when the terminal loses focus (backgrounded) and resumes on regain.
    Standalone-testable by assigning :attr:`state` directly.
    """

    DEFAULT_CSS: ClassVar[str] = """
    StatusPane {
        height: auto;
        width: 1fr;
    }
    """

    #: Bound state, watched so a fresh revision repaints. ``None`` until
    #: the first read-only load completes.
    state: reactive[State | None] = reactive(None)

    #: DISPATCH running-dot pulse phase, toggled by the ``TICK_PULSE``
    #: timer. Watched so each toggle repaints only the dot — the frontier
    #: data is untouched. Not exported; cosmetic-only.
    _pulse_lit: reactive[bool] = reactive(True)

    #: ``True`` when the pulse is paused (terminal backgrounded) so the
    #: running dot renders static and the timer skips the phase toggle.
    _pulse_paused: reactive[bool] = reactive(False)

    def on_mount(self) -> None:
        """Seed from the app's reactive state and watch for revisions.

        Starts the ``TICK_PULSE`` (~2 Hz) timer driving the DISPATCH
        running dot and watches the app's terminal focus so the pulse
        pauses when the terminal is backgrounded.
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        if hasattr(self.app, "app_focus"):
            self.watch(self.app, "app_focus", self._on_app_focus)
        self.set_interval(PULSE_INTERVAL_S, self._tick_pulse)
        self._repaint()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_render_mode(self, _mode: RenderMode) -> None:
        """Repaint when the app flips the bar render mode (Braille ↔ ASCII)."""
        self._repaint()

    def _on_app_focus(self, focused: bool) -> None:
        """Pause the pulse when the terminal backgrounds; resume on regain.

        Args:
            focused: ``True`` when the terminal has focus, ``False`` when
                it is backgrounded (the ``SUSPEND`` analogue).
        """
        if focused:
            self.resume_pulse()
        else:
            self.pause_pulse()

    def _tick_pulse(self) -> None:
        """Advance the DISPATCH dot phase on each ``TICK_PULSE`` tick.

        Cosmetic-only: toggles the pulse phase so the running dot fades
        bright⇄dim. No frontier recompute or data fetch happens here — a
        paused pulse skips the toggle entirely.
        """
        if not self._pulse_paused:
            self._pulse_lit = not self._pulse_lit

    def pause_pulse(self) -> None:
        """Pause the running-dot animation (static dot, no phase toggle)."""
        self._pulse_paused = True

    def resume_pulse(self) -> None:
        """Resume the running-dot animation after a pause."""
        self._pulse_paused = False

    def watch_state(self) -> None:
        """Repaint when the bound state changes."""
        self._repaint()

    def watch__pulse_lit(self) -> None:
        """Repaint when the DISPATCH dot phase toggles (cosmetic-only)."""
        self._repaint()

    def watch__pulse_paused(self) -> None:
        """Repaint when the pulse pauses / resumes (static ↔ animated dot)."""
        self._repaint()

    def _render_mode(self) -> RenderMode:
        """Return the app's active render mode, defaulting when unavailable."""
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _repaint(self) -> None:
        """Re-render the status lines from the current state.

        Lays the four sections in two columns when the pane is wide enough
        (:data:`TWO_COLUMN_THRESHOLD`) and one column in the narrow repo
        quadrant; the live :attr:`content_size` width drives the choice
        (a pre-layout width of ``0`` falls back to one column). Section
        headers carry a bold ``[$accent]…[/]`` span; the blocked line
        carries the palette error colour; every other ``label: value`` row
        carries the accent label tint, with all cell text markup-escaped
        against accidental spans. In a two-column row
        the per-cell styling is applied after re-splitting the row at the
        left-column offset, so a right-column header still highlights. The
        bar/sparkline glyphs honour the app's live :attr:`render_mode`;
        the DISPATCH dot honours the live pulse phase.
        """
        rows = build_status_columns(
            self.state,
            mode=self._render_mode(),
            pulse_lit=self._pulse_lit,
            pulse_paused=self._pulse_paused,
            width=self.content_size.width,
        )
        rendered = [self._style_row(row) for row in rows]
        self.update("\n".join(rendered))

    @staticmethod
    def _style_cell(cell: str) -> str:
        """Style one cell by kind, markup-escaping the content throughout.

        Section headers get the bold accent span; a blocked line gets the
        palette error colour; a DISPATCH band label (``NOW`` / ``NEXT`` /
        ``WAIT``) gets the accent tint on its leading token; every other
        ``label: value`` row gets the accent label tint
        (:func:`~eawf.tui.widgets.markup.style_labeled_line`), matching the
        detail modal. The cell text is markup-escaped throughout so
        user-derived text never opens a stray markup span.

        Args:
            cell: The raw (unescaped) cell text.

        Returns:
            The cell wrapped in its palette span(s), or escaped plain text.
        """
        if cell in _SECTION_HEADERS:
            return f"[b $accent]{escape_markup(cell)}[/]"
        if cell.startswith("blocked:"):
            return f"[$err]{escape_markup(cell)}[/]"
        head, gap, rest = cell.partition("  ")
        if head in _DISPATCH_BANDS:
            return f"[$accent]{head}[/]{gap}{escape_markup(rest)}"
        return style_labeled_line(cell)

    def _style_row(self, row: str) -> str:
        """Style a row's cells, splitting a two-column row into its two cells.

        A single-column row is styled as one cell. A two-column row (one
        wider than :data:`LEFT_COLUMN_WIDTH`) is split at the left-column
        offset so the left and right cells each get header / blocked
        styling, then re-joined preserving the original gap width.

        Args:
            row: The composed plain-text row.

        Returns:
            The markup-styled row.
        """
        split = LEFT_COLUMN_WIDTH + COLUMN_GAP
        if len(row) <= split:
            return self._style_cell(row)
        left_cell = row[:LEFT_COLUMN_WIDTH].rstrip()
        right_cell = row[split:]
        # Pad to the left column's *visible* width before styling: markup
        # tags are zero-width on screen, so ljust on the wrapped string
        # would over-pad. The gap restores the cell's full LEFT_COLUMN_WIDTH.
        pad = " " * (LEFT_COLUMN_WIDTH - len(left_cell) + COLUMN_GAP)
        return f"{self._style_cell(left_cell)}{pad}{self._style_cell(right_cell)}"


__all__ = [
    "COLUMN_GAP",
    "DASH",
    "DEFAULT_MAX_PARALLEL_WAVES",
    "DEFAULT_PROJECT_CODE",
    "DISPATCH_IDLE",
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_PENDING",
    "GATE_RUNNING",
    "LEFT_COLUMN_WIDTH",
    "TWO_COLUMN_THRESHOLD",
    "VELOCITY_WINDOW_DAYS",
    "DispatchSlice",
    "StatusPane",
    "build_dispatch_slice",
    "build_status_columns",
    "build_status_lines",
    "build_velocity_eu_per_day",
    "summary_counts",
]
