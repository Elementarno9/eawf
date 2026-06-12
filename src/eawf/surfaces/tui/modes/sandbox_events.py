"""``SandboxEventsModeScreen`` -- the U6 sandbox-enforcement timeline (mode digit 9).

The Sandbox-events mode renders the spawn-safety floor's denial timeline:
the rows the floor wrote to the canonical event feed when it refused
something (an ``argv-deny`` head, an ``egress-block`` host, an ``env-scrub``
credential drop, a ``cwd-guard`` escape). Each row leads with a severity
sigil -- the hard-deny cross for a ``block`` decision, the warn triangle for
a ``warn`` / ``info`` one -- then the wall-clock time, the spawning session,
and the denied target, so the operator reads at a glance what the floor
stopped and why.

Data source
-----------
The floor persists each enforcement decision to the on-disk event store
(``<state_dir>/store/event.jsonl``) as a ``StoreKind.EVENT`` envelope whose
``event_type`` is ``sandbox.enforcement.<kind>`` and whose ``payload.extras``
carries the five named fields (``ts`` / ``session`` / ``kind`` / ``target``
/ ``severity``) -- see
:func:`eawf.runtime.daemon.dispatch_runner.persist_enforcement_event`. This
pane reads that store read-only on mount (and on a poll-backstop tick),
filters to the enforcement rows, and renders the tail newest-first. It never
mutates the file.

Honest empty
------------
A spawn floor that refused nothing wrote no enforcement rows, so the pane
shows the pinned honest-empty notice ":data:`EMPTY_NOTICE`" -- "no sandbox
events were denied" -- rather than implying a denial stream is flowing. The
notice text is byte-for-byte fixed because the floor's whole value is that
the timeline IS empty in the happy path.

Cosmic-terminal reskin
----------------------
The severity column resolves through the shared
:func:`~eawf.surfaces.tui.widgets.sigils.enforcement_sigil` home (no glyph is
invented here): a ``block`` decision wears the hard-deny cross tinted the
failed hue, a ``warn`` / ``info`` decision the warn triangle. The row
formatter (:func:`format_enforcement_row`) is pure (markup-free, layout only)
so the sigil / time / session / target column layout is unit-testable without
mounting Textual; the tinted markup is composed at mount time by
:func:`format_enforcement_markup`. The sigil column repaints on a unicode
<-> ASCII render-mode flip, like every other reskinned pane.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

import orjson
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.empty_state import (
    SEAL_HERO_ID,
    render_empty_state,
    seal_empty_hero,
    seal_hero_css,
)
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome, enforcement_sigil

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Id of the scrollable timeline list container.
TIMELINE_LIST_ID: str = "sandbox-events-list"

#: Id of the honest-empty notice shown when the floor refused nothing (no
#: enforcement row exists). Removed once the first row lands.
TIMELINE_EMPTY_ID: str = "sandbox-events-empty"

#: CSS class on each rendered enforcement row.
TIMELINE_ROW_CLASS: str = "sandbox-events-row"

#: CSS class added to the row the selection cursor currently rests on (the
#: ``Enter``-open target). Exactly one mounted row carries it when the timeline
#: is populated; a render-mode flip and a selection move both keep it on the
#: cursor row so the operator sees which denial ``Enter`` would open.
TIMELINE_SELECTED_CLASS: str = "sandbox-events-selected"

#: Id of the per-row peek modal ``Enter`` opens over the selected denial. The
#: peek surfaces the row's five named fields (time / session / kind / target /
#: severity) full-width so a long denied target that the timeline column
#: truncates is readable in full.
ROW_PEEK_ID: str = "sandbox-events-peek"

#: The pinned honest-empty notice. Byte-for-byte fixed -- the floor's whole
#: value is that the denial timeline IS empty in the happy path, so the empty
#: surface states it plainly rather than implying a stream is flowing. The
#: dash is a REAL em-dash (U+2014); the literal is pinned exactly by the wave
#: success criterion and the mode test.
EMPTY_NOTICE: str = "no sandbox events — nothing was denied"

#: The ``event_type`` prefix every persisted enforcement row carries
#: (``sandbox.enforcement.argv-deny`` etc.). The on-disk read filters the
#: event store down to rows whose type starts with this stem, so a non-
#: enforcement event in the same feed is skipped.
_ENFORCEMENT_TYPE_PREFIX: str = "sandbox.enforcement."

#: The four enforcement kinds the floor records, in
#: :func:`~eawf.runtime.daemon.dispatch_runner.persist_enforcement_event`
#: order. Pinned here so a row whose ``kind`` extra is absent / unknown reads
#: as a literal-empty kind rather than guessing one.
ENFORCEMENT_KINDS: tuple[str, ...] = ("argv-deny", "egress-block", "env-scrub", "cwd-guard")

#: The ring-buffer cap -- the timeline shows at most this many enforcement
#: rows, newest first. A spawn fleet that denies more than this scrolls the
#: oldest off the visible tail.
TIMELINE_RING_SIZE: int = 200

#: The single UTC timestamp format every row is normalised to. The event
#: store mixes ISO-8601 spellings; collapsing them to one fixed-width form
#: keeps the rendered columns aligned. Mirrors the ``/events`` overlay form.
_TS_FORMAT: str = "%H:%M:%S"

#: Width the wall-clock column is laid out to (``HH:MM:SS`` is eight cells).
_TS_WIDTH: int = 8

#: Width the session column is padded to so the trailing denied-target lines
#: up across rows. The executor session id (``EX-<wave>-<attempt>`` form) is
#: bounded well under this; pin a width so a short session keeps the target
#: column left-aligned.
_SESSION_WIDTH: int = 20

#: Width the kind column is padded to so the denied target lines up. The
#: longest :data:`ENFORCEMENT_KINDS` value is ``egress-block`` (12).
_KIND_WIDTH: int = 12


@dataclass(frozen=True)
class EnforcementRow:
    """One rendered enforcement-timeline row (a flattened enforcement event).

    Attributes:
        timestamp: The enforcement wall-clock time, normalised to
            :data:`_TS_FORMAT` (``HH:MM:SS``) by :func:`_row_from_record`.
        session: The spawning session the enforcement applied to.
        kind: The enforcement boundary that fired -- one of
            :data:`ENFORCEMENT_KINDS`.
        target: The thing the floor refused / scrubbed (a host:port, an argv
            head, a variable name, a cwd).
        severity: The decision severity (``block`` hard-deny, ``warn`` /
            ``info`` degraded-but-continued).
    """

    timestamp: str
    session: str
    kind: str
    target: str
    severity: str


def _normalize_timestamp(raw: str) -> str:
    """Normalise an ISO-8601 timestamp string to one ``HH:MM:SS`` UTC form.

    Parses the trailing-``Z`` and ``+00:00`` ISO spellings (with or without
    fractional seconds), converts to UTC, and reformats to second precision.
    An empty or unparseable value is returned unchanged so a malformed row
    still renders (the width-pad keeps columns aligned).

    Args:
        raw: The raw timestamp string from the event extras.

    Returns:
        The ``HH:MM:SS`` form, or *raw* when it cannot be parsed.
    """
    if not raw:
        return raw
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return raw
    return parsed.astimezone(UTC).strftime(_TS_FORMAT)


def _row_from_record(record: dict[str, object]) -> EnforcementRow | None:
    """Flatten a raw event-store JSONL record into an :class:`EnforcementRow`.

    Returns ``None`` for any record that is not a sandbox-enforcement event
    (a non-dict payload, a non-enforcement ``event_type``, or a missing
    ``extras`` map), so a single non-enforcement or malformed row never blanks
    the timeline. The five named fields are read off ``payload.extras`` -- the
    flat map the floor persists them in.

    Args:
        record: A decoded JSONL envelope dict.

    Returns:
        The flattened enforcement row, or ``None`` when the record is not a
        renderable enforcement event.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event_type", ""))
    if not event_type.startswith(_ENFORCEMENT_TYPE_PREFIX):
        return None
    extras = payload.get("extras")
    if not isinstance(extras, dict):
        return None
    return EnforcementRow(
        timestamp=_normalize_timestamp(str(extras.get("ts", ""))),
        session=str(extras.get("session", "")),
        kind=str(extras.get("kind", "")),
        target=str(extras.get("target", "")),
        severity=str(extras.get("severity", "")),
    )


def load_enforcement_rows(
    event_path: Path | None,
    limit: int = TIMELINE_RING_SIZE,
) -> tuple[EnforcementRow, ...]:
    """Read the sandbox-enforcement tail of the event store, newest first.

    Reads at most *limit* enforcement rows from ``event.jsonl``, newest first.
    The read is total: a missing / unreadable file yields an empty tuple, a
    non-enforcement or malformed line is skipped rather than aborting the
    read, so the timeline degrades to the honest-empty notice instead of
    crashing on a fresh or partially written store. Never mutates the file.

    Args:
        event_path: Path to ``<state_dir>/store/event.jsonl``, or ``None``
            when no scope state is resolved.
        limit: The ring-buffer cap -- the most recent *limit* enforcement
            rows.

    Returns:
        The most recent enforcement rows, newest first (empty when none
        readable).
    """
    if event_path is None or not event_path.is_file():
        return ()
    try:
        raw = event_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"load_enforcement_rows path={event_path!s} unreadable cause={exc!r}")
        return ()
    rows: list[EnforcementRow] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = orjson.loads(stripped)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        row = _row_from_record(record)
        if row is not None:
            rows.append(row)
    rows.reverse()
    return tuple(rows[:limit])


def format_enforcement_row(row: EnforcementRow) -> str:
    """Render one enforcement *row* as a single timeline line (markup-free).

    Layout is ``<sigil> <HH:MM:SS> <session> <kind> <target>``: a two-cell
    leading severity-sigil column (the sigil glyph plus one trailing space),
    the enforcement wall-clock time, the spawning session padded to
    :data:`_SESSION_WIDTH`, the enforcement kind padded to :data:`_KIND_WIDTH`,
    then the denied target. The sigil resolves in the unicode column (the
    markup form swaps to ASCII when the App's render mode is ``ascii``).

    Args:
        row: The enforcement row to render.

    Returns:
        A plain (markup-free) single-line string for one
        :class:`~textual.widgets.Static`.
    """
    sigil = enforcement_sigil(row.severity).render(mode=DEFAULT_RENDER_MODE)
    return (
        f"{sigil} {row.timestamp:<{_TS_WIDTH}}  "
        f"{row.session:<{_SESSION_WIDTH}}  {row.kind:<{_KIND_WIDTH}}  {row.target}"
    )


def format_enforcement_markup(row: EnforcementRow, *, mode: RenderMode) -> str:
    """Return the tinted content markup for one timeline row in render *mode*.

    Composes the severity sigil's SHAPE + COLOUR (via
    :func:`~eawf.surfaces.tui.widgets.sigils.enforcement_sigil`) so the leading
    two-cell column reads as a tinted severity mark -- the hard-deny cross for
    a ``block`` decision, the warn triangle for a ``warn`` / ``info`` one --
    then escapes the time / session / kind / target tail so an arbitrary
    target (which may carry literal ``[`` brackets) renders verbatim through
    Textual's content-markup parser. The column layout mirrors
    :func:`format_enforcement_row`.

    Args:
        row: The enforcement row to render.
        mode: The App's resolved render-mode label -- ``"ascii"`` selects the
            ASCII sigil column, any other value the unicode column.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    resolved = enforcement_sigil(row.severity)
    mark = escape_markup(resolved.render(mode=mode))
    mark_cell = f"[{resolved.tint_hex}]{mark}[/]" if resolved.tint_hex else mark
    tail = (
        f"{row.timestamp:<{_TS_WIDTH}}  "
        f"{row.session:<{_SESSION_WIDTH}}  {row.kind:<{_KIND_WIDTH}}  {row.target}"
    )
    return f"{mark_cell} {escape_markup(tail)}"


def format_enforcement_peek(row: EnforcementRow) -> str:
    """Render the selected *row*'s five named fields as a peek body (markup-free).

    The ``Enter``-open peek lays the timeline row's five fields out one per
    line (``time`` / ``session`` / ``kind`` / ``target`` / ``severity``) so a
    long denied target the single-line timeline column truncates is readable in
    full. Pure (markup-free, no Textual primitive) so the field layout is
    unit-testable without mounting the modal.

    Args:
        row: The enforcement row the selection cursor rests on.

    Returns:
        A plain (markup-free) multi-line body, one ``label: value`` per field.
    """
    return "\n".join(
        (
            f"time:     {row.timestamp}",
            f"session:  {row.session}",
            f"kind:     {row.kind}",
            f"target:   {row.target}",
            f"severity: {row.severity}",
        )
    )


class SandboxRowPeekModal(ModalScreen[None]):
    """A transient peek over one selected enforcement row (``Enter`` opens it).

    Surfaces the selected denial's five named fields full-width
    (:func:`format_enforcement_peek`) so a long denied target the single-line
    timeline column truncates is readable in full. The modal owns no mutation
    -- it is a read-only detail peek built from a pre-resolved
    :class:`EnforcementRow`, so it never reaches back into the timeline or the
    event store. ``Esc`` closes.
    """

    DEFAULT_CSS: ClassVar[str] = """
    SandboxRowPeekModal {
        align: center middle;
    }
    SandboxRowPeekModal > #sandbox-events-peek-card {
        width: 70%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    SandboxRowPeekModal .sandbox-events-peek-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    SandboxRowPeekModal .sandbox-events-peek-body {
        height: auto;
        margin-top: 1;
    }
    SandboxRowPeekModal .sandbox-events-peek-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Esc`` closes the peek -- the only binding the transient owns. The
    #: chassis keys come from the base; the peek adds nothing else.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, row: EnforcementRow, *, mode: RenderMode) -> None:
        """Construct the peek over a pre-resolved *row* in render *mode*.

        Args:
            row: The selected enforcement row to surface full-width.
            mode: The App's resolved render-mode label, threaded into the
                severity sigil + the title chrome mark.
        """
        super().__init__(id=ROW_PEEK_ID)
        self._row = row
        self._mode = mode

    def compose(self) -> ComposeResult:
        """Yield the titled card: severity sigil heading, field body, Esc hint."""
        with VerticalScroll(id="sandbox-events-peek-card"):
            yield Static(self._title(), classes="sandbox-events-peek-title")
            yield Static(format_enforcement_peek(self._row), classes="sandbox-events-peek-body")
            yield Static("[ Esc to close ]", classes="sandbox-events-peek-hint")

    def _title(self) -> str:
        """Return the heading: the severity sigil + the denial's kind + session."""
        resolved = enforcement_sigil(self._row.severity)
        mark = escape_markup(resolved.render(mode=self._mode))
        mark_cell = f"[{resolved.tint_hex}]{mark}[/]" if resolved.tint_hex else mark
        gate = chrome("gate", mode=self._mode)
        title = escape_markup(f"{gate} {self._row.kind} · {self._row.session}")
        return f"{mark_cell} {title}"

    def action_close(self) -> None:
        """Dismiss the peek (``Esc``)."""
        self.dismiss(None)


class SandboxEventsModeScreen(ScopeScreen):
    """Standalone pane showing the spawn-floor denial timeline, newest-first.

    Reads the on-disk event store read-only (the floor persists each
    enforcement decision there) and renders the enforcement tail. When the
    floor refused nothing the pane shows the pinned honest-empty notice rather
    than implying a denial stream is flowing. A render-mode flip repaints the
    severity-sigil column in place; a poll-backstop tick re-reads the store so
    a denial recorded after mount surfaces without a restart.
    """

    DEFAULT_CSS: ClassVar[str] = f"""
    SandboxEventsModeScreen #sandbox-events-body {{
        height: 1fr;
        padding: 1 2;
    }}
    SandboxEventsModeScreen #sandbox-events-list {{
        height: 1fr;
        border: round $accent;
    }}
    SandboxEventsModeScreen .sandbox-events-empty {{
        color: $text-muted;
        height: auto;
        width: 1fr;
        text-align: center;
    }}
    SandboxEventsModeScreen .sandbox-events-row {{
        height: auto;
    }}
    SandboxEventsModeScreen .sandbox-events-selected {{
        background: $boost;
        text-style: bold;
    }}
    {seal_hero_css("SandboxEventsModeScreen")}
    """

    #: ``up`` / ``down`` move the selection cursor (and scroll it into view);
    #: ``Enter`` OPENS the selected denial in a full-field peek -- matching the
    #: advertised ``Enter open`` footer affordance; ``r`` re-reads the store (so
    #: a just-recorded denial surfaces); ``g`` jumps the cursor to the newest
    #: row (the top). The chassis bindings (palette / help / quit / scope / mode
    #: digits) come from the shared base + app-wide bindings. ``g`` keeps the
    #: vim "go-to-top" meaning here -- the timeline is newest-first so the top
    #: is the latest denial.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", "up", show=False),
        Binding("down", "scroll_down", "down", show=False),
        Binding("enter", "open_selected", "open", show=False),
        Binding("r", "reload", "reload", show=False),
        Binding("g", "scroll_home", "top", show=False),
    ]

    #: Index of the row the selection cursor rests on (the ``Enter``-open
    #: target). Clamped to the loaded-row list -- ``0`` on the newest row when
    #: the timeline is populated, ``-1`` when the floor refused nothing.
    selected: reactive[int] = reactive(0, init=False)

    #: The loaded enforcement tail, newest-first -- the row backing for the
    #: selection cursor + the ``Enter`` peek. Seeded by :meth:`_reload_rows`;
    #: an empty tuple before mount / on the honest-empty timeline.
    _rows: tuple[EnforcementRow, ...] = ()

    #: Footer hints for the sandbox-events timeline (arrows primary per the
    #: keymap convention). Every advertised label is produced through
    #: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
    #: tokens stay pinned to the canonical vocabulary; the ``g`` jump-to-top
    #: binding is a live affordance the mode test pins, but it is not
    #: advertised here because ``g`` is not a canonical footer token. The mode
    #: digits are surfaced by the always-visible mode row, not duplicated here.
    FOOTER_HINTS: ClassVar[tuple[str, ...]] = (
        render_hint_label("↑↓", "select"),
        render_hint_label("Enter", "open"),
        render_hint_label("w/r/u", "scope"),
        render_hint_label("/", "palette"),
        render_hint_label("?", "help"),
        render_hint_label("q", "quit"),
    )

    def compose_body(self) -> ComposeResult:
        """Yield the scrollable timeline body (honest-empty until denials land).

        The list starts with a single honest-empty notice -- in the unicode
        render mode the pinned :data:`EMPTY_NOTICE` headline leads with the
        centered ASCII-art Seal (the research-board / W30 brand-mark pattern),
        so the safety pane's happy-path empty reads as the same calm centered
        hero the other modes render rather than a bare small brand glyph;
        :meth:`on_mount` seeds the persisted enforcement rows, removing the
        notice once the first row lands.
        """
        with Vertical(id="sandbox-events-body"), VerticalScroll(id=TIMELINE_LIST_ID):
            yield self._empty_widget(hero_id=TIMELINE_EMPTY_ID)

    def on_mount(self) -> None:
        """Seed the timeline from the on-disk event store, newest-first.

        Calls the base chassis mount (footer hints) first, then reads the
        persisted enforcement rows and mounts them, and wires the render-mode
        watcher so a unicode <-> ASCII flip repaints the severity column.
        """
        super().on_mount()
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._reload_rows()

    def action_reload(self) -> None:
        """Re-read the event store so a just-recorded denial surfaces (``r``)."""
        self._reload_rows()

    def action_scroll_up(self) -> None:
        """Move the selection cursor up one row, scrolling it into view (``up``).

        The cursor clamps at the newest row (index ``0``); the move repaints
        the selection highlight (:meth:`watch_selected`) so the operator sees
        which denial ``Enter`` would open. A no-op on the honest-empty timeline
        (no row to select). The action name stays ``scroll_up`` so the
        advertised ``up`` affordance resolves unchanged.
        """
        if self._rows:
            self.selected = max(0, self.selected - 1)

    def action_scroll_down(self) -> None:
        """Move the selection cursor down one row, scrolling it into view (``down``).

        The cursor clamps at the oldest loaded row; the move repaints the
        selection highlight. A no-op on the honest-empty timeline. The action
        name stays ``scroll_down`` so the advertised ``down`` affordance
        resolves unchanged.
        """
        if self._rows:
            self.selected = min(len(self._rows) - 1, self.selected + 1)

    def action_scroll_home(self) -> None:
        """Jump the selection cursor to the newest enforcement row, the top (``g``)."""
        if self._rows:
            self.selected = 0
        self.query_one(f"#{TIMELINE_LIST_ID}", VerticalScroll).scroll_home()

    def action_open_selected(self) -> None:
        """Open the selected denial in a full-field peek modal (``Enter``).

        Resolves the cursor row to its :class:`EnforcementRow` and pushes the
        read-only :class:`SandboxRowPeekModal` through the App's cap-aware
        ``push_modal`` (falling back to ``push_screen`` under a bare harness so
        the push never raises). A no-op when the floor refused nothing -- the
        binding still resolves, so the advertised ``Enter open`` affordance is
        never a dead click.
        """
        row = self._selected_row()
        if row is None:
            logger.info("sandbox_events_open skipped reason=no-selection")
            return
        modal = SandboxRowPeekModal(row, mode=self._render_mode())
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            push_modal(modal)
        else:
            self.app.push_screen(modal)
        logger.info(f"sandbox_events_open kind={row.kind} session={row.session!r}")

    def _reload_rows(self) -> None:
        """Re-read the enforcement tail and rebuild the timeline rows.

        Clears the list, then either mounts one row per persisted enforcement
        event (newest-first) or the honest-empty notice when the floor refused
        nothing. The shared rebuild path so the on-mount seed and the ``r``
        reload render identically. Stashes the loaded rows + resets the
        selection cursor to the newest row (or ``-1`` on the honest-empty
        timeline) so ``Enter`` opens a real denial.
        """
        listing = self.query_one(f"#{TIMELINE_LIST_ID}", VerticalScroll)
        listing.remove_children()
        rows = load_enforcement_rows(self._event_path())
        self._rows = rows
        self.set_reactive(type(self).selected, 0 if rows else -1)
        if not rows:
            # The honest-empty hero carries no id on a rebuild: the initial
            # compose's hero (which DOES carry the id) was just removed, but
            # Textual defers that removal, so re-using the id here would race a
            # DuplicateIds. The class is enough for styling + the test probe.
            listing.mount(self._empty_widget(hero_id=None))
            return
        mode = self._render_mode()
        widgets = [self._build_row(row, index=index, mode=mode) for index, row in enumerate(rows)]
        listing.mount_all(widgets)

    def _build_row(self, row: EnforcementRow, *, index: int, mode: RenderMode) -> Static:
        """Build one timeline :class:`~textual.widgets.Static` for *row*.

        Stashes the source row on the widget so a render-mode flip can repaint
        the tinted severity column in place without re-reading the store, and
        adds the selected-row highlight class to the cursor row so the
        ``Enter``-open target is visible.

        Args:
            row: The enforcement row to render.
            index: The row's position in the loaded tail (newest is ``0``),
                compared against :attr:`selected` to mark the cursor row.
            mode: The App's resolved render-mode label.

        Returns:
            The composed row widget.
        """
        classes = TIMELINE_ROW_CLASS
        if index == self.selected:
            classes = f"{TIMELINE_ROW_CLASS} {TIMELINE_SELECTED_CLASS}"
        widget = Static(format_enforcement_markup(row, mode=mode), classes=classes)
        widget._enforcement_row = row  # type: ignore[attr-defined]
        return widget

    def watch_selected(self) -> None:
        """Move the selection highlight + scroll the cursor row into view.

        Repaints the selected-row class onto the cursor row (and off the rest)
        and scrolls the mounted cursor widget visible, so an ``up`` / ``down``
        move keeps the ``Enter``-open target on screen. A no-op before mount
        (the rows are not yet laid out).
        """
        if not self.is_mounted:
            return
        widgets = list(self.query(f".{TIMELINE_ROW_CLASS}").results(Static))
        for index, widget in enumerate(widgets):
            widget.set_class(index == self.selected, TIMELINE_SELECTED_CLASS)
        if 0 <= self.selected < len(widgets):
            widgets[self.selected].scroll_visible()

    def _selected_row(self) -> EnforcementRow | None:
        """Return the row the selection cursor rests on, or ``None`` when empty.

        Returns:
            The selected :class:`EnforcementRow`, or ``None`` when the timeline
            is honest-empty / the cursor is out of range.
        """
        if not self._rows or not 0 <= self.selected < len(self._rows):
            return None
        return self._rows[self.selected]

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint every mounted row when the App's render mode flips.

        Re-renders each row's tinted markup from its stashed source row so a
        unicode <-> ASCII flip swaps the severity column in place. A no-op
        before mount (the watcher is wired in :meth:`on_mount`).
        """
        if not self.is_mounted:
            return
        mode = self._render_mode()
        for widget in self.query(f".{TIMELINE_ROW_CLASS}").results(Static):
            row = getattr(widget, "_enforcement_row", None)
            if row is not None:
                widget.update(format_enforcement_markup(row, mode=mode))

    def _render_mode(self) -> RenderMode:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the sigil
        helpers so an ``ascii`` flip swaps every row's severity column to its
        ASCII glyph; falls back to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` under a
        bare test harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The render-mode label (``"ascii"`` or ``"unicode"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _empty_widget(self, *, hero_id: str | None) -> Static | Vertical:
        """Build the honest-empty widget: a seal-led hero (unicode) or bare body.

        In the unicode render mode the pinned :data:`EMPTY_NOTICE` good-state
        copy leads with the centered ASCII-art Seal (the research-board / W30
        brand-mark pattern), so the sandbox happy-path empty reads as the same
        calm centered hero the autopilot / feed / agent-watch / evidence modes
        render. The body Static drops its own brand glyph in that path (the seal
        art is the single brand mark) and keeps the ``sandbox-events-empty``
        class + the timeline-empty id the mode test + the rebuild path probe; the
        :func:`~eawf.surfaces.tui.widgets.empty_state.seal_empty_hero` wrapper
        opens the operator-flagged blank gap between the seal and the headline
        and centers the seal + gap + copy block vertically.

        The ASCII render mode keeps the bare body Static (the small unicode /
        ascii brand glyph leads it) because the half-block art needs block-glyph
        coverage the ascii column does not carry.

        Args:
            hero_id: The id stamped on the body Static (and threaded to the seal
                wrapper). :data:`TIMELINE_EMPTY_ID` on the initial compose so the
                first row's seed can remove it; ``None`` on a ``r``-reload
                rebuild, where Textual's deferred removal of the prior id'd hero
                would race a ``DuplicateIds`` if this one re-used the id.

        Returns:
            The seal-led hero ``Vertical`` (unicode) or the bare body ``Static``
            (ascii), ready to ``yield`` or ``mount``.
        """
        mode = self._render_mode()
        unicode = mode != "ascii"
        body = Static(
            self._empty_hero(with_sigil=not unicode),
            id=hero_id,
            classes="sandbox-events-empty",
        )
        if unicode:
            # The seal wrapper carries its own id (distinct from the body id the
            # mode test queries by class) so it tears down cleanly on a rebuild;
            # `hero_id=None` (the reload path) leaves it id-less to dodge the
            # deferred-removal DuplicateIds race the timeline list hits.
            wrapper_id = SEAL_HERO_ID if hero_id is not None else None
            return seal_empty_hero(body, hero_id=wrapper_id)
        return body

    def _empty_hero(self, *, with_sigil: bool = True) -> str:
        """Return the centered honest-empty hero body for the timeline.

        Routes the :data:`EMPTY_NOTICE` "nothing was denied" copy through the
        shared :func:`~eawf.surfaces.tui.widgets.empty_state.render_empty_state`
        hero so the safety pane reads as a calm, centered good-state (a muted
        brand sigil over a ``$muted`` headline -- *not* a ``$warn`` alert, since
        no denial is the desired state) rather than a top-left one-liner.

        Args:
            with_sigil: When ``True`` the body leads with the small muted brand
                glyph (the ascii honest-empty path); ``False`` drops it so the
                ASCII-art Seal the hero wrapper mounts is the single brand mark
                (the unicode seal-led path).

        Returns:
            The newline-joined content-markup body for the empty-state Static.
        """
        return render_empty_state(
            EMPTY_NOTICE, mode=self._render_mode(), headline_tint="$muted", sigil=with_sigil
        )

    def _event_path(self) -> Path | None:
        """Resolve the host App's read-only ``event.jsonl`` path, if configured.

        Derives the canonical event-store path from the App's bound
        ``state.json`` path via
        :func:`eawf.kernel.store.paths.store_path`, so the timeline reads the
        same feed the floor persisted to. A bare test harness whose host App
        carries no ``_state_path`` (or one whose path is unset) yields ``None``,
        so the pane shows the honest-empty notice rather than reaching off a
        missing store.

        Returns:
            The ``<state_dir>/store/event.jsonl`` path, or ``None`` when no
            state path is bound.
        """
        from pathlib import Path

        from eawf.kernel.state.enums import StoreKind
        from eawf.kernel.store.paths import store_path

        state_path = getattr(self.app, "_state_path", None)
        if not isinstance(state_path, Path):
            return None
        return store_path(state_path, StoreKind.EVENT)


__all__ = [
    "EMPTY_NOTICE",
    "ENFORCEMENT_KINDS",
    "ROW_PEEK_ID",
    "TIMELINE_EMPTY_ID",
    "TIMELINE_LIST_ID",
    "TIMELINE_RING_SIZE",
    "TIMELINE_ROW_CLASS",
    "TIMELINE_SELECTED_CLASS",
    "EnforcementRow",
    "SandboxEventsModeScreen",
    "SandboxRowPeekModal",
    "format_enforcement_markup",
    "format_enforcement_peek",
    "format_enforcement_row",
    "load_enforcement_rows",
]
