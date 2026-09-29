"""crash.recovery: the three doors back into a projection the console lost; none discards work.

The native frame offers exactly the reconnect protocol's three paths as a choice and adds no
fourth: reattach to the current head leaves the console in ``GAP`` until it reconciles,
replay from the last acknowledged sequence leaves it ``REPLAYING``, and attaching read-only
leaves it on an ``OFFLINE SNAPSHOT``. What a door cannot recover is a span of sequence
numbers from the cursor the console last held; a cost no producer estimates reads unknown.
"""

from __future__ import annotations

from eawf.kernel.projection.connection import ConnectionValue
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.session import conn_label

_KEYS = route_pairs("crash.recovery")


def _doors(revision: int) -> tuple[list[str], ...]:
    """Return each door, what it costs and what it cannot recover past ``revision``."""
    return (
        ["reattach", "≈4s", f"events after {group(revision)}"],
        ["replay", "≈90s from 41,190", "nothing — exact to the point"],
        ["read-only", "≈1s", "no mutation until you attach"],
    )


def doors(cursor: int) -> tuple[tuple[str, str, str], ...]:
    """Return each door: its name, what it cannot recover past ``cursor``, and where it leaves you.

    The span a door cannot recover is stated from the cursor the console last held, the
    one sequence number it knows; the head it would reach is the daemon's to state.
    """
    after = group(cursor + 1)
    return (
        ("reattach", f"events from {after} on, until reconciled", conn_label(ConnectionValue.GAP)),
        (
            "replay",
            f"nothing · replays from {after} exactly",
            conn_label(ConnectionValue.REPLAYING),
        ),
        (
            "read-only",
            "nothing · no mutation until you attach",
            conn_label(ConnectionValue.OFFLINE_SNAPSHOT),
        ),
    )


def _active(model: RouteReadModel) -> int:
    """Return how many Runs had not ended at the cursor the console last held."""
    ended = TERMINAL_STATUSES[LifecycleEntity.RUN]
    return sum(
        1
        for row in model.rows
        if not (
            row.field("status").state is TruthState.KNOWN and row.field("status").value in ended
        )
    )


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return the Recovery frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The route's read model at the cursor the console last held: the Runs.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    cursor = int(model.source_cursor)
    choices = doors(cursor)
    dv.sel_in(s, len(choices))
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, "Recovery"),
        summary="The console stopped · the daemon did not",
    )
    # below the wide frame the connection a door leaves is stated once, on the CHOSEN
    # line, so the three packet columns keep their room
    wide = view.wide
    grid = Grid([13, 13, 44, 0] if wide else [13, 13, 0])
    heads = ["DOOR", "COSTS", "CANNOT RECOVER", *(["LEAVES YOU"] if wide else [])]
    body = [
        label("HAPPENED", f"The console lost its projection after event {group(cursor)}."),
        more(f"Agents kept working · {dv.plural(_active(model), 'run')} were active then"),
        thin(w),
        grid.head(heads),
    ]
    body.extend(
        grid.row([name, UNKNOWN_WORD, lost, *([leaves] if wide else [])], i == s.sel, w)
        for i, (name, lost, leaves) in enumerate(choices)
    )
    name, lost, leaves = choices[s.sel]
    body += [
        thin(w),
        label("CHOSEN", f"{name} · leaves you {leaves} · costs {UNKNOWN_WORD}"),
        more(f"cannot recover {lost}"),
        thin(w),
        label("NO LOSS", "No door discards work; two of them defer reading it."),
    ]
    return finish(view, top, body, _KEYS)


def render(view: View) -> list[str]:
    """Return the Recovery frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    doors = _doors(view.fixture.proto.revision)
    grid = Grid([13, 21, 0])
    dv.sel_in(s, len(doors))
    body = [
        " HAPPENED     The console lost its projection at 14:02:11.",
        "              Agents kept working · 4 runs were active then",
        thin(w),
        grid.head(["DOOR", "COSTS", "CANNOT RECOVER"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(doors))
    chosen = doors[s.sel]
    body.extend(
        [
            thin(w),
            f" CHOSEN       {chosen[0]} · costs {chosen[1]}",
            f"              cannot recover {chosen[2]}",
            thin(w),
            " NO LOSS      No door discards work; two of them defer reading it.",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Recovery",
        ctx="The console stopped at 14:02:11 · the daemon did not",
        body=body,
        keys=_KEYS,
    )
