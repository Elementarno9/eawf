"""entry: the pre-session layer and its eight states.

The header's state slot carries the process state because no projection exists yet, and
the keybar carries the state's own keys and ``/ attach later``.
"""

from __future__ import annotations

import re

from eawf.surfaces.tui.console.frame import View, bar, build, entry_state, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import entry_keys
from eawf.surfaces.tui.console.width import cell_len, pad

_TRAIL = re.compile(r"\s+$")
# The widest paths label the column layout leaves room for.
PATHS_LABEL_MAX = 18


def _header(view: View) -> str:
    state = entry_state(view)
    slot = f"{state.glyph} {state.state}"
    return pad(" Eä", view.w - cell_len(slot)) + slot


def _table(view: View) -> list[str]:
    s = view.session
    state = entry_state(view)
    if not state.cols:
        return []
    rows = [_TRAIL.sub("", " " + "".join(pad(name, width) for name, width in state.cols))]
    for i, r in enumerate(state.rows or ()):
        cells = "".join(
            pad(r[j], width - (2 if j == 0 else 0)) for j, (_name, width) in enumerate(state.cols)
        )
        rows.append((" ▸ " if i == s.path_sel else "   ") + _TRAIL.sub("", cells))
    return rows


def _paths(view: View) -> list[str]:
    """Return the paths block.

    Raises:
        ValueError: the paths label is too wide for its column.
    """
    s, w = view.session, view.w
    state = entry_state(view)
    if not state.paths:
        return []
    label = state.paths_label or "PATHS"
    note = state.paths_note or "In the order they are meant to be run."
    if cell_len(label) > PATHS_LABEL_MAX:
        raise ValueError(f"paths label too long: {label}")
    rows = [thin(w), " " + pad(label, max(10, cell_len(label) + 1)) + note]
    for i, (name, text) in enumerate(state.paths):
        lead = name if state.paths_ordered is False else f"{i + 1}  {name}"
        rows.append("   " + ("▸ " if i == s.path_sel else "  ") + pad(lead, 14) + text)
    return rows


def render(view: View) -> list[str]:
    """Return the entry frame for the current pre-session state."""
    s, w = view.session, view.w
    state = entry_state(view)
    rows = [_header(view), f" {state.title}", bar(w)]
    rows.extend(" " + pad(label, 10) + text for label, text in state.panes or ())
    rows.extend(_table(view))
    rows.extend(_paths(view))
    rows.append(thin(w))
    rows.extend(f"   {t}" if t else "" for t in state.tail)
    pairs = [entry.pair() for entry in entry_keys(s, view.fixture)]
    return build(view, rows, keybar(pairs, w))
