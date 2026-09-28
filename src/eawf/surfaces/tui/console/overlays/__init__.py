"""Full-frame overlays: one module per overlay, bound here by name.

An overlay module provides ``render(view) -> list[str]``, which returns the whole frame
with its own header crumb and keybar from the prototype registers. An overlay bound to a
record the console holds is drawn from that record instead
(:mod:`~eawf.surfaces.tui.console.overlays.drawn`). A drawer is not an overlay: it keeps
the route frame and replaces its tail, and the app composes it.

An overlay that does not hold what it draws is drawn by the chassis instead
(:func:`~eawf.surfaces.tui.console.overlays.chassis.unheld_frame`), so it still holds the
frame rather than letting the route's unknown frame through.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.overlays import (
    acceptance,
    consequence,
    draft,
    evidence,
    help_card,
    marker,
    palette,
    pause,
    question,
    readiness,
    resolution,
)
from eawf.surfaces.tui.console.overlays.chassis import holds, unheld_frame
from eawf.surfaces.tui.console.overlays.drawn import draw_overlay
from eawf.surfaces.tui.console.registry import OVERLAYS

Render = Callable[[View], list[str]]

OVERLAY_RENDERERS: Mapping[str, Render] = MappingProxyType(
    {
        "help": help_card.render,
        "palette": palette.render,
        "consequence": consequence.render,
        "question": question.render,
        "pause": pause.render,
        "evidence": evidence.render,
        "acceptance": acceptance.render,
        "readiness": readiness.render,
        "resolution": resolution.render,
        "draft": draft.render,
        "marker": marker.render,
    }
)

if set(OVERLAY_RENDERERS) != set(OVERLAYS):
    raise ValueError("the overlay renderers and the registry's overlay set disagree")


def is_overlay(name: str | None) -> bool:
    """Return whether ``name`` is a full-frame overlay."""
    return name in OVERLAY_RENDERERS


def render_overlay(name: str, view: View) -> list[str]:
    """Return the frame of overlay ``name``: its bound record's, else the registers', else held.

    Raises:
        KeyError: ``name`` is not an overlay.
    """
    render = OVERLAY_RENDERERS[name]
    drawn = draw_overlay(name, view)
    if drawn is not None:
        return drawn
    if not holds(name, view.session, view.fixture):
        return unheld_frame(view, name)
    return render(view)
