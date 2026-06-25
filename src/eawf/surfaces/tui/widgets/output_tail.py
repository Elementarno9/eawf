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

#: The codex ``exec --json`` top-level event types the formatter recognises. A
#: line whose ``type`` is outside this set is not a codex event (plain text, or
#: another runtime's stream not yet modelled) and passes through verbatim rather
#: than being dropped.
_CODEX_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "item.started",
        "item.completed",
        "error",
    }
)


def _format_codex_event(event: dict[str, object]) -> list[str] | None:
    """Extract the readable tail lines from one codex ``exec --json`` event.

    Returns the human-readable lines for a recognised codex event (the
    assistant message text, or a run command with its output), an empty list
    for a recognised-but-bodyless structural frame (``thread.started`` /
    ``turn.*`` / ``item.started``), or ``None`` when the event is NOT a codex
    event so the caller can pass the raw line through unmodified.

    Args:
        event: One decoded JSON event object off the agent's stdout stream.

    Returns:
        The readable lines, ``[]`` for a structural frame, or ``None`` to
        signal "not a codex event -- render raw".
    """
    etype = event.get("type")
    if not isinstance(etype, str) or etype not in _CODEX_EVENT_TYPES:
        return None
    if etype == "error":
        err = event.get("error")
        message = err.get("message") if isinstance(err, dict) else event.get("message")
        return [f"error: {message}"] if isinstance(message, str) and message else []
    if etype != "item.completed":
        # A structural frame (turn / thread / item.started) carries no final
        # readable body; item.completed is where the message + output land.
        return []
    item = event.get("item")
    if not isinstance(item, dict):
        return []
    itype = item.get("type")
    if itype == "agent_message":
        text = item.get("text")
        return text.split("\n") if isinstance(text, str) and text else []
    if itype == "command_execution":
        lines: list[str] = []
        command = item.get("command")
        if isinstance(command, str) and command:
            lines.append(f"$ {command}")
        output = item.get("aggregated_output")
        if isinstance(output, str) and output:
            lines.extend(output.rstrip("\n").split("\n"))
        return lines
    # reasoning / any other completed item carries no operator-facing body.
    return []


def format_agent_output_lines(joined: str) -> list[str]:
    """Render a raw agent output chunk into readable tail lines.

    A spawned runtime streams its stdout as a JSON event log (codex
    ``exec --json`` emits one ``{"type": ...}`` envelope per line). Dumped
    verbatim the tail reads as JSONL noise instead of the agent's words. Parse
    each line and surface the human-readable content -- assistant messages and
    the commands the agent ran with their output -- dropping the structural
    frames. A line that is not a recognised event (plain text, malformed JSON,
    or another runtime's not-yet-modelled format) passes through verbatim, so
    nothing the agent emitted is ever swallowed.

    Args:
        joined: One persisted output chunk -- newline-joined raw stdout.

    Returns:
        The readable tail lines for *joined*, in stream order.
    """
    import orjson

    out: list[str] = []
    for raw in joined.split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            event = orjson.loads(stripped)
        except orjson.JSONDecodeError:
            out.append(raw)
            continue
        formatted = _format_codex_event(event) if isinstance(event, dict) else None
        if formatted is None:
            out.append(raw)
        else:
            out.extend(formatted)
    return out


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
        border: round $accent;
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
