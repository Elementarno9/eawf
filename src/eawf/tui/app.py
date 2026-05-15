"""Rich-backed TUI for ``eawf tui`` (P14-W10 / D15 + D23; P20 quadrant).

Layout sketch (rich.Layout):

::

    +----------------------------------------------------------+
    | Eä  EAWF / P20 / P20-I01                                 |  ← header
    +-------------------------------+--------------------------+
    | roadmap                       | status                   |
    | phases (active):  1           | project: EAWF            |
    | iters  (active):  1           | phase:   P20             |
    | iters  (closed):  2           | iter:    P20-I01 (active)|
    | waves  (pending): 2           | audits:  21              |
    | waves  (in-prog): 1           |                          |
    +-------------------------------+--------------------------+   ← body
    | git                           | backlog                  |     (2x2)
    | branch:   feature/...         | open:   3                |
    | head:     abcd123             | closed: 18               |
    | status:   clean               | total:  21               |
    | upstream: up-to-date          |                          |
    +-------------------------------+--------------------------+
    | b board  c config  oH/oD/.. overlay  Esc/q quit          |  ← footer
    +----------------------------------------------------------+

Per ``feedback_tui_branding`` memory the header brand is literal ``Eä``
(capital E + a-umlaut), bold-accent, *outside-left* of the scope
breadcrumb. Per ``feedback_tui_keymap_conventions`` the keymap lists
arrow keys first with full-name aliases; vim keys appear as secondary
aliases only.

P20-I01-W02 promoted the body from a single state-summary panel to a
2x2 quadrant of repo-scope panes (roadmap / status / git / backlog).
Layout primitives live in :mod:`eawf.tui.layout`; this module owns the
:func:`run_tui` entry point, the state loader, the offline / online
tick modes, and the raw-mode keypress reader.

**Tick modes (P20-W02 success criterion 4):**

* *Offline* (``no_input=True`` / ``plain=True`` / non-TTY): render
  exactly one frame to stdout (via :func:`build_status_text` for the
  text fallback, or :func:`render_layout` when a console is supplied)
  and exit. Suitable for golden snapshot tests and bare-``eawf`` calls
  on non-TTY hosts.
* *Online* (TTY default): open a :class:`rich.live.Live` view at
  :data:`DEFAULT_REFRESH_HZ` Hz with a background reader thread
  feeding a :class:`queue.Queue`. The render loop drains the queue
  non-blockingly so the Live repaint never stalls on stdin.

P20-I03-W01 raised :data:`DEFAULT_REFRESH_HZ` from 1 to 30 Hz and
moved the raw-mode reader onto its own daemon thread. The git pane
caches its shell-out result for ~500ms so 30Hz repaint does not
incur 30 ``git status`` calls per second.

Overlay verb-prefix state machine (P20-I03-W01 success criterion 3):

* First key ``o`` enters ``overlay-pending`` mode; the footer flips
  to :data:`~eawf.tui.layout.FOOTER_KEYMAP_OVERLAY_PENDING`.
* Second key in ``{H, D, M, E, R}`` calls
  :func:`eawf.tui.overlays.open_overlay` and swaps the body to the
  overlay until ``Esc`` returns to the quadrant.
* Any other second key (incl. ``Esc``) cancels the pending state
  without opening an overlay.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live

from eawf.state.models import State
from eawf.tui import overlays as overlays_mod
from eawf.tui import wave_board as wave_board_mod
from eawf.tui.layout import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    FOOTER_KEYMAP,
    FOOTER_KEYMAP_OVERLAY_PENDING,
    build_breadcrumb,
    build_frame,
    summary_counts,
)

logger = logging.getLogger(__name__)


#: Exit keystrokes recognised by the online tick loop. Bare ``\\x1b``
#: is Esc; ``q``/``Q`` are the operator-facing exits; ``\\x03`` and
#: ``\\x04`` are ``Ctrl-C`` and ``Ctrl-D`` from cbreak mode.
_EXIT_KEYS: frozenset[str] = frozenset({"\x1b", "q", "Q", "\x03", "\x04"})

#: Key that opens the wave-board view from the repo-scope quadrant.
#: Pressing ``b`` enters the wave-board sub-loop; Esc / q exits back to
#: the quadrant (matching the wave-board's own exit keys). Picked
#: ``b`` for "board" — distinct from the existing quadrant exits and
#: from any vim-alias the keymap convention reserves.
_WAVE_BOARD_OPEN_KEY: str = "b"

#: Key that opens the tabbed config modal from the repo-scope quadrant
#: (P20-I01-W11). The modal is a full-screen Layout swap that replaces
#: the quadrant; Esc / q returns. Picked ``c`` for "config" — distinct
#: from ``b`` (wave-board) and the exit keys.
_CONFIG_MODAL_OPEN_KEY: str = "c"

#: Verb-prefix key that enters overlay-pending mode (P20-I03-W01).
#: After the operator presses ``o`` the loop waits for one of
#: ``{H, D, M, E, R}`` and dispatches into
#: :func:`eawf.tui.overlays.open_overlay`. Esc / any other key cancels.
_OVERLAY_PREFIX_KEY: str = "o"

#: Default refresh rate for the online :class:`Live` loop. Raised from
#: 1Hz to 30Hz in P20-I03-W01 so live keypresses repaint as soon as the
#: render thread next ticks (~33ms median). The git pane caches its
#: shell-out output for ~500ms so the bump does not amplify subprocess
#: cost.
DEFAULT_REFRESH_HZ: int = 30

#: How long :func:`_drain_key_queue` waits between ``queue.Queue.get``
#: probes when no key is available. Short enough that the loop's
#: response to a keystroke is bounded by the Live repaint cadence,
#: long enough that an idle terminal does not burn CPU on a busy-loop.
_QUEUE_POLL_INTERVAL: float = 0.01


def _load_state(workspace: Path | None) -> dict[str, Any]:
    """Best-effort load of ``<workspace>/.ea/state.json``.

    Returns an empty dict when the file is missing or unreadable —
    the TUI surface stays informational regardless of state health.
    """
    if workspace is None:
        candidate = Path.cwd() / ".ea" / "state.json"
    else:
        candidate = Path(workspace) / ".ea" / "state.json"
    if not candidate.is_file():
        return {}
    try:
        data: dict[str, Any] = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return {}
    return data


def _breadcrumb(state: dict[str, Any]) -> str:
    """Backward-compatible wrapper around :func:`build_breadcrumb`.

    Pre-W02 tests imported this private helper directly; the layout
    module owns the implementation now but the symbol is preserved so
    the existing smoke test stays green without churn.
    """
    return build_breadcrumb(state)


def _summary_counts(state: dict[str, Any]) -> dict[str, int]:
    """Backward-compatible wrapper around :func:`summary_counts`."""
    return summary_counts(state)


def build_status_text(state: dict[str, Any]) -> str:
    """Deterministic single-shot status — used by the non-TTY fallback.

    Offline tick mode (the ``no_input`` / ``plain`` / non-TTY branch
    of :func:`run_tui`) prints this string and exits without entering
    the :class:`rich.live.Live` loop.
    """
    breadcrumb = build_breadcrumb(state)
    counts = summary_counts(state)
    project = (state.get("project") or {}).get("code") or DEFAULT_PROJECT_CODE
    return (
        f"{BRAND}  {breadcrumb}\n"
        f"  project={project} "
        f"phases_open={counts['phases_open']} "
        f"iters_open={counts['iters_open']} "
        f"iters_closed={counts['iters_closed']} "
        f"waves_pending={counts['waves_pending']} "
        f"audits={counts['audits']}\n"
        f"keymap: {FOOTER_KEYMAP}"
    )


def _build_layout(
    state: dict[str, Any],
    *,
    workspace: Path | None = None,
    footer_keymap: str | None = None,
) -> Layout:
    """Compose the header/body/footer frame with the 2x2 body quadrant.

    Delegates to :func:`eawf.tui.layout.build_frame` so the layout
    primitives stay reusable across future TUI surfaces. ``workspace``
    is forwarded to the git pane; ``footer_keymap`` flips the footer
    string to :data:`~eawf.tui.layout.FOOTER_KEYMAP_OVERLAY_PENDING`
    while the operator is mid-verb-prefix.
    """
    return build_frame(state, workspace=workspace, footer_keymap=footer_keymap)


def render_layout(
    state: dict[str, Any],
    *,
    console: Console | None = None,
    workspace: Path | None = None,
) -> str:
    """Render the live Layout into a string buffer.

    Offline callers (golden snapshot tests, the wave-02 dispatch
    fixture) consume this so they never block on an interactive
    :class:`rich.live.Live` loop; production callers wire
    :func:`run_tui` to the real terminal.

    Args:
        state: Loaded ``state.json`` dict.
        console: Optional pre-built console to render into. When
            supplied the function writes into the caller's console and
            returns an empty string; otherwise a fresh non-terminal
            console renders into an in-process :class:`io.StringIO`
            buffer and the captured text is returned.
        workspace: Optional workspace root for the git pane.

    Returns:
        The captured render output when ``console`` is ``None``,
        otherwise an empty string.
    """
    buf = io.StringIO()
    real_console = console or Console(file=buf, force_terminal=False, width=100, record=False)
    layout = _build_layout(state, workspace=workspace)
    real_console.print(layout)
    return buf.getvalue() if console is None else ""


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _read_one_cbreak(tty_file: Any, termios_mod: Any, tty_mod: Any) -> str:
    """Read one keystroke from *tty_file* under cbreak mode.

    A bare ``\\x1b`` (Esc) is distinguished from a CSI / arrow sequence
    (``\\x1b[A`` etc.) by peeking via :mod:`select` with a 50 ms window
    after the leading ESC. Any follow-on bytes are drained and joined
    into the returned string, so the caller's exit-key membership
    check only matches a *bare* Esc.
    """
    import select

    fd = tty_file.fileno()
    try:
        old = termios_mod.tcgetattr(fd)
    except termios_mod.error:
        return ""
    try:
        tty_mod.setcbreak(fd)
        ch = tty_file.read(1)
        if ch == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
            rest = b""
            while select.select([fd], [], [], 0.0)[0]:
                byte = tty_file.read(1)
                if not byte:
                    break
                rest += byte
                if len(rest) >= 16:
                    break
            ch += rest
    finally:
        termios_mod.tcsetattr(fd, termios_mod.TCSADRAIN, old)
    return ch.decode("utf-8", errors="replace") if ch else ""


def _read_key_raw() -> str:
    """Block reading one keypress from the controlling terminal.

    Reads from ``/dev/tty`` so the TUI survives stdin redirection
    (the parent shell stays the controlling terminal even when
    ``eawf`` is run from a wrapper that pipes its own stdin). Uses
    :func:`tty.setcbreak` rather than :func:`tty.setraw` so the
    output discipline (``OPOST``/``ONLCR``) stays intact for the
    concurrent :class:`rich.live.Live` renderer thread.

    Returns the single character read, or empty string on EOF
    (closed tty, ``Ctrl-D`` under cbreak), or empty string when no
    controlling terminal is available.
    """
    try:
        import termios
        import tty
    except ImportError:
        return sys.stdin.readline()[:1]
    try:
        with open("/dev/tty", "rb", buffering=0) as tty_file:
            return _read_one_cbreak(tty_file, termios, tty)
    except OSError:
        if not sys.stdin.isatty():
            return ""
        return _read_one_cbreak(sys.stdin.buffer, termios, tty)


# ---------------------------------------------------------------------------
# Non-blocking input thread (P20-I03-W01 success criterion 2)
# ---------------------------------------------------------------------------


def _input_reader_loop(
    keys: queue.Queue[str], read_key: Callable[[], str], stop: threading.Event
) -> None:
    """Daemon-thread body — block on *read_key* and push onto *keys*.

    The function exits cleanly when *read_key* returns an empty string
    (EOF / closed tty) or when *stop* is set. Any other exception is
    logged and the loop terminates so the main thread can fall through
    its own exit path.
    """
    while not stop.is_set():
        try:
            ch = read_key()
        except KeyboardInterrupt:
            keys.put("\x03")
            return
        except Exception as exc:  # pragma: no cover — defensive log
            logger.warning(f"_input_reader_loop read_key failed: {exc!r}")
            return
        if not ch:
            # EOF (closed tty / Ctrl-D under cbreak). Push an empty
            # sentinel so the main loop can see the close and exit.
            keys.put("")
            return
        keys.put(ch)


def _start_input_thread(
    read_key: Callable[[], str],
) -> tuple[queue.Queue[str], threading.Event, threading.Thread]:
    """Start a daemon reader thread feeding a :class:`queue.Queue`.

    Returns the queue, the stop :class:`threading.Event`, and the
    spawned :class:`threading.Thread` (already started).
    """
    keys: queue.Queue[str] = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(
        target=_input_reader_loop,
        args=(keys, read_key, stop),
        name="eawf-tui-input",
        daemon=True,
    )
    thread.start()
    return keys, stop, thread


def _drain_key(keys: queue.Queue[str], *, timeout: float) -> str | None:
    """Wait up to *timeout* seconds for one key; return ``None`` on miss.

    Reading via :meth:`queue.Queue.get` with a timeout lets the Live
    repaint thread keep ticking even when the operator is idle — the
    render loop checks for a key, gets ``None``, and falls through to
    the next repaint without blocking.
    """
    try:
        return keys.get(timeout=timeout)
    except queue.Empty:
        return None


def _make_queue_reader(keys: queue.Queue[str]) -> Callable[[], str]:
    """Return a ``read_key``-compatible function that drains *keys*.

    The config modal and any other sub-loop that wants blocking-key
    semantics can call this to consume keystrokes from the shared
    queue rather than re-reading the tty in parallel with the daemon
    thread. The returned function blocks indefinitely on
    :meth:`queue.Queue.get` — matching the original modal contract.
    """

    def _reader() -> str:
        return keys.get()

    return _reader


def run_tui(
    *,
    workspace: Path | None = None,
    no_input: bool = False,
    plain: bool = False,
    read_key: Callable[[], str] | None = None,
    refresh_per_second: int = DEFAULT_REFRESH_HZ,
) -> int:
    """Open the live Rich TUI or emit a single-shot offline frame.

    Tick modes:

    * **Offline** — when ``no_input`` or ``plain`` is true, or stdout
      is not a tty: render exactly one frame via
      :func:`build_status_text` and exit (no :class:`rich.live.Live`,
      no keypress loop). Golden tests pin this mode.
    * **Online** — TTY default: open :class:`rich.live.Live` at
      ``refresh_per_second`` ticks/sec, spawn a daemon reader thread
      that pushes keystrokes onto a :class:`queue.Queue`, and drain
      the queue non-blockingly. The render loop never stalls on
      stdin; state is reloaded each time a key arrives. Exit on
      ``Esc``/``q``/``Ctrl-C``/EOF.

    Args:
        workspace: Project root containing ``.ea/state.json``. Defaults
            to ``Path.cwd()``.
        no_input: Skip the live loop, emit deterministic status text.
        plain: Same as ``no_input`` — caller chose plain output.
        read_key: Test seam for the raw-mode keypress reader. Defaults
            to :func:`_read_key_raw`. Tests inject a function that
            returns keystrokes one at a time; the reader thread will
            propagate ``StopIteration`` via an empty-string sentinel
            that exits the loop cleanly.
        refresh_per_second: Online-mode tick rate for
            :class:`rich.live.Live`. Defaults to
            :data:`DEFAULT_REFRESH_HZ`.

    Returns:
        Exit code (``0`` on clean shutdown).
    """
    state = _load_state(workspace)
    if no_input or plain or not _is_tty():
        text = build_status_text(state)
        print(text)
        return 0
    reader = read_key or _read_key_raw
    console = Console(force_terminal=True)
    view = "quadrant"
    overlay_pending = False
    overlay_renderable: Layout | None = None
    wave_view = wave_board_mod.WaveBoardState()
    keys_q, stop_event, _thread = _start_input_thread(_wrap_reader_for_thread(reader))
    try:
        with Live(
            _build_layout(state, workspace=workspace),
            console=console,
            screen=True,
            refresh_per_second=refresh_per_second,
            transient=False,
        ) as live:
            while True:
                ch = _drain_key(keys_q, timeout=_QUEUE_POLL_INTERVAL)
                if ch is None:
                    # No key this tick — let the Live loop repaint.
                    continue
                if ch == "":
                    # Reader thread saw EOF — close out cleanly.
                    break
                if ch == "\x03":
                    raise KeyboardInterrupt
                if view == "quadrant":
                    if overlay_pending:
                        overlay_pending, overlay_renderable = _handle_overlay_second_key(
                            ch, workspace
                        )
                        if overlay_renderable is not None:
                            view = "overlay"
                            live.update(overlay_renderable)
                        else:
                            live.update(_build_layout(_load_state(workspace), workspace=workspace))
                        continue
                    if ch in _EXIT_KEYS:
                        break
                    if ch == _OVERLAY_PREFIX_KEY:
                        overlay_pending = True
                        live.update(
                            _build_layout(
                                _load_state(workspace),
                                workspace=workspace,
                                footer_keymap=FOOTER_KEYMAP_OVERLAY_PENDING,
                            )
                        )
                        continue
                    if ch == _WAVE_BOARD_OPEN_KEY:
                        view = "wave_board"
                        wave_view = wave_board_mod.WaveBoardState()
                        live.update(_build_wave_board_or_quadrant(workspace, wave_view))
                        continue
                    if ch == _CONFIG_MODAL_OPEN_KEY:
                        # Suspend the parent Live: the modal opens its
                        # own Live context with full screen takeover.
                        # The modal shares our input queue (not the
                        # raw test-seam reader) so the daemon thread
                        # can keep pulling stdin without racing the
                        # modal's blocking calls.
                        live.stop()
                        modal_reader = _make_queue_reader(keys_q)
                        try:
                            _open_config_modal(workspace, modal_reader)
                        finally:
                            live.start()
                        live.update(_build_layout(_load_state(workspace), workspace=workspace))
                        continue
                    live.update(_build_layout(_load_state(workspace), workspace=workspace))
                elif view == "overlay":
                    if ch in _EXIT_KEYS:
                        view = "quadrant"
                        overlay_renderable = None
                        live.update(_build_layout(_load_state(workspace), workspace=workspace))
                    # Other keys are ignored while an overlay is up —
                    # overlays are read-only single frames per W04.
                    continue
                else:  # view == "wave_board"
                    if ch in _EXIT_KEYS:
                        view = "quadrant"
                        live.update(_build_layout(_load_state(workspace), workspace=workspace))
                        continue
                    typed_state = _load_typed_state(workspace)
                    if typed_state is not None:
                        wave_view = wave_board_mod.apply_key(wave_view, ch, state=typed_state)
                    live.update(_build_wave_board_or_quadrant(workspace, wave_view))
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
    return 0


def _wrap_reader_for_thread(reader: Callable[[], str]) -> Callable[[], str]:
    """Wrap *reader* so a ``StopIteration`` from a test iterator quits cleanly.

    Test seams pass an ``iter([...]).__next__``-style reader; once the
    iterator is exhausted the thread would otherwise raise
    ``StopIteration`` out of the loop and Python 3.7+ converts that to
    ``RuntimeError``. Catching it here and returning an empty sentinel
    matches the EOF path the loop already understands.
    """

    def _wrapped() -> str:
        try:
            return reader()
        except StopIteration:
            return ""

    return _wrapped


# ---------------------------------------------------------------------------
# Overlay verb-prefix dispatch
# ---------------------------------------------------------------------------


#: Map second-key character -> overlay kind. The verb prefix ``o`` is
#: consumed by the loop before this map is consulted; only the object
#: letters live here. Uppercase canonical so the keymap hint reads
#: cleanly; lowercase fallbacks accepted so the operator does not need
#: to hold Shift.
_OVERLAY_SECOND_KEYS: dict[str, overlays_mod.OverlayKind] = {
    "H": "hypothesis",
    "D": "decision",
    "M": "memory",
    "E": "events",
    "R": "dispatch",
}


def _resolve_overlay_target_id(kind: overlays_mod.OverlayKind, state: State) -> str | None:
    """Pick a sensible target id for the overlay-kind.

    Operators land on these overlays without having selected a record
    first, so we default to the first record of the relevant state
    collection. ``None`` is a valid return — the caller renders a
    ``(no records)`` placeholder instead of opening the overlay.
    """
    if kind == "hypothesis":
        ids = list(state.hypotheses.keys()) if state.hypotheses else []
        return ids[0] if ids else None
    if kind == "decision":
        ids = list(state.decisions.keys()) if state.decisions else []
        return ids[0] if ids else None
    if kind == "memory":
        ids = list(state.memory_index.keys()) if state.memory_index else []
        return ids[0] if ids else None
    if kind == "events":
        # Events live in the on-disk store, not :class:`State` — pass
        # a filter label so the overlay title shows context.
        return "recent"
    # kind == "dispatch"
    current_wave = state.current.active_wave_ids[0] if state.current.active_wave_ids else None
    if current_wave is not None:
        return current_wave
    waves = list(state.waves.keys())
    return waves[0] if waves else None


def _build_overlay_placeholder(kind: overlays_mod.OverlayKind) -> Layout:
    """Render a one-frame ``(no records)`` placeholder for *kind*.

    Used when the relevant state collection is empty and there is no
    record to feed :func:`eawf.tui.overlays.open_overlay`. The layout
    reuses the chassis so the brand strip stays consistent.
    """
    from rich.panel import Panel
    from rich.text import Text

    from eawf.tui.layout import build_brand_text

    text = build_brand_text(kind)
    text.append(f"  | overlay: {kind} (no records)", style="dim")
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
    )
    layout["header"].update(Panel(text, title=None, border_style="dim"))
    layout["body"].update(
        Panel(
            Text(f"no {kind} records under the current scope"),
            title=kind,
            border_style="cyan",
        )
    )
    return layout


def _handle_overlay_second_key(ch: str, workspace: Path | None) -> tuple[bool, Layout | None]:
    """Dispatch the second key of an ``o<letter>`` verb prefix.

    Returns a ``(overlay_pending, overlay_renderable)`` tuple. When
    *overlay_renderable* is non-None the caller swaps the body to it
    and transitions to the overlay view; when it is None the caller
    repaints the quadrant (the user cancelled or picked an unknown
    letter).
    """
    overlay_kind = _OVERLAY_SECOND_KEYS.get(ch) or _OVERLAY_SECOND_KEYS.get(ch.upper())
    if overlay_kind is None:
        # Esc / arrow / unknown — cancel the pending state and repaint
        # the bare quadrant.
        return False, None
    typed_state = _load_typed_state(workspace)
    if typed_state is None:
        # Schema mismatch — render a placeholder so the operator sees
        # *something* rather than crashing the loop.
        return False, _build_overlay_placeholder(overlay_kind)
    target_id = _resolve_overlay_target_id(overlay_kind, typed_state)
    if target_id is None:
        return False, _build_overlay_placeholder(overlay_kind)
    try:
        renderable = overlays_mod.open_overlay(overlay_kind, typed_state, target_id)
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning(
            f"_handle_overlay_second_key overlay_kind={overlay_kind} "
            f"target_id={target_id!r} failed: {exc!r}"
        )
        return False, _build_overlay_placeholder(overlay_kind)
    # ``open_overlay`` returns a ``RenderableType``; the wave-board and
    # quadrant tick loops only support a :class:`Layout` swap. We know
    # the production builder always returns a Layout — assert and
    # narrow for the type checker.
    if isinstance(renderable, Layout):
        return False, renderable
    return False, _build_overlay_placeholder(overlay_kind)


def _load_typed_state(workspace: Path | None) -> State | None:
    """Load + validate ``state.json`` into a typed :class:`State`.

    Returns ``None`` when the state file is missing or fails schema
    validation — the wave-board sub-loop degrades to an empty-plan
    placeholder in that case rather than crashing the live loop.
    """
    raw = _load_state(workspace)
    if not raw:
        return None
    try:
        return State.model_validate(raw)
    except Exception as exc:  # pragma: no cover — defensive log
        logger.warning(f"_load_typed_state workspace={workspace!r} schema mismatch: {exc!r}")
        return None


def _open_config_modal(workspace: Path | None, reader: Callable[[], str]) -> None:
    """Open the W11 tabbed config modal as a full-screen Layout swap.

    The modal owns its own :class:`rich.live.Live` context; the parent
    Live in :func:`run_tui` is suspended around the call so the screen
    state is exclusive. ``repo`` defaults to ``Path.cwd()`` (matches
    the layered-config CLI's ``_resolve_anchors``).
    """
    from eawf.tui import config_modal as config_modal_mod

    repo = Path.cwd()
    state = _load_state(workspace)
    config_modal_mod.run_config_modal(
        state=state,
        workspace=workspace,
        repo=repo,
        read_key=reader,
    )


def _build_wave_board_or_quadrant(
    workspace: Path | None, wave_view: wave_board_mod.WaveBoardState
) -> Layout:
    """Render the wave-board frame, falling back to the quadrant on bad state.

    When the workspace has no ``state.json`` or the file is unreadable
    the typed wave-board cannot resolve DAG edges, so we degrade to
    the dict-shaped quadrant frame which is robust to a missing
    state file. The sub-loop keeps the operator on the wave-board
    view name; the rendered surface just looks like the quadrant
    until state is repaired.
    """
    typed_state = _load_typed_state(workspace)
    if typed_state is None:
        return _build_layout(_load_state(workspace), workspace=workspace)
    return wave_board_mod.build_wave_board_frame(typed_state, view=wave_view)


def _measure_render_rate(state: dict[str, Any], *, seconds: float = 0.25) -> float:
    """Best-effort offline FPS measurement for diagnostics.

    Renders the quadrant frame as fast as possible into a discarded
    :class:`io.StringIO` for *seconds* of wall time and returns the
    frames-per-second rate. Used by P20-I03-W01 to record the rough
    online-loop ceiling in the wave handoff. Not part of the public
    API; tests / dispatch tooling only.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, record=False)
    count = 0
    start = time.perf_counter()
    end = start + seconds
    while time.perf_counter() < end:
        console.print(_build_layout(state))
        count += 1
        buf.seek(0)
        buf.truncate()
    elapsed = time.perf_counter() - start
    return count / elapsed if elapsed > 0 else 0.0


__all__ = [
    "DEFAULT_REFRESH_HZ",
    "build_status_text",
    "render_layout",
    "run_tui",
]
