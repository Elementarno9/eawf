"""Drawers: go, actions, inspect and raw.

A drawer keeps the route frame above it and replaces the frame's tail rows and keybar with
its own rows; the app composes it. The go drawer is shown while the ``g`` prefix is armed,
not through the session's overlay.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.action_menu import menu_rows
from eawf.surfaces.tui.console.attention import menu_verbs, verb_available
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import View, entry_state
from eawf.surfaces.tui.console.renderers import copy_target
from eawf.surfaces.tui.console.session import Session

GO_ROWS: tuple[str, ...] = (
    " GO        g h  home · scope     g a  activity      g n  needs you",
    "           g b  backlog          g t  timeline      g r  release",
    "           g s  settings         g y  history       g d  health",
    "           g i  notifications    g l  sandbox log   g u  unattended",
)


def go_rows(view: View) -> list[str]:
    """Return the go drawer: the twelve destinations."""
    return list(GO_ROWS)


def action_rows(view: View) -> list[str]:
    """Return the action drawer: the route's verbs, refused ones with their live reasons."""
    s, fx = view.session, view.fixture
    return menu_rows(menu_verbs(s, fx), guard=lambda v: verb_available(s, fx, v), w=view.w)


def _inspected(session: Session, fixture: Fixture) -> str:
    return "cost ~4.62 of 20.00" if session.route == "run.detail" else copy_target(session, fixture)


def inspect_rows(view: View) -> list[str]:
    """Return the inspect drawer: the focused field with its provenance."""
    s, fx = view.session, view.fixture
    if s.route == "entry":
        state = entry_state(view)
        rows = state.rows or ()
        value = rows[s.path_sel][0] if s.path_sel < len(rows) else state.state.lower()
        return [
            f" INSPECT   value       {value}",
            "           quality     measured · as stored in the snapshot",
            "           answered by none · no projection exists yet",
            f"           revision    {group(fx.proto.revision)} · the last known revision",
            f"           freshness   snapshot · {pt.SNAPSHOT_AGE} old, not live",
        ]
    derived = s.route == "run.detail"
    return [
        f" INSPECT   value       {_inspected(s, fx)}",
        "           quality     " + ("~ derived · provider rate card" if derived else "measured"),
        f"           answered by daemon@1 · revision {group(fx.proto.revision)} · exact",
        "           freshness   as of 14:02 · within this view’s 2s target",  # noqa: RUF001
    ]


def raw_rows(view: View) -> list[str]:
    """Return the raw drawer: a bounded, scrubbed segment of the runner's own words."""
    target = dv.target_id(view.session, view.fixture)
    last = view.fixture.proto.revision
    return [
        f" RAW       {target} · the runner’s own words, quoted exactly",  # noqa: RUF001
        f"           seq {group(last - 2)}  kind=progress          ×212 coalesced",  # noqa: RUF001
        f"           seq {group(last - 1)}  kind=heartbeat         bytes=48",
        f"           seq {group(last)}  kind=tool.completed    bytes=380",
        "           bounded · scrubbed · retention-governed",
    ]


DRAWERS: Mapping[str, Callable[[View], list[str]]] = MappingProxyType(
    {"go": go_rows, "actions": action_rows, "inspect": inspect_rows, "raw": raw_rows}
)
