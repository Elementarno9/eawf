"""The pause overlay: a control whose outcome is unknown, stated as unknown."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.reads import can_mutate

RUN = pt.PAUSED_RUN
#: What the pause card's two verbs preview on its consequence card, by letter.
TARGETS: Mapping[str, dict[str, str]] = MappingProxyType(
    {
        "n": {
            "verb": "reconcile",
            "id": RUN,
            "kind": "run",
            "effects": "the daemon is asked again for the outcome of the pause",
            "not": "it does not restart the run and does not fail the task",
        },
        "c": {
            "verb": "cancel",
            "id": RUN,
            "kind": "run",
            "effects": "cancel is requested; it stays unknown until the run answers",
            "not": "it does not fail the task and does not undo any merged work",
        },
    }
)


def render(view: View) -> list[str]:
    """Return the pause overlay."""
    s, w = view.session, view.w
    rows = [
        header(view, f" Eä ▸ pause · {RUN}"),
        " the control outcome is unknown · we do not know",
        bar(w),
        " PAUSE     requested   14:03:11 · accepted 14:03:11",
        "           confirmed   —  the run never answered",
        thin(w),
        " RESUME PREDICATE  a heartbeat at or after 14:03:11",
        " RETRY BUDGET      2 of 3 attempts used · 1 left",
        thin(w),
        " UNKNOWN   This is not a failure and not a success. Until the run answers,",
        "           neither resume nor cancel can be confirmed — only requested.",
        thin(w),
        " NOT       Reconciling does not restart the run and does not fail the task.",
    ]
    live = [("n", "reconcile"), ("c", "cancel"), ("Esc", "back")]
    pairs = live if can_mutate(s) else [("Esc", "back")]
    return build(view, rows, keybar(pairs, w))
