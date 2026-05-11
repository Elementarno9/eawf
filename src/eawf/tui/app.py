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

The v0.3 deliverable is the scaffold — :func:`run_tui` opens a one-shot
``rich.Live`` view and exits cleanly on ``Esc``. No state mutation; the
view re-renders on each tick from the current ``state.json``.

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
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

logger = logging.getLogger(__name__)


_HEADER_BRAND: str = "Eä"
_FOOTER_KEYMAP: str = "↑↓ navigate · Enter select · Esc quit"


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
    except (json.JSONDecodeError, OSError):
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


def run_tui(
    *,
    workspace: Path | None = None,
    no_input: bool = False,
    plain: bool = False,
) -> int:
    """Open the live Rich TUI or emit a single-shot status fallback.

    Returns:
        Exit code (``0`` on clean shutdown, non-zero reserved for
        future error paths).
    """
    state = _load_state(workspace)
    if no_input or plain or not _is_tty():
        text = build_status_text(state)
        print(text)
        return 0
    console = Console()
    layout = _build_layout(state)
    console.print(layout)
    return 0


__all__ = [
    "build_status_text",
    "render_layout",
    "run_tui",
]
