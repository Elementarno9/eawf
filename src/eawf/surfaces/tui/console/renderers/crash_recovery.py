"""crash.recovery: the three doors back into a projection the console lost; none discards work.

The native frame offers exactly the reconnect protocol's three paths as a choice and adds no
fourth: reattach to the current head leaves the console in ``GAP`` until it reconciles,
replay from the last acknowledged sequence leaves it ``REPLAYING``, and attaching read-only
leaves it on an ``OFFLINE SNAPSHOT``. What a door cannot recover is a span of sequence
numbers from the cursor the console last held, and what it costs is counted in reads from
that same cursor: one read of the head, every event after the cursor, or no read at all.
Enter takes the door under the cursor through the seam, and nothing else takes one.

The ``DAEMON`` line states the other half of a crash: what the daemon's own last start
repaired before it answered, and how long that took, from the record that start wrote.
"""

from __future__ import annotations

from eawf.kernel.projection.connection import ConnectionValue
from eawf.kernel.projection.operations import CrashRecoveryReadModel
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.runtime.boot_recovery import BootRecovery
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import clock_time, group
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.renderers.read_model import (
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.seam import READ_ONLY_DOOR, REATTACH_DOOR, REPLAY_DOOR
from eawf.surfaces.tui.console.session import conn_label
from eawf.surfaces.tui.console.tokens import Severity

#: What Enter says when the console holds no daemon link to take a door through.
NO_LINK = "the console holds no daemon link · no door can be taken"

_KEYS = route_pairs("crash.recovery")

#: What the ``DAEMON`` line says when no start on this machine has recorded its recovery.
NO_START_RECORDED = "no daemon start has recorded its recovery on this machine yet"

#: What the ``DAEMON`` line says for a start that found nothing torn.
CLEAN_START = "clean · nothing was torn, so nothing was repaired"


def daemon_line(last: BootRecovery | None) -> str:
    """Return what the daemon's last start repaired and what that cost, in words."""
    if last is None:
        return NO_START_RECORDED
    repairs = [
        f"cut {dv.plural(last.truncated_ledgers, 'torn ledger')}" if last.truncated_ledgers else "",
        f"finished {dv.plural(last.finished_intents, 'intent')}" if last.finished_intents else "",
        f"abandoned {dv.plural(last.abandoned_intents, 'intent')}"
        if last.abandoned_intents
        else "",
        f"dropped {dv.plural(last.document_rows_dropped, 'compacted row')}"
        if last.document_rows_dropped
        else "",
    ]
    done = " · ".join(item for item in repairs if item) or CLEAN_START
    return f"last start {clock_time(last.started_at)} · {done} · {last.duration_ms()} ms"


def _doors(revision: int) -> tuple[list[str], ...]:
    """Return each door, what it costs and what it cannot recover past ``revision``."""
    return (
        ["reattach", "≈4s", f"events after {group(revision)}"],
        ["replay", "≈90s from 41,190", "nothing — exact to the point"],
        ["read-only", "≈1s", "no mutation until you attach"],
    )


def doors(cursor: int) -> tuple[tuple[str, str, str, str], ...]:
    """Return each door: its name, its cost, what it cannot recover and where it leaves you.

    The span a door cannot recover and the reads it costs are both stated from the cursor
    the console last held, the one sequence number it knows; the head it would reach is
    the daemon's to state.
    """
    after = group(cursor + 1)
    return (
        (
            REATTACH_DOOR,
            "one head read",
            f"events from {after} on, until reconciled",
            conn_label(ConnectionValue.GAP),
        ),
        (
            REPLAY_DOOR,
            f"all from {after}",
            f"nothing · replays from {after} exactly",
            conn_label(ConnectionValue.REPLAYING),
        ),
        (
            READ_ONLY_DOOR,
            "no read",
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


def native_frame(view: View, model: CrashRecoveryReadModel) -> list[str]:
    """Return the Recovery frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The route's read model at the cursor the console last held: the Runs, and
            the daemon's last start.

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
    grid = Grid([13, 17, 44, 0] if wide else [13, 17, 0])
    heads = ["DOOR", "COSTS", "CANNOT RECOVER", *(["LEAVES YOU"] if wide else [])]
    body = [
        label("HAPPENED", f"The console lost its projection after event {group(cursor)}."),
        more(f"Agents kept working · {dv.plural(_active(model), 'run')} were active then"),
        label("DAEMON", daemon_line(model.last_start)),
        thin(w),
        grid.head(heads),
    ]
    body.extend(
        grid.row([name, cost, lost, *([leaves] if wide else [])], i == s.sel, w)
        for i, (name, cost, lost, leaves) in enumerate(choices)
    )
    name, cost, lost, leaves = choices[s.sel]
    body += [
        thin(w),
        label("CHOSEN", f"{name} · leaves you {leaves} · costs {cost}"),
        more(f"cannot recover {lost}"),
        thin(w),
        label("NO LOSS", "No door discards work; two of them defer reading it."),
    ]
    return finish(view, top, body, _KEYS)


def render(view: View) -> list[str]:
    """Return the Recovery frame, native when a read model is held."""
    model = native(view)
    # the Recovery route's read model always carries the daemon's last start
    if isinstance(model, CrashRecoveryReadModel):
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


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Take the door under the cursor on Enter, through the seam and never on its own.

    The door is the one the frame marks, read from the same cursor the frame drew; the
    connection value it leaves the console in arrives with the next render. A console
    with no daemon link has no door to take and says so.

    Args:
        ctx: The keystroke's context.
        key: The dispatcher name of the key pressed.
        shift: Whether shift was held; the frame binds no shifted key.

    Returns:
        Whether the key was claimed.
    """
    s = ctx.s
    if s.route != "crash.recovery" or key != "Enter" or shift or busy(s):
        return False
    model = ctx.projection
    if ctx.recover is None or not isinstance(model, RouteReadModel):
        ctx.notify(NO_LINK, "recovery", Severity.WARN)
        ctx.log(key, NO_LINK)
        return True
    choices = doors(int(model.source_cursor))
    name, _cost, _lost, leaves = choices[min(max(s.sel, 0), len(choices) - 1)]
    ctx.recover(name)
    ctx.log(key, f"door {name} · leaves you {leaves}")
    return True
