"""``Heartbeat`` — the pulsing ``•`` liveness dot.

A leaf :class:`~textual.widgets.Static` that pulses a single ``•`` glyph on
a fixed cadence so the operator has visual proof the render loop is live.
``accent``-coloured by default, ``err``-coloured when :attr:`degraded` is
set, with a one-shot lit-frame :meth:`ack` for the ``F5`` force-refresh
acknowledgement.

This is the single Heartbeat for the surface: the metrics / events overlays
embed it in their card chrome and the footer
(:mod:`eawf.tui_v2.widgets.footer`) imports it for the shared chassis footer
dot. Carrying the ``.-degraded`` colour rule in this class's ``DEFAULT_CSS``
keeps the degraded (red) dot self-contained wherever the widget is mounted,
independent of the app-level ``theme.tcss``.

The pulse runs off a Textual ``set_interval`` timer started on mount; the
visible/hidden toggle and the colour swap are pure-ish reactive state so a
Pilot test can drive a tick and assert the rendered dot without faking the
clock.
"""

from __future__ import annotations

from typing import ClassVar

from textual.reactive import reactive
from textual.widgets import Static

#: The heartbeat glyph — a single bullet that pulses on each tick.
HEARTBEAT_GLYPH: str = "•"

#: Heartbeat pulse cadence in seconds (the dot toggles visible/hidden on
#: this interval so the operator sees a steady blink).
HEARTBEAT_INTERVAL_S: float = 1.0


class Heartbeat(Static):
    """A pulsing ``•`` liveness dot, standalone leaf widget.

    Default ``accent`` colour; ``err`` colour when :attr:`degraded` is
    set. The dot toggles visible/hidden on :data:`HEARTBEAT_INTERVAL_S`
    so the operator sees a steady blink proving the render loop is live;
    :meth:`ack` forces a lit frame for the ``F5`` force-refresh
    acknowledgement. Construct it bare (``Heartbeat()``) and mount it in
    any modal chrome that wants a liveness indicator.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Heartbeat {
        width: auto;
        height: 1;
        color: $accent;
    }
    Heartbeat.-degraded {
        color: $error;
    }
    """

    #: ``True`` when any pane is degraded — flips the dot to the error
    #: colour. Watched so assignment repaints the colour class.
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

        The full 0.5 s double-pulse animation lands with the force-refresh
        wiring (§5.9); this guarantees a visible lit frame so the operator
        sees the manual-refresh ack immediately.
        """
        self._lit = True

    def watch_degraded(self, degraded: bool) -> None:
        """Swap the dot colour class when the degraded flag flips.

        Args:
            degraded: ``True`` when any pane is degraded.
        """
        self.set_class(degraded, "-degraded")

    def watch__lit(self) -> None:
        """Repaint when the pulse phase toggles."""
        self._repaint()

    def _repaint(self) -> None:
        """Render the dot (or a blank cell when unlit)."""
        self.update(HEARTBEAT_GLYPH if self._lit else " ")


__all__ = [
    "HEARTBEAT_GLYPH",
    "HEARTBEAT_INTERVAL_S",
    "Heartbeat",
]
