"""``AuditFailedModal`` — mutating audit-failure overlay.

The audit-failure surface, and a **mutating** menu (picked over a
read-only overlay): auto-opened when the TUI receives an
``audit_completed`` event with ``verdict=fail`` for the active scope, it
surfaces the five repair actions — ``retry`` / ``split`` /
``land-partial`` / ``abandon`` / ``scope-change`` — each of which
dispatches a ``/flow`` worker via
``eawf agent dispatch <wave-id> --action <action>``. The modal carries a
single **status line** that streams
``dispatching <action> → <runtime> · attempt <n>`` while the dispatched
subagent runs, then ``closed`` on its ``agent_end`` return — keeping the
operator on the active audit without a second modal (no stack-depth
pressure against the cap of 3).

This is the only operator surface for mutating wave actions: the
``/wave`` palette verbs are read-only, so ``retry`` / ``abandon`` /
``split`` reach the operator **only** through this structured menu.

Above the menu the modal surfaces the **failing check** that tripped the
verdict — its name marked with the failed lifecycle sigil + the gate
chrome glyph — and a single ``evidence`` line carrying the structured
evidence summary (a gate's exit code, a claim's ``file:line`` citation),
NOT a raw stack trace. The operator therefore reads *which* check failed
and *why* in two lines before choosing a repair action, rather than
scrolling a dumped traceback.

This wave lands the **overlay**: the failing-check header + evidence line,
the five-row menu rendered for a failing wave, the ``↑`` / ``↓`` highlight,
the status-line render seam (:meth:`AuditFailedModal.mark_dispatching` /
:meth:`AuditFailedModal.mark_closed`), and the chosen-action result
returned through the dismiss value. Wiring the pick to the
``eawf agent dispatch`` CLI verb + driving the status line off the
subagent's event stream rides the wave that lands that CLI verb — it does
not exist yet — so the host shells out on the returned action and feeds
the status line from the dispatch events.

The status-line text is built by a pure helper
(:func:`format_dispatch_line`) so the wording is unit-testable without
mounting Textual; the modal is a thin view over the menu + the line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.sigils import Sigil

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailingCheck:
    """The audit check that tripped the fail verdict, plus its evidence.

    Carries the structured failure summary the modal surfaces above the
    repair menu so the operator reads *which* check failed and *why* before
    choosing an action. The evidence is a one-line summary (a gate's exit
    code, a claim's ``file:line`` citation), NOT a raw stack trace — the
    overlay deliberately renders a clean evidence line rather than a dumped
    traceback.

    Attributes:
        name: The failing check name (e.g. ``pytest_pass`` /
            ``coverage_min``).
        evidence: A one-line structured evidence summary (e.g.
            ``exit=1`` / ``src/eawf/foo.py:42 missing citation``). Empty
            when the daemon reported no evidence for the check.
    """

    name: str
    evidence: str = ""


#: The five mutating repair actions, in menu order. ``retry`` (index
#: ``0``) is the default highlight — the most common repair response.
_ACTIONS: tuple[str, ...] = (
    "retry",
    "split",
    "land-partial",
    "abandon",
    "scope-change",
)

#: One-line hint per action, shown beside the action name in the menu.
_ACTION_HINTS: dict[str, str] = {
    "retry": "re-dispatch the wave",
    "split": "split into smaller waves",
    "land-partial": "land what passed, defer the rest",
    "abandon": "abandon the wave",
    "scope-change": "revise the wave success criteria",
}


def format_dispatch_line(action: str, runtime: str, attempt: int) -> str:
    """Build the status line for an in-flight dispatch.

    The exact wording: ``dispatching <action> → <runtime> · attempt
    <n>`` (em-arrow + middle-dot separators).

    Args:
        action: The dispatched repair action (e.g. ``retry``).
        runtime: The runtime the subagent dispatched onto (e.g.
            ``claude-code``).
        attempt: The 1-based attempt counter for the wave.

    Returns:
        The formatted status line.
    """
    return f"dispatching {action} → {runtime} · attempt {attempt}"


class AuditFailedModal(ModalScreen[str]):
    """Mutating audit-failure menu (returns the chosen action on dismiss).

    ``↑`` / ``↓`` move the highlight across the five actions, ``Enter``
    confirms the highlighted action — its label is the dismiss value the
    host shells out to ``eawf agent dispatch <wave-id> --action <action>``
    — and ``Esc`` closes without dispatching. While a dispatch is in
    flight the host calls :meth:`mark_dispatching` to stream the status
    line, then :meth:`mark_closed` on the subagent's return.
    """

    DEFAULT_CSS: ClassVar[str] = """
    AuditFailedModal {
        align: center middle;
    }
    AuditFailedModal > #audit-failed-box {
        width: 70%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }
    AuditFailedModal .audit-failed-title {
        text-style: bold;
        color: $error;
        height: auto;
    }
    AuditFailedModal .audit-failed-check {
        color: $error;
        height: 1;
        margin-top: 1;
    }
    AuditFailedModal .audit-failed-evidence {
        color: $text-muted;
        height: auto;
    }
    AuditFailedModal #audit-failed-menu {
        height: auto;
        margin-top: 1;
    }
    AuditFailedModal .audit-failed-action {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    AuditFailedModal .audit-failed-action.-selected {
        color: $accent;
        text-style: bold reverse;
    }
    AuditFailedModal .audit-failed-status {
        height: 1;
        color: $accent;
        margin-top: 1;
    }
    AuditFailedModal .audit-failed-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``↑`` / ``↓`` move the highlight, ``Enter`` dispatches, ``Esc``
    #: closes. Vim ``j`` / ``k`` ride the arrows per the operator keymap.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "confirm", "dispatch", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index into :data:`_ACTIONS` of the highlighted action (``0`` =
    #: retry).
    selected: reactive[int] = reactive(0)

    def __init__(
        self,
        wave_id: str,
        runtime: str = "claude-code",
        *,
        failing_check: FailingCheck | None = None,
    ) -> None:
        """Construct the overlay for a failing wave.

        Args:
            wave_id: The wave whose audit failed (the dispatch target).
            runtime: The runtime the repair subagent will dispatch onto;
                rendered in the status line. Defaults to the canonical
                ``claude-code`` adapter id.
            failing_check: The check that tripped the fail verdict plus its
                one-line structured evidence summary. Surfaced above the
                repair menu so the operator reads which check failed and why
                before choosing an action. ``None`` (the
                event-stream-not-yet-wired path) renders no check header.
        """
        super().__init__()
        self._wave_id = wave_id
        self._runtime = runtime
        self._failing_check = failing_check

    def _render_mode(self) -> RenderMode:
        """Return the App's resolved render mode, defaulting when unbound.

        Reads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` so the
        failing-check sigil + gate-chrome glyphs pick the right column. A
        bare harness whose host App carries no ``render_mode`` (a direct
        construction outside the full app) falls back to the shared default.

        Returns:
            The active render mode (``"unicode"`` / ``"ascii"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def compose(self) -> ComposeResult:
        """Yield the title, failing-check header, menu, status line, hint.

        When a :class:`FailingCheck` is supplied the modal renders a
        ``<failed-sigil> <gate-glyph> <name>`` header line + a clean
        ``evidence: <summary>`` line above the menu (the structured failure,
        never a raw trace) so the operator reads which check failed and why
        before choosing a repair action.
        """
        mode = self._render_mode()
        with Vertical(id="audit-failed-box"):
            yield Static(f"audit failed: {self._wave_id}", classes="audit-failed-title")
            if self._failing_check is not None:
                fail_glyph = sigils.glyph(Sigil.FAILED, mode=mode)
                gate_glyph = sigils.chrome("gate", mode=mode)
                yield Static(
                    f"{fail_glyph} {gate_glyph} {self._failing_check.name}",
                    classes="audit-failed-check",
                )
                evidence = self._failing_check.evidence or "no evidence reported"
                yield Static(
                    f"evidence: {evidence}",
                    classes="audit-failed-evidence",
                )
            with Vertical(id="audit-failed-menu"):
                for index, action in enumerate(_ACTIONS):
                    hint = _ACTION_HINTS[action]
                    yield Static(
                        f"{action} — {hint}",
                        classes="audit-failed-action",
                        id=f"action-{index}",
                    )
            yield Static("", classes="audit-failed-status", id="audit-failed-status")
            yield Static(
                "[ ↑/↓ select · Enter dispatch · Esc close ]",
                classes="audit-failed-hint",
            )

    def on_mount(self) -> None:
        """Paint the initial highlight on the safe default (``retry``)."""
        self._repaint_actions()

    def watch_selected(self) -> None:
        """Repaint the action highlight when the selection moves."""
        if self.is_mounted:
            self._repaint_actions()

    def _repaint_actions(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted action."""
        for index in range(len(_ACTIONS)):
            cell = self.query_one(f"#action-{index}", Static)
            cell.set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, wrapping at the ends.

        Args:
            delta: ``-1`` for the previous action, ``+1`` for the next.
        """
        count = len(_ACTIONS)
        self.selected = (self.selected + delta) % count

    def mark_dispatching(self, action: str, attempt: int) -> None:
        """Render the in-flight status line for *action*.

        Called by the host while the dispatched repair subagent runs.

        Args:
            action: The dispatched action (e.g. ``retry``).
            attempt: The 1-based attempt counter for the wave.
        """
        line = format_dispatch_line(action, self._runtime, attempt)
        logger.info(f"audit_failed dispatching wave={self._wave_id} action={action!r}")
        self.query_one("#audit-failed-status", Static).update(line)

    def mark_closed(self) -> None:
        """Render the terminal ``closed`` status line.

        Called by the host on the dispatched subagent's ``agent_end``
        return.
        """
        self.query_one("#audit-failed-status", Static).update("closed")

    def action_confirm(self) -> None:
        """Dismiss with the highlighted action label (the dispatch target)."""
        action = _ACTIONS[self.selected]
        logger.info(f"audit_failed action={action!r} wave={self._wave_id}")
        self.dismiss(action)

    def action_close(self) -> None:
        """Dismiss with ``"close"`` (``Esc`` = close without dispatch)."""
        logger.info(f"audit_failed action='close' wave={self._wave_id}")
        self.dismiss("close")


def open_audit_failed(
    app: object,
    wave_id: str,
    runtime: str = "claude-code",
    *,
    failing_check: FailingCheck | None = None,
) -> None:
    """Push the audit-failed overlay onto *app*'s screen stack (cap-checked).

    Routes through the App's modal-cap-aware ``push_modal`` helper when
    present (so the modal-stack depth limit is enforced), falling back to a
    plain ``push_screen`` under a bare harness — mirroring the
    :func:`~eawf.surfaces.tui.screens.help.open_help` pattern. The future
    ``audit_completed`` (verdict=fail) daemon-push handler calls this;
    there is **no palette verb** for it — mutating wave actions reach the
    operator only through this structured menu.

    Args:
        app: The running App (typed loosely to avoid an import cycle with
            :mod:`eawf.surfaces.tui.app`).
        wave_id: The wave whose audit failed (the dispatch target).
        runtime: The runtime the repair subagent will dispatch onto.
        failing_check: The check that tripped the fail verdict plus its
            one-line evidence summary, surfaced above the repair menu.
    """
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(AuditFailedModal(wave_id, runtime=runtime, failing_check=failing_check))
        return
    push_screen = getattr(app, "push_screen", None)
    if callable(push_screen):
        push_screen(AuditFailedModal(wave_id, runtime=runtime, failing_check=failing_check))


__all__ = [
    "AuditFailedModal",
    "FailingCheck",
    "format_dispatch_line",
    "open_audit_failed",
]
