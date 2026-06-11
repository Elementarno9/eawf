"""``OutputTail`` -- the raw agent-output tail pane for the FA4 session zoom.

The typed lifecycle stream (``dispatch_cost`` / ``agent_end`` / ...) tells the
operator WHAT happened to a session; it never shows what the agent actually
SAYS. This pane closes that gap: it appends the spawned child's raw stdout
lines as they arrive and keeps the newest line in view (auto-scroll), so the
operator reads the agent's own words live rather than only its typed milestones.

Auto-scroll discipline
----------------------
New lines mount at the BOTTOM (a tail, not the newest-first event stream the
lifecycle panes use) and the pane scrolls to the end on each append, so a long
stream pins the latest output to the viewport bottom the way ``tail -f`` does.
The scroll is animation-free so a Pilot test reads the settled position
deterministically.

Stalled-stream honesty
----------------------
A freshly-mounted (or quiesced) tail that has received no output renders the
pinned literal :data:`WAITING_NOTICE` -- ``waiting for output...`` -- rather
than a frozen blank pane, so the operator can tell a stream that has not yet
spoken from one that has gone dead. The notice is removed the instant the first
real line lands and never reappears (a stream that stops mid-flight keeps its
last lines on screen, not the waiting notice).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from eawf.surfaces.tui.widgets.markup import escape_markup

logger = logging.getLogger(__name__)

#: Id of the pinned waiting notice shown before the first output line lands.
OUTPUT_TAIL_WAITING_ID: str = "output-tail-waiting"

#: CSS class on each rendered raw-output line row.
OUTPUT_TAIL_ROW_CLASS: str = "output-tail-row"

#: The pinned literal a stalled / not-yet-spoken stream renders rather than a
#: frozen blank pane. The trailing real ellipsis (U+2026) reads as "still
#: waiting" rather than a truncated sentence.
WAITING_NOTICE: str = "waiting for output…"


class OutputTail(VerticalScroll):
    """A ``tail -f`` view of a spawned agent's raw stdout lines.

    Appends each raw output line at the bottom (:meth:`append_line`) and
    auto-scrolls so the newest line stays in view. Before the first line lands
    it shows the pinned :data:`WAITING_NOTICE` so a not-yet-spoken (or stalled)
    stream is never a frozen blank pane; the notice is dropped on the first
    real line and never returns. Feed many lines at once through
    :meth:`extend` so a buffer replay is one scroll rather than N.
    """

    DEFAULT_CSS: ClassVar[str] = """
    OutputTail {
        height: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    OutputTail .output-tail-row {
        height: auto;
    }
    OutputTail #output-tail-waiting {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        """Build an empty tail pane.

        Args:
            id: Optional DOM id for the pane (the host zoom namespaces it).
        """
        super().__init__(id=id)
        self._has_output = False

    @property
    def has_output(self) -> bool:
        """Return whether at least one real output line has been appended.

        ``False`` while the pinned waiting notice is shown; flips to ``True``
        the instant the first line lands and stays ``True`` thereafter (a
        stalled stream keeps its last lines, never the waiting notice).
        """
        return self._has_output

    def compose(self) -> ComposeResult:
        """Yield the pinned waiting notice (the not-yet-spoken surface)."""
        yield Static(WAITING_NOTICE, id=OUTPUT_TAIL_WAITING_ID)

    def append_line(self, line: str) -> None:
        """Append one raw output *line* at the bottom and auto-scroll to it.

        Drops the pinned waiting notice on the first line (so a spoken stream
        no longer reads as waiting), mounts the line as a literal-escaped row
        at the bottom of the tail, and scrolls to the end so the newest line
        stays in view -- the ``tail -f`` behaviour. A no-op before mount.

        Args:
            line: The raw stdout line to render (escaped so a ``[`` in the
                agent's output never parses as a style tag).
        """
        if not self.is_mounted:
            return
        self._drop_waiting_notice()
        self.mount(Static(escape_markup(line), classes=OUTPUT_TAIL_ROW_CLASS))
        self._has_output = True
        self.scroll_end(animate=False)
        logger.debug(f"append_line len={len(line)}")

    def extend(self, lines: list[str]) -> None:
        """Append each line in *lines* in order, scrolling once at the end.

        The buffer-replay entry point: feeding a backlog line-by-line through
        :meth:`append_line` would scroll N times, so this drops the notice
        once, mounts every row, and scrolls to the end a single time. An empty
        *lines* leaves the pane (and its waiting notice) untouched. A no-op
        before mount.

        Args:
            lines: The raw stdout lines to append, oldest-first.
        """
        if not self.is_mounted or not lines:
            return
        self._drop_waiting_notice()
        for line in lines:
            self.mount(Static(escape_markup(line), classes=OUTPUT_TAIL_ROW_CLASS))
        self._has_output = True
        self.scroll_end(animate=False)
        logger.debug(f"extend count={len(lines)}")

    def _drop_waiting_notice(self) -> None:
        """Remove the pinned waiting notice if it is still mounted.

        Idempotent: the notice exists only before the first line, so a second
        call (the buffer replay then a live push) is a quiet no-op.
        """
        waiting = self.query(f"#{OUTPUT_TAIL_WAITING_ID}")
        if waiting:
            waiting.first().remove()


__all__ = [
    "OUTPUT_TAIL_ROW_CLASS",
    "OUTPUT_TAIL_WAITING_ID",
    "WAITING_NOTICE",
    "OutputTail",
]
