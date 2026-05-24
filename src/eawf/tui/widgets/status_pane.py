"""``StatusPane`` — current-scope status summary widget (widget catalog).

A :class:`~textual.widgets.Static` composite that surfaces the current
scope's lifecycle counters — project / phase / iter / wave counts, audit
count, open worktrees, and blocked (failed) waves — alongside an EFFORT
block (consumed/estimate EU, signed variance %, an EU/day velocity
sparkline, and an ETA) and a GATES block (live ``audit_check_*`` N/M
progress collapsing to the verdict). Rendered as a live pane that watches
the reactive :class:`~eawf.state.models.State`.

The pane groups its rendered lines into three labelled sections —
LIFECYCLE / EFFORT / GATES — each built by a dedicated pure function so
the line set stays unit-testable without mounting the widget. A fourth
DISPATCH band (NOW / NEXT / WAIT) is a documented follow-up: it appends
after GATES, so the section-builder seam (:func:`build_status_lines`
composing the per-section builders in order) leaves a clean attachment
point for it.

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

from textual.reactive import reactive
from textual.widgets import Static

from eawf.state.enums import (
    AuditStatus,
    IterStatus,
    PhaseStatus,
    WaveStatus,
    WorktreeStatus,
)
from eawf.tui.widgets.eu_bar import (
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    RenderMode,
    render_completion_bar,
    render_eu_bar_plain,
)
from eawf.tui.widgets.variance_tile import render_variance_plain

if TYPE_CHECKING:
    from eawf.state.models import Audit, State

logger = logging.getLogger(__name__)

#: Placeholder shown when a pointer (phase / iter) is unset.
DASH: str = "—"

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


def _scoped_scope_ids(state: State) -> set[str]:
    """Return the scope ids (active iter + its waves) effort attaches to.

    EU estimates / actuals are keyed by ``scope_id`` — either the iter id
    or one of its wave ids. The set lets :func:`_effort_eu` and
    :func:`build_velocity_eu_per_day` pick only the rows belonging to the
    active iter's subtree, never a sibling iter's leftovers.
    """
    iter_id = _active_iter_id(state)
    if iter_id is None:
        return set()
    wave_ids = {wid for wid, w in state.waves.items() if w.iter_id == iter_id}
    return {iter_id, *wave_ids}


def _effort_eu(state: State | None) -> tuple[float, float]:
    """Return the active iter's ``(consumed_eu, estimate_eu)`` pair.

    Sums :attr:`~eawf.state.models.ActualSummary.elapsed_eu` over the
    actuals whose ``scope_id`` is the active iter or one of its waves, and
    likewise sums :attr:`~eawf.state.models.EstimateSummary.expected_eu`
    over the matching estimates. Either total is ``0.0`` when no row is in
    scope; the caller treats a non-positive estimate as the empty state.

    Args:
        state: The bound state, or ``None``.

    Returns:
        ``(consumed_eu, estimate_eu)``.
    """
    if state is None:
        return 0.0, 0.0
    scope_ids = _scoped_scope_ids(state)
    if not scope_ids:
        return 0.0, 0.0
    consumed = sum(a.elapsed_eu for a in (state.actuals or {}).values() if a.scope_id in scope_ids)
    estimate = sum(
        e.expected_eu for e in (state.estimates or {}).values() if e.scope_id in scope_ids
    )
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

    Buckets the active iter's actuals by their ``updated_at`` calendar day
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


def _lifecycle_lines(state: State | None) -> list[str]:
    """Build the LIFECYCLE section lines (the lifecycle counter block).

    Mirrors the ``eawf status`` summary: project / phase / iter pointers,
    the active-phase wave counters, a completion bar, the audit / worktree
    counters, then a blocked line when any wave has failed.

    Args:
        state: The bound state, or ``None``.

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
    progress = render_completion_bar(counts["waves_closed"], counts["waves_total"])
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


def build_status_lines(state: State | None, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> list[str]:
    """Build the status pane's grouped text lines from *state*.

    Pure render source — unit-testable without mounting the widget. The
    lines are grouped into three labelled sections, each headed by its
    name and built by a dedicated section builder:

    * ``LIFECYCLE`` — the project / phase / iter pointers, wave counters,
      completion bar, audit / worktree counters, blocked line.
    * ``EFFORT`` — consumed/estimate EU, signed variance %, an EU/day
      velocity sparkline, and an ETA from the current burn.
    * ``GATES`` — the active audit's live ``N/M`` check progress with
      per-check glyphs, collapsing to the verdict on completion.

    A fourth ``DISPATCH`` band (NOW / NEXT / WAIT) appends after GATES in
    a follow-up; the per-section composition below is the seam it attaches
    to.

    Args:
        state: The bound state, or ``None``.
        mode: Active render mode (``"braille"`` or ``"ascii"``) threaded
            from :attr:`eawf.tui.app.EaApp.render_mode`.

    Returns:
        The ordered list of plain-text lines, section headers included.
    """
    lines = ["LIFECYCLE", *_lifecycle_lines(state)]
    lines += ["", "EFFORT", *_effort_lines(state, mode=mode)]
    lines += ["", "GATES", *_gate_lines(state, mode=mode)]
    return lines


class StatusPane(Static):
    """Live current-scope status summary pane.

    Watches the host app's reactive ``state`` (seeded on mount) and
    repaints the grouped LIFECYCLE / EFFORT / GATES block on every
    revision. Standalone-testable by assigning :attr:`state` directly.
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

    def on_mount(self) -> None:
        """Seed from the app's reactive state and watch for revisions."""
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._repaint()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_render_mode(self, _mode: RenderMode) -> None:
        """Repaint when the app flips the bar render mode (Braille ↔ ASCII)."""
        self._repaint()

    def watch_state(self) -> None:
        """Repaint when the bound state changes."""
        self._repaint()

    def _render_mode(self) -> RenderMode:
        """Return the app's active render mode, defaulting when unavailable."""
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _repaint(self) -> None:
        """Re-render the status lines from the current state.

        Section headers carry a bold ``[$accent]…[/]`` span; the blocked
        line carries the palette error colour; every other line is escaped
        against accidental markup and rendered plain. The bar/sparkline
        glyphs honour the app's live :attr:`render_mode`.
        """
        headers = {"LIFECYCLE", "EFFORT", "GATES"}
        rendered: list[str] = []
        for line in build_status_lines(self.state, mode=self._render_mode()):
            safe = line.replace("[", "[[")
            if line in headers:
                rendered.append(f"[b $accent]{safe}[/]")
            elif line.startswith("blocked:"):
                rendered.append(f"[$err]{safe}[/]")
            else:
                rendered.append(safe)
        self.update("\n".join(rendered))


__all__ = [
    "DASH",
    "DEFAULT_PROJECT_CODE",
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_PENDING",
    "GATE_RUNNING",
    "VELOCITY_WINDOW_DAYS",
    "StatusPane",
    "build_status_lines",
    "build_velocity_eu_per_day",
    "summary_counts",
]
