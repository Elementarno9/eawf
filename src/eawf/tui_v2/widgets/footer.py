"""``Footer`` + ``Heartbeat`` — shared chassis footer (C06 widget catalog).

Per the C06 brief §5.3 widget rows + Decision D3 (shared chassis): a
single footer composite reused by every per-scope screen
(``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``) with **no
per-scope duplication**. The footer carries:

* a context-aware key-hint strip using **full key names** only
  (``PageUp`` / ``PageDown`` / ``Enter`` / ``Esc`` — never ``PgUp``) per
  the operator keymap convention (D11), and
* a live :class:`Heartbeat` dot (D22) — a ``•`` pulse that proves the
  TUI is alive, ``accent``-coloured by default and ``err``-coloured when
  any pane is degraded, with a 0.5 s double-pulse ack on the ``r``
  force-refresh keypress.

Bundling the heartbeat inside the footer is the literal D3 trim: the
three scope screens reuse one :class:`Footer` (which owns the
:class:`Heartbeat`) rather than each re-declaring the chrome — the
``~5300 → ~2500`` salvageable-LOC target [2:122-126]. Colours resolve
against the ``theme.tcss`` palette vars (``$muted`` for the hints,
``$accent`` / ``$err`` for the heartbeat) — never hardcoded hex.

The heartbeat pulse runs off a Textual ``set_interval`` timer started on
mount; the host screen flips :attr:`Heartbeat.degraded` (wired to the
App's degraded reactive in a later wave) to swap the dot colour. The
pulse cadence + the visible/hidden toggle are pure-ish state on the
widget so a Pilot test can drive a tick and assert the dot.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

#: The heartbeat glyph — a single bullet that pulses on each tick.
HEARTBEAT_GLYPH: str = "•"

#: Heartbeat pulse cadence in seconds (the dot toggles visible/hidden on
#: this interval so the operator sees a steady blink).
HEARTBEAT_INTERVAL_S: float = 1.0

#: Default footer key hints (full key names per D11). Screens may pass a
#: scope-specific override via :meth:`Footer.set_hints`; this is the base
#: chrome shared by every scope.
DEFAULT_HINTS: tuple[str, ...] = (
    "↑↓ move",
    "Enter open",
    "/ palette",
    "? help",
    "q quit",
)


def format_hints(hints: tuple[str, ...]) -> str:
    """Join key hints into the footer strip with a separating bullet.

    Args:
        hints: The ordered key-hint fragments (full key names).

    Returns:
        The joined hint string, e.g. ``↑↓ move · Enter open · q quit``.
    """
    return "  ·  ".join(hints)


class Heartbeat(Static):
    """A pulsing ``•`` liveness dot (D22).

    Default ``accent`` colour; ``err`` colour when :attr:`degraded` is
    set. The dot toggles visible/hidden on :data:`HEARTBEAT_INTERVAL_S`
    so the operator sees a steady blink proving the render loop is live;
    :meth:`ack` fires a one-shot double-pulse for the ``r`` force-refresh
    acknowledgement.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Heartbeat {
        width: auto;
        height: 1;
        color: $accent;
    }
    """

    #: ``True`` when any pane is degraded — flips the dot to ``err``.
    degraded: reactive[bool] = reactive(False)

    #: Internal pulse phase; toggled by the timer so the dot blinks.
    _lit: reactive[bool] = reactive(True)

    def on_mount(self) -> None:
        """Start the pulse timer and paint the first lit frame."""
        self.set_interval(HEARTBEAT_INTERVAL_S, self._pulse)
        self._repaint()

    def _pulse(self) -> None:
        """Toggle the pulse phase on each timer tick."""
        self._lit = not self._lit

    def ack(self) -> None:
        """Force a lit frame as the ``r`` force-refresh acknowledgement.

        The full 0.5 s double-pulse animation lands with the force-
        refresh wiring; this guarantees a visible lit frame so the
        operator sees the manual-refresh ack immediately.
        """
        self._lit = True

    def watch_degraded(self, degraded: bool) -> None:
        """Swap the dot colour class when the degraded flag flips."""
        self.set_class(degraded, "-degraded")

    def watch__lit(self) -> None:
        """Repaint when the pulse phase toggles."""
        self._repaint()

    def _repaint(self) -> None:
        """Render the dot (or a blank cell when unlit)."""
        self.update(HEARTBEAT_GLYPH if self._lit else " ")


class Footer(Static):
    """Shared chassis footer: context key hints + a live heartbeat dot.

    Reused verbatim by every per-scope screen (D3 shared chassis). The
    footer composes a hint strip and a :class:`Heartbeat`; a host screen
    may override the hints via :meth:`set_hints` without touching the
    chrome. Standalone-testable via the Pilot harness.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Footer {
        height: 1;
        dock: bottom;
        background: $panel;
        padding: 0 1;
    }
    Footer > Horizontal {
        height: 1;
    }
    Footer .footer-hints {
        width: 1fr;
        height: 1;
    }
    """

    #: Active key hints, watched so a host override repaints the strip.
    hints: reactive[tuple[str, ...]] = reactive(DEFAULT_HINTS)

    def compose(self) -> ComposeResult:
        """Lay out the hint strip (left, flex) + heartbeat dot (right)."""
        with Horizontal():
            yield Static(format_hints(self.hints), classes="footer-hints")
            yield Heartbeat(id="heartbeat")

    def set_hints(self, hints: tuple[str, ...]) -> None:
        """Replace the footer key hints (scope-specific override).

        Args:
            hints: The ordered key-hint fragments (full key names).
        """
        self.hints = hints

    def watch_hints(self, hints: tuple[str, ...]) -> None:
        """Repaint the hint strip when the hints change.

        Guarded on mount: the child ``Static`` only exists after
        :meth:`compose`, so a pre-mount reactive assignment is a no-op
        (``compose`` reads the current value).
        """
        if not self.is_mounted:
            return
        self.query_one(".footer-hints", Static).update(format_hints(hints))


__all__ = [
    "DEFAULT_HINTS",
    "HEARTBEAT_GLYPH",
    "HEARTBEAT_INTERVAL_S",
    "Footer",
    "Heartbeat",
    "format_hints",
]
