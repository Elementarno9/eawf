"""Drawers (the prototype's PANES): go, actions, inspect, raw. Discovered by module name
``chassis/drawers/<name>.py``.

Contract for a drawer module:

``render(session, fixture, w, h) -> list[str]``
    Returns ONLY the drawer's inline rows (unpadded is fine; the chassis pads them). The
    chassis keeps the route frame above, truncates it to ``h - 2 - len(rows)`` body rows
    (adding ``N more rows below · Esc closes the pane`` when content is hidden), draws a
    thin rule, the drawer rows, then the drawer's keybar from ``keys.DRAWER_PAIRS``.
    The go drawer is opened by the armed ``g`` prefix, not by ``session.overlay``.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_cache: dict[str, ModuleType | None] = {}


def module_name(name: str) -> str:
    return f"{__package__}.{name}"


def module_for(name: str) -> ModuleType | None:
    if name in _cache:
        return _cache[name]
    try:
        mod: ModuleType | None = importlib.import_module(module_name(name))
    except ModuleNotFoundError as exc:
        if exc.name != module_name(name):
            raise
        mod = None
    _cache[name] = mod
    return mod


def render_drawer(name: str, session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    mod = module_for(name)
    if mod is None or not hasattr(mod, "render"):
        return [f" NOT PORTED  drawer {name} · no module at {module_name(name)}"]
    return mod.render(session, fixture, w, h)
