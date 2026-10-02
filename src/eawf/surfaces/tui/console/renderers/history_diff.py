"""history.diff: one entity at two revisions, field by field.

``p`` steps to an older revision pair and ``e`` opens the entity the diff is about.

The native frame diffs one entity across one recorded change, never a range: the newest
first, and ``p`` steps to each older change of the same record the feed holds, back to
the newest after the oldest. The change feed states each field the commit changed with
its value before and after, and who asked. Every field the change does not list held its
value, which the frame says rather than hiding. A value too large to keep shows its
preview and an ellipsis. A change that names nobody says so, and is never attributed to
the system. An entity with no change on file says since when none was recorded.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from eawf.kernel.identity import IdentityError, parse_qualified_urn
from eawf.kernel.projection.spine import SpineView
from eawf.kernel.store.changes import ChangeRecord, StoredValue
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.format import clock_minute, day
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.live_reads import HISTORY_DIFF_READ, diff_subject, held_changes
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.registry import COLLECTION_ROUTES
from eawf.surfaces.tui.console.renderers.history import none_since
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    UNKNOWN_WORD,
    finish,
    label,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
from eawf.surfaces.tui.console.session import Session

PAIRS: tuple[str, ...] = pt.DIFF_PAIRS
ENTITY = pt.DIFF_ENTITY
_KEYS = route_pairs("history.diff")


#: What a field the row did not carry on one side of the change reads as.
ABSENT = "absent"


def shown(value: StoredValue | None) -> str:
    """Return one side of a field change as a cell: the value, or its preview when cut.

    A reference is shown as the key it addresses, because a full address is wider than
    the cell and an identifier never gives way to the cell's edge.
    """
    if value is None:
        return ABSENT
    if value.cut:
        return f"{value.value}…"
    if not isinstance(value.value, str):
        return json.dumps(value.value)
    try:
        return parse_qualified_urn(value.value).entity_key
    except IdentityError:
        return value.value


def _pair(change: ChangeRecord) -> str:
    """Return the revisions a change moved the record between."""
    if change.revision_before is None:
        return f"created at rev {change.revision_after}"
    return f"rev {change.revision_before} → rev {change.revision_after}"


def _changes(live: Mapping[str, object], key: str | None) -> tuple[ChangeRecord, ...]:
    """Return the held changes of record ``key``, newest first."""
    page = held_changes(live, HISTORY_DIFF_READ)
    return tuple(c for c in page.changes if c.record_key == key) if page is not None else ()


def _shown_at(s: Session, changes: tuple[ChangeRecord, ...]) -> int:
    """Return the place of the change the diff shows: the stepped-to one, else the newest."""
    ids = [c.change_id for c in changes]
    return ids.index(s.diff_change) if s.diff_change in ids else 0


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return the History diff frame drawn from the corpus the daemon served.

    Args:
        view: The render being built; its session's subject names the entity.
        spine: The diagnostics corpus at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    key = diff_subject(s.subj_id or s.sel_id, [row.key for row in spine.rows])
    found = spine.index_of(key)
    subject = spine.rows[found] if found is not None else None
    page = held_changes(view.live, HISTORY_DIFF_READ)
    changes = _changes(view.live, key)
    place = _shown_at(s, changes)
    change = changes[place] if changes else None
    pair = _pair(change) if change is not None else ""
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(view, spine, "History", "Diff"),
        summary=f"{key or 'no entity'} · {pair or 'no change on file'}",
    )
    named = subject.title or subject.collection.value if subject is not None else "a record"
    body = [
        label(
            "SUBJECT", f"{key} · {named}" if key else f"{UNAVAILABLE} · no entity is held to diff"
        )
    ]
    grid = Grid([12, 17, 17, 0])
    if change is not None:
        at = f"{day(change.recorded_at)} {clock_minute(change.recorded_at)}"
        body.append(label("BETWEEN", f"{pair} · {at} · {change.event_name}"))
        body += [thin(w), grid.head(["FIELD", "THEN", "NOW", "CAUSED BY"])]
        dv.sel_in(s, len(change.changes))
        cause = change.actor_ref or UNKNOWN_WORD
        body.extend(
            grid.row([item.field, shown(item.before), shown(item.after), cause], s.sel == i, w)
            for i, item in enumerate(change.changes)
        )
        body += [
            thin(w),
            label("UNCHANGED", "every field not listed held its value · none is hidden"),
            label(
                "EARLIER",
                f"{dv.plural(len(changes) - 1 - place, 'earlier change')} on file"
                f" · change {place + 1} of {len(changes)}",
            ),
        ]
    else:
        dv.sel_in(s, 0)
        if page is None:
            body.append(label("BETWEEN", f"{UNKNOWN_WORD} · the change feed has not been read yet"))
        else:
            body.append(label("BETWEEN", none_since(page.since)))
    body.append(label("RULE", "One entity at two revisions — the subject is never a range."))
    # p steps only between two changes, and e opens only a held entity with a frame
    idle = {"p"} if len(changes) < 2 else set()
    if subject is None or subject.collection not in COLLECTION_ROUTES:
        idle.add("e")
    return finish(view, top, body, [pair for pair in _KEYS if pair[0] not in idle])


def render(view: View) -> list[str]:
    """Return the History diff frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, w = view.session, view.w
    unchanged = "– unchanged"  # noqa: RUF001
    fields = [
        ["state", chip("ok", "RUNNING"), chip("wn", "WAIT-PERM"), "the agent"],
        ["reason", "run active", "needs permission", "the agent"],
        ["attempt", "1 of 1", "1 of 1", unchanged],
        ["cost", "~3.10", "~4.62", "rate card"],
        ["peak rss", "∅ uncertified", "∅ uncertified", unchanged],
    ]
    dv.sel_in(s, len(fields))
    grid = Grid([12, 17, 17, 0])
    body = [
        f" SUBJECT      {ENTITY} · EAWF-0042 Bound replay",
        pt.DIFF_BETWEEN,
        thin(w),
        grid.head(["FIELD", "THEN", "NOW", "CAUSED BY"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(fields))
    body.extend(
        [
            thin(w),
            " UNCHANGED    11 fields · counted here, never hidden",
            " RULE         One entity at two revisions — the subject is never a range.",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ History ▸ Diff",
        ctx=f"{ENTITY} · {s.diff_pair or PAIRS[0]} · 4 fields changed",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the diffed entity on ``e`` and step to an older revision pair on ``p``."""
    s = ctx.s
    if busy(s):
        return False
    spine = ctx.projection
    if isinstance(spine, SpineView):
        return _native_key(ctx, spine, key)
    if key == "e":
        go(ctx, "run.detail", "the entity this diff is about", ENTITY)
        return True
    if key == "p":
        cur = s.diff_pair or PAIRS[0]
        at = PAIRS.index(cur) if cur in PAIRS else -1
        s.diff_pair = PAIRS[(at + 1) % len(PAIRS)]
        ctx.notify(s.diff_pair, title="revisions")
        ctx.log("p", f"revisions → {s.diff_pair}")
        return True
    return False


def _native_key(ctx: Ctx, spine: SpineView, key: str) -> bool:
    """Open the held entity on ``e``; step to the record's next older change on ``p``."""
    s = ctx.s
    subject = diff_subject(s.subj_id or s.sel_id, [row.key for row in spine.rows])
    if key == "e":
        found = spine.index_of(subject)
        route = COLLECTION_ROUTES.get(spine.rows[found].collection) if found is not None else None
        if route is None or subject is None:
            ctx.noop(key)
        else:
            go(ctx, route, "the entity this diff is about", subject)
        return True
    if key == "p":
        changes = _changes(ctx.live, subject)
        if len(changes) < 2:
            ctx.noop(key)
            return True
        step = changes[(_shown_at(s, changes) + 1) % len(changes)]
        s.diff_change, s.sel = step.change_id, 0
        ctx.log("p", f"revisions → {_pair(step)}")
        return True
    return False
