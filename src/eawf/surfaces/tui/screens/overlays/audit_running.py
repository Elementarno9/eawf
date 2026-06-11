"""``AuditRunningModal`` — live audit-in-progress overlay.

The read-only audit-progress surface: auto-opened when the TUI receives
an ``audit_started`` event for a scope visible in the current screen, it
shows one row per audit check with a lifecycle-sigil status glyph (the
closed sigil = pass / the failed sigil = fail / the running sigil = still
running, drawn from the shared
:mod:`~eawf.surfaces.tui.widgets.sigils` vocabulary), a block-progress bar
over the reported-check share, and a running ``done/total`` tally. The
overlay pops on the
``audit_completed`` event (the host swaps in
:class:`~eawf.surfaces.tui.screens.overlays.audit_failed.AuditFailedModal` when
the verdict is ``fail``) or on ``Esc`` (minimise to the footer chip
``A19 4/7``).

This wave lands the **overlay**: the per-check rows rendered from a
typed :class:`AuditProgress`, the glyph + tally presentation, and the
live-update seam (:meth:`AuditRunningModal.update_progress`) the
daemon-push handler drives as ``check_*`` events arrive. Wiring the
``audit_started`` / ``audit_completed`` daemon-push auto-open + the
per-check event stream rides the wave that lands the event
subscription; the overlay exposes the update method those events call.

The progress aggregate is a pure dataclass (:class:`AuditProgress`) so the
rendered rows + the ``done/total`` tally are unit-testable without
mounting Textual; the modal is a thin view that repaints on each
:meth:`update_progress`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.eu_bar import (
    DEFAULT_RENDER_MODE,
    RenderMode,
    render_completion_bar,
)
from eawf.surfaces.tui.widgets.sigils import Sigil

logger = logging.getLogger(__name__)


class CheckState(StrEnum):
    """Per-check progress state in a running audit.

    Attributes:
        RUNNING: The check has not yet reported (the running lifecycle
            sigil).
        PASS: The check passed (the closed lifecycle sigil).
        FAIL: The check failed (the failed lifecycle sigil).
    """

    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"


#: :class:`CheckState` -> the lifecycle :class:`~eawf.surfaces.tui.widgets.sigils.Sigil`
#: whose glyph renders the per-check row. A running check ticks the running
#: sigil, a pass folds onto the closed sigil, a fail onto the failed sigil --
#: so the audit overlay shares the SHAPE vocabulary every reskin pane reads
#: rather than inventing its own dot / check / cross marks.
_CHECK_SIGIL: dict[CheckState, Sigil] = {
    CheckState.RUNNING: Sigil.RUNNING,
    CheckState.PASS: Sigil.CLOSED,
    CheckState.FAIL: Sigil.FAILED,
}


def _check_glyph(state: CheckState, *, mode: RenderMode) -> str:
    """Return the per-check glyph for *state* in the active render *mode*.

    Routes through the single-home sigil vocabulary
    (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) so the audit overlay
    never hardcodes a glyph: a running check renders the running sigil, a
    pass the closed sigil, a fail the failed sigil.

    Args:
        state: The check's current :class:`CheckState`.
        mode: The App's resolved render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The single-cell glyph string for *state* in the resolved column.
    """
    return sigils.glyph(_CHECK_SIGIL[state], mode=mode)


@dataclass(frozen=True)
class CheckRow:
    """One audit-check row in the running-audit overlay.

    Attributes:
        name: The check name (e.g. ``pytest_pass`` / ``coverage_min``).
        state: The check's current :class:`CheckState`.
    """

    name: str
    state: CheckState


@dataclass(frozen=True)
class AuditProgress:
    """A snapshot of a running audit's per-check progress.

    Attributes:
        audit_id: The audit id (e.g. ``A19-P14``) — derived from state
            for the current scope, never operator-typed.
        scope_label: A short scope label shown in the title (the resolved
            scope the audit covers). Derived, not operator-typed.
        checks: Ordered per-check rows.
    """

    audit_id: str
    scope_label: str
    checks: tuple[CheckRow, ...]

    def done(self) -> int:
        """Return the count of checks that have reported (pass or fail)."""
        return sum(1 for c in self.checks if c.state is not CheckState.RUNNING)

    def total(self) -> int:
        """Return the total number of checks."""
        return len(self.checks)

    def with_check(self, name: str, state: CheckState) -> AuditProgress:
        """Return a copy with check *name* set to *state*.

        A no-op (returns ``self`` unchanged) when *name* is not a known
        check, so a stray event for an unrelated check cannot corrupt the
        snapshot.

        Args:
            name: The check name to update.
            state: The new state for that check.

        Returns:
            The updated progress snapshot, or ``self`` when *name* is
            unknown.
        """
        updated: list[CheckRow] = []
        hit = False
        for check in self.checks:
            if check.name == name:
                updated.append(replace(check, state=state))
                hit = True
            else:
                updated.append(check)
        if not hit:
            return self
        return replace(self, checks=tuple(updated))


class AuditRunningModal(ModalScreen[None]):
    """Live audit-progress overlay (Esc minimises; auto-pops on complete).

    Read-only per the brief: each check renders its glyph + name and the
    title carries a ``done/total`` tally. :meth:`update_progress` swaps in
    a fresh :class:`AuditProgress` snapshot (the daemon-push handler calls
    it as ``check_*`` events arrive) and the overlay repaints. ``Esc``
    dismisses (the host re-derives the footer chip).
    """

    DEFAULT_CSS: ClassVar[str] = """
    AuditRunningModal {
        align: center middle;
    }
    AuditRunningModal > #audit-running-box {
        width: 60%;
        max-width: 90;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    AuditRunningModal .audit-running-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    AuditRunningModal .audit-running-bar {
        color: $accent;
        height: 1;
        margin-top: 1;
    }
    AuditRunningModal #audit-running-rows {
        height: auto;
        max-height: 70%;
        margin-top: 1;
    }
    AuditRunningModal .audit-check {
        height: 1;
        color: $text-muted;
    }
    AuditRunningModal .audit-check.-pass {
        color: $accent;
    }
    AuditRunningModal .audit-check.-fail {
        color: $error;
    }
    AuditRunningModal .audit-running-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Esc`` minimises the overlay (the only binding it owns; the
    #: ``audit_completed`` event drives the auto-pop).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "minimise", show=False),
    ]

    #: The current progress snapshot. Seeded in ``__init__`` and reassigned
    #: by :meth:`update_progress`; the reactive watcher repaints the title
    #: tally + per-check rows on each change.
    progress: reactive[AuditProgress | None] = reactive(None, init=False)

    def __init__(self, progress: AuditProgress) -> None:
        """Construct the overlay for an initial *progress* snapshot.

        Args:
            progress: The initial per-check progress (typically every
                check ``RUNNING`` at ``audit_started``).
        """
        super().__init__()
        self.progress = progress

    def compose(self) -> ComposeResult:
        """Yield the title, the block-progress bar, per-check rows, hint.

        The initial text is composed directly from the seeded snapshot so
        the first paint is correct without waiting on a reactive watcher;
        :meth:`update_progress` drives live re-renders thereafter. The
        block-progress bar (the shared
        :func:`~eawf.surfaces.tui.widgets.eu_bar.render_completion_bar` over
        the reported-check share) sits under the title so the operator reads
        the audit's done-fraction at a glance beside the per-check sigils.
        """
        snapshot = self.progress
        mode = self._render_mode()
        with Vertical(id="audit-running-box"):
            yield Static(
                _title_text(snapshot), classes="audit-running-title", id="audit-running-title"
            )
            yield Static(
                _bar_text(snapshot, mode=mode),
                classes="audit-running-bar",
                id="audit-running-bar",
            )
            with VerticalScroll(id="audit-running-rows"):
                yield Static(_rows_text(snapshot, mode=mode), id="audit-running-rows-inner")
            yield Static("[ Esc to minimise ]", classes="audit-running-hint")

    def _render_mode(self) -> RenderMode:
        """Return the App's resolved render mode, defaulting when unbound.

        Reads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` so the
        per-check sigils + the block-progress bar pick the right glyph
        column. A bare harness whose host App carries no ``render_mode`` (a
        direct construction outside the full app) falls back to the shared
        default.

        Returns:
            The active render mode (``"unicode"`` / ``"ascii"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def update_progress(self, progress: AuditProgress) -> None:
        """Swap in a fresh progress snapshot (daemon-push entry point).

        Args:
            progress: The new per-check progress snapshot.
        """
        self.progress = progress

    def watch_progress(self) -> None:
        """Repaint the title tally, bar, and per-check rows on a new snapshot."""
        if self.is_mounted:
            self._repaint()

    def _repaint(self) -> None:
        """Rebuild the title, bar, and per-check row block from the snapshot."""
        snapshot = self.progress
        mode = self._render_mode()
        self.query_one("#audit-running-title", Static).update(_title_text(snapshot))
        self.query_one("#audit-running-bar", Static).update(_bar_text(snapshot, mode=mode))
        self.query_one("#audit-running-rows-inner", Static).update(_rows_text(snapshot, mode=mode))

    def action_close(self) -> None:
        """Dismiss the overlay (``Esc`` = minimise)."""
        self.dismiss(None)


def _title_text(progress: AuditProgress | None) -> str:
    """Format the overlay title line (``audit <id> · <scope> [d/t]``).

    Args:
        progress: The current snapshot, or ``None`` before one is seeded.

    Returns:
        The title text, or a placeholder when no snapshot is present.
    """
    if progress is None:
        return "audit"
    return (
        f"audit {progress.audit_id} · {progress.scope_label} [{progress.done()}/{progress.total()}]"
    )


def _bar_text(progress: AuditProgress | None, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Format the block-progress bar over the reported-check share.

    Renders the shared
    :func:`~eawf.surfaces.tui.widgets.eu_bar.render_completion_bar` over the
    ``done/total`` reported-check ratio so the audit's progress reads as a
    block-filled bar (unicode) / ``#``/``-`` fill (ascii) with a trailing
    ``done/total`` counter. A snapshot with no checks (or none yet seeded)
    surfaces the shared empty-state sentinel rather than a fabricated bar.

    Args:
        progress: The current snapshot, or ``None`` before one is seeded.
        mode: The active render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The rendered completion-bar string, or the empty-state sentinel when
        there are no checks.
    """
    if progress is None:
        return render_completion_bar(0, 0, mode=mode)
    return render_completion_bar(progress.done(), progress.total(), mode=mode)


def _rows_text(progress: AuditProgress | None, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Format the per-check row block (one ``<sigil>  <name>`` per line).

    Each check renders its lifecycle sigil glyph (running / closed / failed
    via :func:`_check_glyph`) so the row marks share the reskin SHAPE
    vocabulary rather than a hardcoded dot / check / cross.

    Args:
        progress: The current snapshot, or ``None`` before one is seeded.
        mode: The active render mode (``"unicode"`` / ``"ascii"``).

    Returns:
        The newline-joined check rows, or ``(no checks)`` when empty.
    """
    if progress is None or not progress.checks:
        return "(no checks)"
    return "\n".join(
        f"{_check_glyph(check.state, mode=mode)}  {check.name}" for check in progress.checks
    )


def open_audit_running(app: object, progress: AuditProgress) -> None:
    """Push the audit-running overlay onto *app*'s screen stack (cap-checked).

    Routes through the App's modal-cap-aware ``push_modal`` helper when
    present (so the modal-stack depth limit is enforced), falling back to a
    plain ``push_screen`` under a bare harness — mirroring the
    :func:`~eawf.surfaces.tui.screens.help.open_help` pattern. The ``/audit``
    palette path and the future ``audit_started`` daemon-push handler both
    call this.

    Args:
        app: The running App (typed loosely to avoid an import cycle with
            :mod:`eawf.surfaces.tui.app`).
        progress: The initial per-check progress snapshot.
    """
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(AuditRunningModal(progress))
        return
    push_screen = getattr(app, "push_screen", None)
    if callable(push_screen):
        push_screen(AuditRunningModal(progress))


__all__ = [
    "AuditProgress",
    "AuditRunningModal",
    "CheckRow",
    "CheckState",
    "open_audit_running",
]
