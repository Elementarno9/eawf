"""Full-frame overlays: one module per overlay, bound here by name.

An overlay module provides ``render(view) -> list[str]``, which returns the whole frame
with its own header crumb and keybar. The four decision overlays end with their state
rows (:mod:`~eawf.surfaces.tui.console.overlays.states`). A drawer is not an overlay: it
keeps the route frame and replaces its tail, and the app composes it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.overlays import (
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
    """Return the frame of overlay ``name``.

    Raises:
        KeyError: ``name`` is not an overlay.
    """
    return OVERLAY_RENDERERS[name](view)
