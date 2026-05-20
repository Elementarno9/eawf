"""``StatusPane`` — current-scope status summary widget (C06 widget catalog).

Per the C06 brief §5.3 widget row: a :class:`~textual.widgets.Static`
composite that surfaces the current scope's lifecycle counters —
project / phase / iter / wave counts, audit count, open worktrees, and
blocked (failed) waves — the same information the ``eawf status`` text
output carries, rendered as a live pane that watches the reactive
:class:`~eawf.state.models.State`.

The full V8 SESSIONS sub-block (per-session run rows) is a richer
follow-up; this wave ships the counter summary block those rows hang off
and leaves a documented seam (:func:`build_status_lines` is the single
render source). The pane is driven by the host
:class:`~eawf.tui_v2.app.EaApp` reactive ``state``: on mount it seeds
from ``app.state`` and registers a watcher so daemon-pushed revisions
repaint it; standalone tests assign :attr:`state` directly.

Counter derivation lives in pure functions (:func:`summary_counts`) so
the numbers are unit-testable without mounting the widget. Colours, where
used, resolve against the ``theme.tcss`` palette vars — never hardcoded
hex.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.reactive import reactive
from textual.widgets import Static

from eawf.state.enums import (
    AuditStatus,
    IterStatus,
    PhaseStatus,
    WaveStatus,
    WorktreeStatus,
)

if TYPE_CHECKING:
    from eawf.state.models import State

#: Placeholder shown when a pointer (phase / iter) is unset.
DASH: str = "—"

#: Project-code fallback when no project record is loaded.
DEFAULT_PROJECT_CODE: str = "EAWF"


def summary_counts(state: State | None) -> dict[str, int]:
    """Tally the lifecycle counters the status pane surfaces.

    All keys are present and zero for a ``None`` / empty state so the pane
    renders a deterministic frame before any roadmap activity.

    Args:
        state: The bound state, or ``None``.

    Returns:
        A dict with keys ``phases_active`` / ``iters_active`` /
        ``waves_pending`` / ``waves_in_progress`` / ``waves_failed`` /
        ``audits_running`` / ``audits_total`` / ``worktrees_active``.
    """
    if state is None:
        return {
            "phases_active": 0,
            "iters_active": 0,
            "waves_pending": 0,
            "waves_in_progress": 0,
            "waves_failed": 0,
            "audits_running": 0,
            "audits_total": 0,
            "worktrees_active": 0,
        }
    waves = state.waves.values()
    audits = (state.audits or {}).values()
    worktrees = (state.worktrees or {}).values()
    return {
        "phases_active": sum(1 for p in state.phases.values() if p.status is PhaseStatus.ACTIVE),
        "iters_active": sum(1 for it in state.iters.values() if it.status is IterStatus.ACTIVE),
        "waves_pending": sum(1 for w in waves if w.status is WaveStatus.PENDING),
        "waves_in_progress": sum(1 for w in waves if w.status is WaveStatus.IN_PROGRESS),
        "waves_failed": sum(1 for w in waves if w.status is WaveStatus.FAILED),
        "audits_running": sum(1 for a in audits if a.status is AuditStatus.RUNNING),
        "audits_total": len(state.audits or {}),
        "worktrees_active": sum(1 for wt in worktrees if wt.status is WorktreeStatus.ACTIVE),
    }


def build_status_lines(state: State | None) -> list[str]:
    """Build the status pane's text lines from *state*.

    Pure render source — unit-testable without mounting the widget. The
    line set mirrors the ``eawf status`` summary: project / phase / iter
    pointers, then the wave / audit / worktree counters, then a blocked
    line when any wave has failed.

    Args:
        state: The bound state, or ``None``.

    Returns:
        The ordered list of plain-text lines.
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
    lines = [
        f"project:   {project}",
        f"phase:     {phase}",
        f"iter:      {iter_id}",
        f"waves:     {counts['waves_in_progress']} active · {counts['waves_pending']} pending",
        f"audits:    {counts['audits_running']} running · {counts['audits_total']} total",
        f"worktrees: {counts['worktrees_active']} active",
    ]
    blocked = counts["waves_failed"]
    if blocked:
        lines.append(f"blocked:   {blocked} failed wave(s)")
    return lines


class StatusPane(Static):
    """Live current-scope status summary pane.

    Watches the host app's reactive ``state`` (seeded on mount) and
    repaints the counter block on every revision. Standalone-testable by
    assigning :attr:`state` directly.
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
        self._repaint()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Repaint when the bound state changes."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the status lines from the current state.

        The blocked line is wrapped in a ``[$err]…[/]`` content-markup
        span so it carries the palette error colour; every other line is
        escaped against accidental markup and rendered plain.
        """
        rendered: list[str] = []
        for line in build_status_lines(self.state):
            safe = line.replace("[", "[[")
            if line.startswith("blocked:"):
                rendered.append(f"[$err]{safe}[/]")
            else:
                rendered.append(safe)
        self.update("\n".join(rendered))


__all__ = [
    "DASH",
    "DEFAULT_PROJECT_CODE",
    "StatusPane",
    "build_status_lines",
    "summary_counts",
]
