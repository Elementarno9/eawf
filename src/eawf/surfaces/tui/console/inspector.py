"""The inspector: the frame Enter would open, drawn in a pane beside a list route.

At :data:`~eawf.surfaces.tui.console.frame.INSPECT_FROM` columns and wider, Activity,
Backlog and Attention draw their list on the left and, to its right, the route or card
Enter would open from the row under the cursor, rendered at the pane's own width. What
Enter opens is found by pressing Enter on a copy of the session, so the pane never shows
a place the key would not go, and Enter on the list still opens that place whole.

The pane is only a reading of the frame: the list keeps the cursor, the keybar and every
key, and the header and keybar span the whole width as on any other frame.
"""

from __future__ import annotations

from dataclasses import replace

from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.frame import INSPECT_FROM, View, paint_rack, paint_verbose
from eawf.surfaces.tui.console.header import CrumbPart, crumb_runs
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays import is_overlay, render_overlay
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

#: The list routes that set an inspector beside their list.
INSPECTED_ROUTES: frozenset[str] = frozenset({"activity", "backlog", "attention"})
#: The pane's least width: the narrowest frame any route is laid out for.
PANE_MIN_W = 80
#: The rule between the list and the pane.
DIVIDER = "│"


def pane_width(w: int) -> int:
    """Return the pane's width in a ``w``-cell frame: two fifths, never under the least.

    Args:
        w: The frame's width in cells, gutters excluded.

    Returns:
        The pane's width; the list takes the rest less the divider's cell.
    """
    return max(PANE_MIN_W, (w - 1) * 2 // 5)


def inspects(view: View) -> bool:
    """Return whether ``view``'s frame may set an inspector beside its list.

    Only a list route at :data:`INSPECT_FROM` columns or wider does, and only while the
    list holds the keys: an open overlay, a go prefix or a typed filter owns them instead.
    """
    s = view.session
    return (
        view.columns >= INSPECT_FROM
        and s.route in INSPECTED_ROUTES
        and s.overlay is None
        and not s.prefix
        and not s.typing
    )


def entered(ctx: Ctx) -> Session | None:
    """Return the session Enter would leave on the list ``ctx`` acts on, or ``None``.

    Enter is pressed on a copy of the session through the production dispatcher, with
    every path out of the console cut, so nothing is sent, copied or recovered and the
    session on screen is untouched. A toast the press raised is dropped: the rack is the
    console's, not the pane's.

    Args:
        ctx: The context a key on the list would act in.

    Returns:
        The copy after Enter, on the route or card it opened; ``None`` when Enter opened
        nothing, such as on an empty list.
    """
    s = ctx.session
    copy = s.model_copy(deep=True)
    probe = replace(
        ctx, session=copy, send=None, clipboard=None, recover=None, onboard=None, verbose=False
    )
    dispatch(probe, "Enter")
    if copy.route == s.route and copy.overlay is None:
        return None
    copy.toasts = []
    return copy


def _draw(view: View) -> list[str]:
    """Return ``view``'s route or card frame, clearing what the last render published."""
    s = view.session
    s.record_facts = None
    s.record_nav = None
    s.bucket_keys = None
    s.windowed = False
    s.nav_rows = None
    s.bar_keys = None
    overlay = s.overlay
    if overlay is not None and is_overlay(overlay):
        return render_overlay(overlay, view)
    return render_route(view)


def _heading(header: str, w: int) -> str:
    """Return ``header`` cut to its gutter and crumb leaf, padded to ``w`` cells.

    The brand, the scope and the state slot are the whole-width header's to show; the
    pane names only its subject.
    """
    runs = crumb_runs(header)
    lead = runs[0].text if runs[0].part is CrumbPart.SLOT else ""
    leaf = next((run.text for run in reversed(runs) if run.part is CrumbPart.LEAF), "")
    return pad(lead + leaf, w)


def pane_lines(view: View) -> list[str]:
    """Return the pane's rows: the inspected frame at the pane's width, less its keybar.

    The frame is drawn one row shorter than ``view``'s, so with its keybar left off it
    fills the rows between the header and the keybar exactly. Its header row is cut to
    the subject's crumb leaf, since the whole-width header already shows the rest.

    Raises:
        ValueError: ``view`` holds no inspector.
    """
    pane = view.inspector
    if pane is None:
        raise ValueError("the view holds no inspector to draw")
    w = pane_width(view.w)
    drawn = _draw(replace(pane, w=w, h=view.h - 1, gutter=0, verbose=False, inspector=None))
    return [_heading(drawn[0], w), *drawn[1:-1]]


def beside(view: View) -> list[str]:
    """Return ``view``'s list frame with the inspector pane set to the right of its list.

    The list is laid out at the width the pane leaves, as a terminal that wide would lay
    it out; the header and the keybar are the whole-width frame's, so the keys offered
    are the list's own. The rack and the verbose row are painted over the joined rows.

    Raises:
        ValueError: ``view`` holds no inspector.
    """
    pane = pane_lines(view)
    s, w, h = view.session, view.w, view.h
    list_w = w - pane_width(w) - 1
    toasts, s.toasts = s.toasts, []
    listed = _draw(replace(view, w=list_w, gutter=0, verbose=False, inspector=None))
    whole = _draw(replace(view, verbose=False, inspector=None))
    s.toasts = toasts
    out = [whole[0], *(f"{listed[i]}{DIVIDER}{pane[i - 1]}" for i in range(1, h - 1))]
    if view.verbose:
        paint_verbose(s, out, w)
    paint_rack(s, out, w, verbose=view.verbose)
    out.append(whole[-1])
    return out


__all__ = [
    "DIVIDER",
    "INSPECTED_ROUTES",
    "PANE_MIN_W",
    "beside",
    "entered",
    "inspects",
    "pane_lines",
    "pane_width",
]
