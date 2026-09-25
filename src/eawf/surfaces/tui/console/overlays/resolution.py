"""The resolution card: why a purged target no longer resolves and what still reads."""

from __future__ import annotations

from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar

TARGET = pt.PURGED_RUN


def render(view: View) -> list[str]:
    """Return the resolution card."""
    w = view.w
    rows = [
        header(view, f" Eä ▸ resolution · {TARGET}"),
        " this target no longer resolves",
        bar(w),
        f" FACT      {TARGET} events purged · revision 41,002",
        thin(w),
        " ENDING    purged · retention 7d",
        "           the fact remains; only its events are gone",
        thin(w),
        " WHAT WORKS  The run, its result and its lineage stay readable.",
        "             its timeline and raw segment do not, and never will",
        thin(w),
        " NOT       This is not missing, moved, retired or denied — four other",
        "           endings, each with its own card and its own remedy.",
    ]
    return build(view, rows, keybar([("Y", "copy URN"), ("Esc", "back")], w))
