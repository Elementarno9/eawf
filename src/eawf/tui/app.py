"""Rich-backed TUI for ``eawf tui`` (P14-W10 / D15 + D23).

Layout sketch (rich.Layout):

::

    +----------------------------------------------------------+
    | Eä  EAWF / P14 / P14-I01                          v0.3   |  ← header
    +----------------------------------------------------------+
    | state.json: 1 phase open · 0 waves pending · 0 audits   |
    | hypothesis pane · audit pane · ship pane (placeholders) |
    +----------------------------------------------------------+
    | ↑↓ navigate · Enter select · Esc quit                    |  ← footer
    +----------------------------------------------------------+

Per ``feedback_tui_branding`` memory the header brand is literal ``Eä``
(capital E + a-umlaut), bold-accent, *outside-left* of the scope
breadcrumb. Per ``feedback_tui_keymap_conventions`` the keymap lists
arrow keys first with full-name aliases, never vim-only shortcuts.

The v0.3 deliverable is the scaffold — :func:`run_tui` opens a
``rich.Live`` view and blocks on raw-mode keypresses until ``Esc`` /
``q`` / ``Ctrl-C``. No state mutation; the view re-renders on every
keystroke from the current ``state.json``.

For non-TTY callers (``--plain``, ``--no-input``, or stdout is not a
tty) :func:`run_tui` falls back to :func:`build_status_text` — a
deterministic single-shot text summary the bare ``eawf`` invocation
routes through when the operator has no terminal.
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
from rich.panel import Panel
from rich.text import Text

logger = logging.getLogger(__name__)


_HEADER_BRAND: str = "Eä"
_FOOTER_KEYMAP: str = "↑↓ navigate · Enter select · Esc quit"
_EXIT_KEYS: frozenset[str] = frozenset({"\x1b", "q", "Q", "\x03", "\x04"})


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
        return json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return {}


def _breadcrumb(state: dict[str, Any]) -> str:
    """Build the ``scope/phase/iter`` breadcrumb from state."""
    project = (state.get("project") or {}).get("code") or "EAWF"
    current = state.get("current") or {}
    phase = current.get("phase_id")
    iter_id = current.get("iter_id")
    parts = [project]
    if phase:
        parts.append(phase)
    if iter_id:
        parts.append(iter_id)
    return " / ".join(parts)


def _summary_counts(state: dict[str, Any]) -> dict[str, int]:
    """Count phases, iters, waves, audits visible in state."""
    return {
        "phases_open": sum(
            1 for p in (state.get("phases") or {}).values() if p.get("status") == "active"
        ),
        "iters_open": sum(
            1 for it in (state.get("iters") or {}).values() if it.get("status") == "active"
        ),
        "waves_pending": sum(
            1 for w in (state.get("waves") or {}).values() if w.get("status") == "pending"
        ),
        "audits": len(state.get("audits") or {}),
    }


def build_status_text(state: dict[str, Any]) -> str:
    """Deterministic single-shot status — used by the non-TTY fallback."""
    breadcrumb = _breadcrumb(state)
    counts = _summary_counts(state)
    return (
        f"{_HEADER_BRAND}  {breadcrumb}\n"
        f"  phases_open={counts['phases_open']} "
        f"iters_open={counts['iters_open']} "
        f"waves_pending={counts['waves_pending']} "
        f"audits={counts['audits']}\n"
        f"keymap: {_FOOTER_KEYMAP}"
    )


def _build_layout(state: dict[str, Any]) -> Layout:
    """Compose the three-row Layout used by the live TUI."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    breadcrumb = _breadcrumb(state)
    header_text = Text()
    header_text.append(f"{_HEADER_BRAND}  ", style="bold white")
    header_text.append(breadcrumb, style="cyan")
    layout["header"].update(Panel(header_text, title=None, border_style="dim"))
    counts = _summary_counts(state)
    body_text = Text(
        "\n".join(
            [
                f"phases open: {counts['phases_open']}",
                f"iters open:  {counts['iters_open']}",
                f"waves pending: {counts['waves_pending']}",
                f"audits: {counts['audits']}",
            ]
        )
    )
    layout["body"].update(Panel(body_text, title="state.json"))
    layout["footer"].update(Panel(Text(_FOOTER_KEYMAP), title=None, border_style="dim"))
    return layout


def render_layout(state: dict[str, Any], *, console: Console | None = None) -> str:
    """Render the live Layout into a string buffer.

    The smoke test consumes this so we never block on an interactive
    ``rich.Live`` loop; production callers wire :func:`run_tui` to the
    real terminal.
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
    concurrent ``rich.Live`` renderer thread.

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
) -> int:
    """Open the live Rich TUI or emit a single-shot status fallback.

    Args:
        workspace: Project root containing ``.ea/state.json``. Defaults
            to ``Path.cwd()``.
        no_input: Skip the live loop, emit deterministic status text.
        plain: Same as ``no_input`` — caller chose plain output.
        read_key: Test seam for the raw-mode keypress reader. Defaults
            to :func:`_read_key_raw`.

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
            refresh_per_second=4,
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
