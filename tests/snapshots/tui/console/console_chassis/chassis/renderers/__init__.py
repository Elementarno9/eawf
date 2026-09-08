"""Route renderers, discovered by module name: ``chassis/renderers/<route id with '.'
replaced by '_'>.py``.

Contract for a route module:

``render(session, fixture, w, h) -> list[str]``
    Returns the FULL frame: exactly ``h`` rows of ``w`` cells with the keybar as the last
    row, produced by ``chassis.frame.build(session, rows, keybar_row, w, h)`` where
    ``rows[0]`` is ``chassis.frame.header_row(...)`` and ``rows[1]`` the route subtitle.
    The chassis composes drawers, the rack and the verbose row around this frame the way
    the prototype's ``render()`` does, so a port is a transcription of ``R[route]``.

``seam(ctx, key, shift) -> bool`` (optional)
    The route's own key handling from the prototype's Phase G window-capture handler.
    Runs before the core dispatcher; return True to claim the key (the chassis then
    disarms the quit guard and re-renders). Do the route's own ``busy()`` gating inside.

``copy(session, fixture) -> str`` (optional)
    What ``y`` copies about this route's subject (the prototype's ``copyFor``).

A missing module renders a clearly marked placeholder so the app never crashes.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

from ...chassis import registry

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

Renderer = Callable[["Session", "Fixture", int, int], list[str]]
_cache: dict[str, ModuleType | None] = {}


def module_name(route: str) -> str:
    return f"{__package__}." + route.replace(".", "_")


def module_for(route: str) -> ModuleType | None:
    if route in _cache:
        return _cache[route]
    try:
        mod: ModuleType | None = importlib.import_module(module_name(route))
    except ModuleNotFoundError as exc:
        if exc.name != module_name(route):
            raise
        mod = None
    _cache[route] = mod
    return mod


def has_renderer(route: str) -> bool:
    """A route is bound when it is registered; a missing module is a placeholder, not
    an absence, so navigation to it behaves as in the prototype."""
    return route in registry.ROUTE_BY_ID


def is_ported(route: str) -> bool:
    return module_for(route) is not None


def seam_for(route: str) -> Callable[[Any, str, bool], bool] | None:
    mod = module_for(route)
    return getattr(mod, "seam", None) if mod else None


def copy_for(route: str) -> Callable[[Session, Fixture], str] | None:
    mod = module_for(route)
    return getattr(mod, "copy", None) if mod else None


def render_route(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    mod = module_for(session.route)
    if mod is None or not hasattr(mod, "render"):
        return placeholder(session, fixture, w, h)
    return mod.render(session, fixture, w, h)


def placeholder(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    """A clearly marked frame for a route nobody has ported yet."""
    from ...chassis.frame import bar, build, header_row, keybar, thin

    rows = [
        header_row(session, fixture, f" Eä ▸ {fixture.scope} ▸ {session.route}", w),
        f" NOT PORTED · {session.route} · no renderer module at {module_name(session.route)}",
        bar(w),
        " PLACEHOLDER  this frame is a stand-in drawn by chassis/renderers/__init__.py",
        "              port the prototype's R[route] into the module named above",
        thin(w),
    ]
    return build(session, rows, keybar([("Esc", "back")], w), w, h)


def clear_cache() -> None:
    _cache.clear()
