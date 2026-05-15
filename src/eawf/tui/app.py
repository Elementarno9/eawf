"""Rich-backed TUI for ``eawf tui`` (P14-W10 / D15 + D23; P20-W02 quadrant).

Layout sketch (rich.Layout):

::

    +----------------------------------------------------------+
    | Eä  EAWF / P20 / P20-I01                                 |  ← header
    +-------------------------------+--------------------------+
    | roadmap                       | status                   |
    | phases (active):  1           | project: EAWF            |
    | iters  (active):  1           | phase:   P20             |
    | waves  (pending): 2           | iter:    P20-I01         |
    | waves  (in-prog): 1           | audits:  21             |
    +-------------------------------+--------------------------+   ← body
    | git                           | backlog                  |     (2x2)
    | branch: feature/...           | open:   3                |
    | head:   abcd123               | closed: 18               |
    | status: clean                 | total:  21               |
    +-------------------------------+--------------------------+
    | ↑↓←→ navigate  PageUp/... ... Esc/q quit (vim: h j k l)  |  ← footer
    +----------------------------------------------------------+

Per ``feedback_tui_branding`` memory the header brand is literal ``Eä``
(capital E + a-umlaut), bold-accent, *outside-left* of the scope
breadcrumb. Per ``feedback_tui_keymap_conventions`` the keymap lists
arrow keys first with full-name aliases; vim keys appear as secondary
aliases only.

P20-I01-W02 promotes the body from a single state-summary panel to a
2x2 quadrant of repo-scope panes (roadmap / status / git / backlog).
Layout primitives live in :mod:`eawf.tui.layout`; this module owns the
:func:`run_tui` entry point, the state loader, the offline /online
tick modes, and the raw-mode keypress reader.

**Tick modes (P20-W02 success criterion 4):**

* *Offline* (``no_input=True`` / ``plain=True`` / non-TTY): render
  exactly one frame to stdout (via :func:`build_status_text` for the
  text fallback, or :func:`render_layout` when a console is supplied)
  and exit. Suitable for golden snapshot tests and bare-``eawf`` calls
  on non-TTY hosts.
* *Online* (TTY default): open a :class:`rich.live.Live` view with a
  fixed refresh interval (``DEFAULT_REFRESH_HZ`` ticks per second) and
  block on raw-mode keypresses until ``Esc`` / ``q`` / ``Ctrl-C`` /
  EOF. The frame is rebuilt from a fresh :func:`_load_state` read on
  every keystroke so external state CLI writes are reflected without
  a manual refresh.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live

from eawf.tui.layout import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    FOOTER_KEYMAP,
    build_breadcrumb,
    build_frame,
    summary_counts,
)

logger = logging.getLogger(__name__)


#: Exit keystrokes recognised by the online tick loop. Bare ``\\x1b``
#: is Esc; ``q``/``Q`` are the operator-facing exits; ``\\x03`` and
#: ``\\x04`` are ``Ctrl-C`` and ``Ctrl-D`` from cbreak mode.
_EXIT_KEYS: frozenset[str] = frozenset({"\x1b", "q", "Q", "\x03", "\x04"})

#: Default refresh rate for the online :class:`Live` loop. Picked low
#: enough that the rebuild-on-keypress remains the dominant tick path
#: while still letting time-based panes (e.g. the wave-08 git status
#: snapshot) repaint at ~1Hz.
DEFAULT_REFRESH_HZ: int = 1


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
        f"waves_pending={counts['waves_pending']} "
        f"audits={counts['audits']}\n"
        f"keymap: {FOOTER_KEYMAP}"
    )


def _build_layout(state: dict[str, Any]) -> Layout:
    """Compose the header/body/footer frame with the 2x2 body quadrant.

    Delegates to :func:`eawf.tui.layout.build_frame` so the layout
    primitives stay reusable across future TUI surfaces.
    """
    return build_frame(state)


def render_layout(state: dict[str, Any], *, console: Console | None = None) -> str:
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

    Returns:
        The captured render output when ``console`` is ``None``,
        otherwise an empty string.
    """
    buf = io.StringIO()
    real_console = console or Console(file=buf, force_terminal=False, width=100, record=False)
    layout = _build_layout(state)
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
    * **Online** — TTY default: open :class:`rich.live.Live` with
      ``refresh_per_second`` ticks/sec and block on raw-mode
      keypresses until ``Esc``/``q``/``Ctrl-C``/EOF. State is
      reloaded on every keystroke.

    Args:
        workspace: Project root containing ``.ea/state.json``. Defaults
            to ``Path.cwd()``.
        no_input: Skip the live loop, emit deterministic status text.
        plain: Same as ``no_input`` — caller chose plain output.
        read_key: Test seam for the raw-mode keypress reader. Defaults
            to :func:`_read_key_raw`.
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
    try:
        with Live(
            _build_layout(state),
            console=console,
            screen=True,
            refresh_per_second=refresh_per_second,
            transient=False,
        ) as live:
            while True:
                try:
                    ch = reader()
                except KeyboardInterrupt:
                    break
                if not ch:
                    break
                if ch in _EXIT_KEYS:
                    break
                live.update(_build_layout(_load_state(workspace)))
    except KeyboardInterrupt:
        pass
    return 0


__all__ = [
    "build_status_text",
    "render_layout",
    "run_tui",
]
