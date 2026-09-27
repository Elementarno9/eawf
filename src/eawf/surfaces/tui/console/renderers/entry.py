"""entry: the pre-session layer and its eight states.

The header's state slot carries the process state because no projection exists yet, and
the keybar carries the state's own keys and ``/ attach later``. A state drawn from the
attach path adds rows only at the widths its disclosure names.
"""

from __future__ import annotations

import re
import textwrap

from eawf.surfaces.tui.console.chrome import EntryState
from eawf.surfaces.tui.console.frame import View, bar, build, entry_state, thin
from eawf.surfaces.tui.console.header import ProcessValue
from eawf.surfaces.tui.console.keybar import KeyEntry, keybar
from eawf.surfaces.tui.console.keymap import ATTACH_LATER
from eawf.surfaces.tui.console.width import cell_len, pad

_TRAIL = re.compile(r"\s+$")
# The widest paths label the column layout leaves room for.
PATHS_LABEL_MAX = 18
# The narrowest gutter a disclosure label is set in.
_DISCLOSURE_GUTTER = 10


def _header(state: EntryState, w: int) -> str:
    process = ProcessValue(glyph=state.glyph, label=state.state)
    slot = f"{process.glyph} {process.label}"
    return pad(" Eä", w - cell_len(slot)) + slot


def _table(state: EntryState, path_sel: int) -> list[str]:
    if not state.cols:
        return []
    rows = [_TRAIL.sub("", " " + "".join(pad(name, width) for name, width in state.cols))]
    for i, r in enumerate(state.rows or ()):
        cells = "".join(
            pad(r[j], width - (2 if j == 0 else 0)) for j, (_name, width) in enumerate(state.cols)
        )
        rows.append((" ▸ " if i == path_sel else "   ") + _TRAIL.sub("", cells))
    return rows


def _paths(state: EntryState, path_sel: int, w: int) -> list[str]:
    """Return the paths block.

    Raises:
        ValueError: the paths label is too wide for its column.
    """
    if not state.paths:
        return []
    label = state.paths_label or "PATHS"
    note = state.paths_note or "In the order they are meant to be run."
    if cell_len(label) > PATHS_LABEL_MAX:
        raise ValueError(f"paths label too long: {label}")
    rows = [thin(w), " " + pad(label, max(10, cell_len(label) + 1)) + note]
    for i, (name, text) in enumerate(state.paths):
        lead = name if state.paths_ordered is False else f"{i + 1}  {name}"
        rows.append("   " + ("▸ " if i == path_sel else "  ") + pad(lead, 14) + text)
    return rows


def _disclosure(state: EntryState, w: int) -> list[str]:
    """Return the labelled rows this width discloses, each block under a thin rule."""
    rows: list[str] = []
    for min_width, label, text in state.disclosure:
        if w < min_width:
            continue
        gutter = max(_DISCLOSURE_GUTTER, cell_len(label) + 2)
        lines = textwrap.wrap(text, width=w - gutter - 2) or [""]
        rows.append(thin(w))
        rows.extend(
            " " + (pad(label, gutter) if i == 0 else " " * gutter) + line
            for i, line in enumerate(lines)
        )
    return rows


def render_state(view: View, state: EntryState) -> list[str]:
    """Return the entry frame for pre-session ``state``, whatever route the session is on.

    Until a projection exists the process layer owns the whole frame, so a linked
    console waiting on its first read draws its resolving state through this too.
    """
    s, w = view.session, view.w
    rows = [_header(state, w), f" {state.title}", bar(w)]
    rows.extend(" " + pad(label, 10) + text for label, text in state.panes or ())
    rows.extend(_table(state, s.path_sel))
    rows.extend(_paths(state, s.path_sel, w))
    rows.append(thin(w))
    rows.extend(f"   {t}" if t else "" for t in state.tail)
    rows.extend(_disclosure(state, w))
    own = [KeyEntry(label, tuple(key.split())) for key, label in state.keys]
    pairs = [entry.pair() for entry in (*own, ATTACH_LATER)]
    return build(view, rows, keybar(pairs, w))


def render(view: View) -> list[str]:
    """Return the entry frame for the current pre-session state."""
    return render_state(view, entry_state(view))
