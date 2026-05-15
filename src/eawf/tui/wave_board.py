"""Wave-board view for the Eä Rich TUI (P20-I01-W03).

A full-screen list-and-detail surface over the current iter's wave
plan. Reuses the header / footer chassis from :mod:`eawf.tui.layout`
so the brand strip and footer hint stay consistent with the
repo-scope quadrant (W02). The body is a list-on-top, detail-below
split — the list pane is the primary navigation surface and the
detail pane drills into the currently selected wave.

Layout sketch::

    +----------------------------------------------------------+
    | Eä  EAWF / P20 / P20-I01                                 |  ← header
    +----------------------------------------------------------+
    | waves (filter=all, 5 of 5)                               |
    | > P20-I01-W02  in_progress  feat: quadrant TUI           |  ← list
    |   P20-I01-W03  pending      feat: wave board             |
    |   P20-I01-W01  closed       feat: roadmap table          |
    |   P20-I01-W05  failed       feat: x                      |
    +----------------------------------------------------------+
    | wave P20-I01-W02                                         |
    |   status: in_progress                                    |
    |   deps:        P20-I01-W01                               |  ← detail
    |   blocked_by:  -                                         |
    |   tests:       -                                         |
    |   budget:      4000 / 8000 (50%)                         |
    |   criteria:                                              |
    |     - list view sorted by status priority then wave_id   |
    +----------------------------------------------------------+
    | ↑↓ select  Enter open  f filter  Esc back  (vim: j k g G)|  ← footer
    +----------------------------------------------------------+

**Sort order (success criterion 1).** Operators care about
*in-flight* work first, so the priority order is
``in_progress > claimed > pending > failed > closed > abandoned``.
Within each bucket waves sort by ``wave_id`` ascending so the
display stays stable across reloads.

**Filter cycle (success criterion 3).** Pressing ``f`` advances
through ``all`` → ``pending`` → ``claimed/in_progress`` →
``closed`` → ``failed`` → ``all``. The current mode is shown in
the list-header line and the footer hint.

**Detail view (success criterion 2).** Reads the DAG edges through
:func:`eawf.state.wave_graph.edges_for_iter` so the typed accessor
(W15) is the single source of truth; never inlines a walk over
``Wave.blocks``. Tests / budget / criteria come straight off the
:class:`~eawf.state.models.Wave` record.

The wave-board does NOT mutate state; it is a read-only viewer.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from eawf.state.enums import WaveStatus
from eawf.state.models import State, Wave
from eawf.state.wave_graph import edges_for_iter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filter / sort constants
# ---------------------------------------------------------------------------


#: Status priority used by :func:`sort_waves`. Lower number = higher
#: priority = nearer the top of the list. The operator wants to see
#: in-flight work first, then queued, then terminal states.
STATUS_PRIORITY: dict[str, int] = {
    WaveStatus.IN_PROGRESS.value: 0,
    WaveStatus.CLAIMED.value: 1,
    WaveStatus.PENDING.value: 2,
    WaveStatus.FAILED.value: 3,
    WaveStatus.CLOSED.value: 4,
    WaveStatus.ABANDONED.value: 5,
}

#: Sentinel priority for any unrecognised status string (kept stable so
#: the sort is total even if the enum drifts). Sorts after every known
#: status so unknown labels do not steal the operator's eye.
_UNKNOWN_STATUS_PRIORITY: int = 99


#: Ordered tuple of filter modes (success criterion 3). The cycle is
#: defined by index order: pressing ``f`` advances to the next member,
#: wrapping at the end. The ``claimed_in_progress`` mode merges the two
#: actively-worked statuses into one filter so the operator sees what
#: is "in flight" at a glance.
FILTER_MODES: tuple[str, ...] = (
    "all",
    "pending",
    "claimed_in_progress",
    "closed",
    "failed",
)


#: Footer hint for the wave-board surface. Lists primary keys first
#: (arrows + Enter/Esc) with vim aliases trailing per
#: ``feedback_tui_keymap_conventions``.
WAVE_BOARD_FOOTER: str = (
    "↑↓ select  PageUp/PageDown page  Home/End jump  "
    "Enter detail  f filter  Esc back  (vim: j k g G)"
)


# ---------------------------------------------------------------------------
# Wave-board view state (Pydantic v2; strict)
# ---------------------------------------------------------------------------


class WaveBoardState(BaseModel):
    """Ephemeral view state for the wave-board surface.

    Tracks the operator's selection cursor and current filter mode.
    Strict (``extra="forbid"``) so a typo at construction time fails
    fast rather than silently dropping fields.

    The state is *ephemeral* — it does not persist across runs. The
    on-disk ``state.json`` is the source of truth for wave records;
    this object only carries view-mode information for the live loop.
    """

    model_config = ConfigDict(extra="forbid")

    selected_index: int = Field(default=0, ge=0)
    filter_mode: str = "all"


# ---------------------------------------------------------------------------
# Sort / filter helpers (public, pure)
# ---------------------------------------------------------------------------


def status_priority(status: str) -> int:
    """Return the sort priority for *status*.

    Lower values rank higher (top of the list). Unknown statuses get
    a sentinel priority so the sort stays total.

    Args:
        status: Status string (e.g. ``"in_progress"``).

    Returns:
        Integer priority; lower = nearer the top.
    """
    return STATUS_PRIORITY.get(status, _UNKNOWN_STATUS_PRIORITY)


def sort_waves(waves: list[Wave]) -> list[Wave]:
    """Sort *waves* by status priority then wave_id (ascending).

    Operator-friendly ordering per success criterion 1:
    ``in_progress > claimed > pending > failed > closed``. Within each
    bucket waves sort by ``id`` lexicographically so the display is
    stable across reloads.

    Args:
        waves: List of :class:`Wave` records (order irrelevant).

    Returns:
        New list (does not mutate input) sorted in operator order.
    """
    return sorted(waves, key=lambda w: (status_priority(w.status.value), w.id))


def filter_waves(waves: list[Wave], mode: str) -> list[Wave]:
    """Filter *waves* by *mode*.

    ``mode`` must be one of :data:`FILTER_MODES`:

    - ``all`` — no filter, every wave passes through.
    - ``pending`` — only ``WaveStatus.PENDING``.
    - ``claimed_in_progress`` — ``WaveStatus.CLAIMED`` or
      ``WaveStatus.IN_PROGRESS`` (the "in flight" view).
    - ``closed`` — only ``WaveStatus.CLOSED``.
    - ``failed`` — only ``WaveStatus.FAILED``.

    Args:
        waves: Source list (order preserved among the kept waves).
        mode: Filter mode name.

    Returns:
        New list containing only waves matching *mode*.

    Raises:
        ValueError: when *mode* is not in :data:`FILTER_MODES`.
    """
    if mode not in FILTER_MODES:
        raise ValueError(f"unknown filter mode: {mode!r}")
    if mode == "all":
        return list(waves)
    if mode == "pending":
        keep = {WaveStatus.PENDING}
    elif mode == "claimed_in_progress":
        keep = {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
    elif mode == "closed":
        keep = {WaveStatus.CLOSED}
    elif mode == "failed":
        keep = {WaveStatus.FAILED}
    else:  # pragma: no cover — exhausted by the FILTER_MODES guard
        raise ValueError(f"unknown filter mode: {mode!r}")
    return [w for w in waves if w.status in keep]


def next_filter_mode(mode: str) -> str:
    """Return the next filter mode in the cycle.

    Wraps at the end so the cycle is closed. Unknown *mode* values
    reset to the first element (``"all"``) so a corrupt view state
    cannot wedge the cycle.

    Args:
        mode: Current filter mode.

    Returns:
        Next filter mode name from :data:`FILTER_MODES`.
    """
    try:
        idx = FILTER_MODES.index(mode)
    except ValueError:
        logger.warning(f"next_filter_mode mode={mode!r} not in FILTER_MODES; resetting")
        return FILTER_MODES[0]
    return FILTER_MODES[(idx + 1) % len(FILTER_MODES)]


# ---------------------------------------------------------------------------
# State / iter resolution
# ---------------------------------------------------------------------------


def waves_for_iter(state: State, iter_id: str) -> list[Wave]:
    """Return every :class:`Wave` whose ``iter_id`` matches.

    The returned list is in arbitrary order; callers that need a
    display order pipe the result through :func:`sort_waves`.

    Args:
        state: Validated :class:`State` document.
        iter_id: Iter id (e.g. ``"P20-I01"``).

    Returns:
        List of :class:`Wave` records under *iter_id*; empty when the
        iter has no waves (or does not exist — the wave-board treats
        a missing iter as an empty plan).
    """
    return [w for w in state.waves.values() if w.iter_id == iter_id]


def _active_iter_id(state: State) -> str | None:
    """Pick the iter the wave-board should focus on.

    Prefers ``state.current.iter_id`` (the operator's active iter).
    Falls back to ``None`` when the pointer is unset; the caller
    renders an "empty plan" placeholder in that case.
    """
    return state.current.iter_id if state.current else None


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def _format_list_header(filter_mode: str, shown: int, total: int) -> str:
    """List-pane header line: filter mode + visible/total count."""
    return f"waves (filter={filter_mode}, {shown} of {total})"


def _format_list_row(wave: Wave, *, selected: bool) -> str:
    """Render one wave row: cursor marker + id + status + title."""
    marker = ">" if selected else " "
    return f"{marker} {wave.id}  {wave.status.value:<12}  {wave.title}"


def build_list_panel(
    waves: list[Wave],
    *,
    filter_mode: str,
    selected_index: int,
    total: int,
) -> Panel:
    """Render the list pane: header + one row per visible wave.

    Args:
        waves: The already-sorted-and-filtered slice to render.
        filter_mode: Current filter mode (shown in the header).
        selected_index: 0-based index into *waves* to mark with ``>``.
        total: Total wave count under the active iter (pre-filter),
            shown alongside the filtered count.

    Returns:
        Rich :class:`Panel` titled ``"waves"``.
    """
    lines: list[str] = [_format_list_header(filter_mode, len(waves), total)]
    if not waves:
        lines.append("  (no waves match this filter)")
    else:
        for idx, wave in enumerate(waves):
            lines.append(_format_list_row(wave, selected=idx == selected_index))
    return Panel(Text("\n".join(lines)), title="waves", border_style="cyan")


def _format_dep_list(items: tuple[str, ...]) -> str:
    """Comma-separated id list or ``-`` placeholder when empty."""
    return ", ".join(items) if items else "-"


def _format_budget(wave: Wave) -> str:
    """Render the budget cell: ``<consumed> / <budget> (<pct>%)`` or ``-``.

    The CLI's ``eawf wave budget show`` data path reads
    ``wave.token_budget`` and ``wave.tokens_consumed`` — this helper
    mirrors that shape so the operator sees the same numbers.
    """
    budget = wave.token_budget
    consumed = wave.tokens_consumed
    if budget is None:
        return f"- / - (consumed {consumed})" if consumed else "-"
    if budget == 0:
        # Avoid div-by-zero on a degenerate budget; still show the cap.
        return f"{consumed} / 0"
    pct = round(100 * consumed / budget)
    return f"{consumed} / {budget} ({pct}%)"


def _format_criteria(criteria: list[str]) -> list[str]:
    """One bullet line per success criterion, or a single ``-`` row.

    Returns the rendered lines, ready to be joined into the detail
    panel body — keeps the indentation consistent with the other
    detail rows.
    """
    if not criteria:
        return ["    -"]
    return [f"    - {c}" for c in criteria]


def _format_tests(wave: Wave) -> str:
    """Tests cell — placeholder until a dedicated state field lands.

    The wave model does not (yet) carry a typed ``tests`` outcome
    structure; the dispatch spec calls for ``-`` when absent, which
    is the current state for every wave. We surface the outcome
    string when set (close-time annotation) so the operator at least
    sees the close-outcome text in the same column.
    """
    if wave.outcome:
        return wave.outcome
    return "-"


def build_detail_panel(
    wave: Wave | None,
    *,
    state: State,
) -> Panel:
    """Render the detail pane for *wave*.

    Reads DAG edges via :func:`eawf.state.wave_graph.edges_for_iter`
    so the typed accessor (W15) is the single source of truth. The
    wave-board never walks ``Wave.deps`` / ``Wave.blocks`` inline.

    Args:
        wave: Selected :class:`Wave`. When ``None`` (empty list,
            invalid cursor) the pane renders an "empty" placeholder.
        state: Validated :class:`State` document used to resolve DAG
            edges through the typed accessor.

    Returns:
        Rich :class:`Panel` titled ``"detail"``.
    """
    if wave is None:
        body = Text("(no wave selected)")
        return Panel(body, title="detail", border_style="cyan")
    edges_map = edges_for_iter(wave.iter_id, state)
    edges = edges_map.get(wave.id)
    if edges is None:
        # Defensive: should not happen because edges_for_iter walks
        # every wave under the iter, but the typed accessor is the
        # source of truth so we degrade gracefully.
        logger.warning(f"build_detail_panel wave={wave.id!r} missing from edges_for_iter")
        deps_str = _format_dep_list(tuple(sorted(wave.deps)))
        blocked_by_str = "-"
    else:
        deps_str = _format_dep_list(edges.deps)
        blocked_by_str = _format_dep_list(edges.blocked_by)
    lines: list[str] = [
        f"wave {wave.id}",
        f"  title:       {wave.title}",
        f"  status:      {wave.status.value}",
        f"  deps:        {deps_str}",
        f"  blocked_by:  {blocked_by_str}",
        f"  tests:       {_format_tests(wave)}",
        f"  budget:      {_format_budget(wave)}",
        "  criteria:",
    ]
    lines.extend(_format_criteria(wave.success_criteria))
    return Panel(Text("\n".join(lines)), title="detail", border_style="cyan")


# ---------------------------------------------------------------------------
# Header / footer reuse + frame composition
# ---------------------------------------------------------------------------


def build_header_panel(state: State) -> Panel:
    """Header strip — brand + scope breadcrumb.

    Reuses the layout module's header chassis so the wave-board
    brand strip is byte-identical to the W02 quadrant header.
    The :func:`eawf.tui.layout.build_header_panel` helper takes a
    plain dict rather than the typed State; we round-trip via
    ``model_dump`` so the typed surface stays the contract here
    while the layout helper remains state-shape-agnostic.
    """
    from eawf.tui.layout import build_header_panel as _layout_header

    return _layout_header(state.model_dump(mode="json"))


def build_footer_panel() -> Panel:
    """Footer strip — wave-board-specific keymap hint."""
    return Panel(Text(WAVE_BOARD_FOOTER), title=None, border_style="dim")


def build_wave_board_frame(
    state: State,
    *,
    view: WaveBoardState,
) -> Layout:
    """Assemble the header + body (list / detail) + footer frame.

    The body is a vertical split: list pane on top, detail pane
    below, with a 2:1 ratio so the list dominates the screen.

    Args:
        state: Validated :class:`State` document.
        view: Current wave-board view state (selection + filter).

    Returns:
        Rich :class:`Layout` ready to feed
        :class:`~rich.live.Live` or :func:`render_wave_board`.
    """
    iter_id = _active_iter_id(state)
    if iter_id is None:
        iter_waves: list[Wave] = []
    else:
        iter_waves = waves_for_iter(state, iter_id)
    sorted_waves = sort_waves(iter_waves)
    filtered = filter_waves(sorted_waves, view.filter_mode)
    selected_index = max(0, min(view.selected_index, len(filtered) - 1)) if filtered else 0
    selected_wave = filtered[selected_index] if filtered else None

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    body = Layout(name="body_split")
    # Sizing: the list pane grows with the wave count plus one row
    # for the filter-mode header and the panel borders. The detail
    # pane takes whatever vertical room is left so deps / blocked_by /
    # tests / budget / criteria stay visible on a ~24-row terminal.
    list_rows = max(1, len(filtered) if filtered else 1) + 1
    list_height = list_rows + 2  # panel top + bottom borders
    body.split_column(
        Layout(name="list", size=list_height),
        Layout(name="detail", ratio=1),
    )
    body["list"].update(
        build_list_panel(
            filtered,
            filter_mode=view.filter_mode,
            selected_index=selected_index,
            total=len(iter_waves),
        )
    )
    body["detail"].update(build_detail_panel(selected_wave, state=state))
    layout["header"].update(build_header_panel(state))
    layout["body"].update(body)
    layout["footer"].update(build_footer_panel())
    return layout


def render_wave_board(
    state: State,
    *,
    view: WaveBoardState | None = None,
    console: Console | None = None,
) -> str:
    """Render the wave-board frame into a string buffer.

    Offline callers (golden tests, headless rendering) consume this
    so they never block on an interactive :class:`rich.live.Live`
    loop. Mirrors :func:`eawf.tui.app.render_layout` semantics.

    Args:
        state: Validated :class:`State` document.
        view: Current view state. Defaults to a fresh
            :class:`WaveBoardState` (cursor at 0, filter ``all``).
        console: Optional pre-built console to render into. When
            supplied the function writes into the caller's console
            and returns an empty string.

    Returns:
        Captured render output when ``console`` is ``None``,
        otherwise an empty string.
    """
    view = view or WaveBoardState()
    buf = io.StringIO()
    real_console = console or Console(file=buf, force_terminal=False, width=100, record=False)
    layout = build_wave_board_frame(state, view=view)
    real_console.print(layout)
    return buf.getvalue() if console is None else ""


# ---------------------------------------------------------------------------
# Online tick mode (rich.live.Live + keypress loop)
# ---------------------------------------------------------------------------


#: Exit keystrokes recognised by the online tick loop. Esc / q / Q
#: return the operator to the parent surface; ``\x03`` / ``\x04`` are
#: Ctrl-C / Ctrl-D from cbreak mode.
_EXIT_KEYS: frozenset[str] = frozenset({"\x1b", "q", "Q", "\x03", "\x04"})

#: Keys that move the cursor up the list (arrow up, vim ``k``, Home).
_UP_KEYS: frozenset[str] = frozenset({"\x1b[A", "k"})

#: Keys that move the cursor down the list (arrow down, vim ``j``).
_DOWN_KEYS: frozenset[str] = frozenset({"\x1b[B", "j"})

#: Filter cycle key.
_FILTER_KEY: str = "f"

#: Jump-to-top key (Home / vim ``g``).
_TOP_KEYS: frozenset[str] = frozenset({"\x1b[H", "g"})

#: Jump-to-bottom key (End / vim ``G``).
_BOTTOM_KEYS: frozenset[str] = frozenset({"\x1b[F", "G"})

#: Default refresh rate for the online :class:`Live` loop. Matches
#: :data:`eawf.tui.app.DEFAULT_REFRESH_HZ` so the two surfaces feel
#: equally responsive.
DEFAULT_REFRESH_HZ: int = 1


def _visible_count(state: State, view: WaveBoardState) -> int:
    """Return the count of waves that would appear under *view*."""
    iter_id = _active_iter_id(state)
    if iter_id is None:
        return 0
    waves = waves_for_iter(state, iter_id)
    return len(filter_waves(sort_waves(waves), view.filter_mode))


def apply_key(view: WaveBoardState, key: str, *, state: State) -> WaveBoardState:
    """Apply *key* to *view* and return the next :class:`WaveBoardState`.

    Pure function — does not touch the live :class:`rich.live.Live`
    loop and is therefore easy to drive from tests. Unknown keys
    return the view unchanged.

    Args:
        view: Current view state.
        key: Single keystroke (or ESC-prefixed CSI sequence for
            arrow keys).
        state: Validated :class:`State`; needed so cursor bounds
            stay valid as the filter shrinks the visible list.

    Returns:
        Updated :class:`WaveBoardState`.
    """
    count = _visible_count(state, view)
    if key == _FILTER_KEY:
        return WaveBoardState(selected_index=0, filter_mode=next_filter_mode(view.filter_mode))
    if key in _UP_KEYS:
        new_idx = max(0, view.selected_index - 1)
        return view.model_copy(update={"selected_index": new_idx})
    if key in _DOWN_KEYS:
        upper = max(0, count - 1)
        new_idx = min(upper, view.selected_index + 1)
        return view.model_copy(update={"selected_index": new_idx})
    if key in _TOP_KEYS:
        return view.model_copy(update={"selected_index": 0})
    if key in _BOTTOM_KEYS:
        upper = max(0, count - 1)
        return view.model_copy(update={"selected_index": upper})
    return view


def _load_state(workspace: Path | None) -> State | None:
    """Best-effort load + validate of ``<workspace>/.ea/state.json``.

    Returns ``None`` when the state file is missing or invalid — the
    wave-board falls back to an empty plan placeholder rather than
    crashing the live loop.
    """
    if workspace is None:
        candidate = Path.cwd() / ".ea" / "state.json"
    else:
        candidate = Path(workspace) / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        payload: dict[str, Any] = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        logger.warning(f"_load_state path={candidate!r} unreadable; falling back to empty plan")
        return None
    try:
        return State.model_validate(payload)
    except Exception as exc:  # pragma: no cover — defensive log
        logger.warning(f"_load_state path={candidate!r} schema mismatch: {exc!r}")
        return None


def run_wave_board(
    *,
    workspace: Path | None = None,
    read_key: Callable[[], str] | None = None,
    refresh_per_second: int = DEFAULT_REFRESH_HZ,
    initial_view: WaveBoardState | None = None,
) -> int:
    """Open the wave-board live view and block on keystrokes.

    Returns when the operator presses Esc / q / Ctrl-C / EOF. The
    state is reloaded from disk on every keystroke so external
    ``eawf state ...`` writes are reflected without a manual
    refresh — matches :func:`eawf.tui.app.run_tui` semantics.

    Args:
        workspace: Project root containing ``.ea/state.json``. Defaults
            to ``Path.cwd()``.
        read_key: Test seam for the raw-mode keypress reader. The
            wave-board uses the same reader as the parent TUI; when
            ``None`` the caller is expected to provide one (the
            production wiring lives in :mod:`eawf.tui.app`).
        refresh_per_second: Online-mode tick rate for
            :class:`rich.live.Live`. Defaults to
            :data:`DEFAULT_REFRESH_HZ`.
        initial_view: Starting view state. Defaults to a fresh
            :class:`WaveBoardState` (cursor 0, filter ``all``).

    Returns:
        Exit code (``0`` on clean shutdown).
    """
    if read_key is None:
        # The wave-board does not own a raw-mode reader; the parent
        # surface (eawf.tui.app) injects one. Falling back to a
        # blocking stdin readline keeps the function importable but
        # the parent always supplies a real reader in production.
        import sys

        def read_key() -> str:
            return sys.stdin.readline()[:1]

    state = _load_state(workspace)
    if state is None:
        logger.info(f"run_wave_board workspace={workspace!r} no_state opening_empty_frame")
    view = initial_view or WaveBoardState()
    console = Console(force_terminal=True)
    try:
        with Live(
            _build_layout_or_empty(state, view=view),
            console=console,
            screen=True,
            refresh_per_second=refresh_per_second,
            transient=False,
        ) as live:
            while True:
                try:
                    ch = read_key()
                except KeyboardInterrupt:
                    break
                if not ch:
                    break
                if ch in _EXIT_KEYS:
                    break
                refreshed = _load_state(workspace)
                if refreshed is not None:
                    state = refreshed
                view = apply_key(view, ch, state=state) if state else view
                live.update(_build_layout_or_empty(state, view=view))
    except KeyboardInterrupt:
        pass
    return 0


def _build_layout_or_empty(state: State | None, *, view: WaveBoardState) -> Layout:
    """Render frame for *state*, or a friendly empty-plan placeholder.

    The wave-board stays informational even when the workspace has
    no state file or the file is unreadable — matches the parent TUI
    convention of degrading gracefully rather than crashing.
    """
    if state is None:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3),
        )
        layout["header"].update(
            Panel(Text("Eä  (no state.json found)"), title=None, border_style="dim")
        )
        layout["body"].update(
            Panel(
                Text("no wave plan available — run `eawf state init` to seed a workspace"),
                title="waves",
                border_style="cyan",
            )
        )
        layout["footer"].update(build_footer_panel())
        return layout
    return build_wave_board_frame(state, view=view)


__all__ = [
    "DEFAULT_REFRESH_HZ",
    "FILTER_MODES",
    "STATUS_PRIORITY",
    "WAVE_BOARD_FOOTER",
    "WaveBoardState",
    "apply_key",
    "build_detail_panel",
    "build_footer_panel",
    "build_header_panel",
    "build_list_panel",
    "build_wave_board_frame",
    "filter_waves",
    "next_filter_mode",
    "render_wave_board",
    "run_wave_board",
    "sort_waves",
    "status_priority",
    "waves_for_iter",
]
