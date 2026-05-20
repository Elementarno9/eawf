"""``AuditRunningModal`` — live audit-in-progress overlay (C06 §5.7).

The read-only audit-progress surface from the C06 brief §5.7 modal-stack
inventory: auto-opened when the TUI receives an ``audit_started`` event
for a scope visible in the current screen, it shows one row per audit
check with a status glyph (``✓`` pass / ``✗`` fail / ``·`` still running)
and a running ``done/total`` tally. The overlay pops on the
``audit_completed`` event (the host swaps in
:class:`~eawf.tui_v2.screens.overlays.audit_failed.AuditFailedModal` when
the verdict is ``fail``) or on ``Esc`` (minimise to the footer chip
``A19 4/7``).

Per the W19 deferral pattern this wave lands the **overlay**: the
per-check rows rendered from a typed :class:`AuditProgress`, the glyph +
tally presentation, and the live-update seam (:meth:`AuditRunningModal.update_progress`)
the daemon-push handler drives as ``check_*`` events arrive. Wiring the
``audit_started`` / ``audit_completed`` daemon-push auto-open + the
per-check event stream (C06 §5.8) rides the wave that lands the event
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

logger = logging.getLogger(__name__)


class CheckState(StrEnum):
    """Per-check progress state in a running audit.

    Attributes:
        RUNNING: The check has not yet reported (``·`` spinner glyph).
        PASS: The check passed (``✓`` glyph).
        FAIL: The check failed (``✗`` glyph).
    """

    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"


#: Glyph rendered for each :class:`CheckState`. ``·`` doubles as the
#: still-running spinner (a static dot — the live spinner animation rides
#: the wave that wires the per-check event tick).
_GLYPH: dict[CheckState, str] = {
    CheckState.RUNNING: "·",
    CheckState.PASS: "✓",
    CheckState.FAIL: "✗",
}


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
        audit_id: The audit id (e.g. ``A19-P14``).
        scope_label: A short scope label shown in the title (e.g. the
            wave / iter / phase the audit covers).
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
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    AuditRunningModal .audit-running-title {
        text-style: bold;
        color: $accent;
        height: 1;
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
        """Yield the title (with tally), the per-check rows, and the hint.

        The initial text is composed directly from the seeded snapshot so
        the first paint is correct without waiting on a reactive watcher;
        :meth:`update_progress` drives live re-renders thereafter.
        """
        snapshot = self.progress
        with Vertical(id="audit-running-box"):
            yield Static(
                _title_text(snapshot), classes="audit-running-title", id="audit-running-title"
            )
            with VerticalScroll(id="audit-running-rows"):
                yield Static(_rows_text(snapshot), id="audit-running-rows-inner")
            yield Static("[ Esc to minimise ]", classes="audit-running-hint")

    def update_progress(self, progress: AuditProgress) -> None:
        """Swap in a fresh progress snapshot (daemon-push entry point).

        Args:
            progress: The new per-check progress snapshot.
        """
        self.progress = progress

    def watch_progress(self) -> None:
        """Repaint the title tally + per-check rows on a new snapshot."""
        if self.is_mounted:
            self._repaint()

    def _repaint(self) -> None:
        """Rebuild the title + the per-check row block from the snapshot."""
        snapshot = self.progress
        self.query_one("#audit-running-title", Static).update(_title_text(snapshot))
        self.query_one("#audit-running-rows-inner", Static).update(_rows_text(snapshot))

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


def _rows_text(progress: AuditProgress | None) -> str:
    """Format the per-check row block (one ``<glyph>  <name>`` per line).

    Args:
        progress: The current snapshot, or ``None`` before one is seeded.

    Returns:
        The newline-joined check rows, or ``(no checks)`` when empty.
    """
    if progress is None or not progress.checks:
        return "(no checks)"
    return "\n".join(f"{_GLYPH[check.state]}  {check.name}" for check in progress.checks)


def open_audit_running(app: object, progress: AuditProgress) -> None:
    """Push the audit-running overlay onto *app*'s screen stack (cap-checked).

    Routes through the App's modal-cap-aware ``push_modal`` helper when
    present (so the modal-stack depth limit is enforced), falling back to a
    plain ``push_screen`` under a bare harness — mirroring the
    :func:`~eawf.tui_v2.screens.help.open_help` pattern. The ``/audit``
    palette path and the future ``audit_started`` daemon-push handler both
    call this.

    Args:
        app: The running App (typed loosely to avoid an import cycle with
            :mod:`eawf.tui_v2.app`).
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
