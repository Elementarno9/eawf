"""history.diff: one entity at two revisions, field by field.

``p`` cycles the revision pair and ``e`` opens the entity the diff is about.

The native frame diffs one entity at exactly two of its own revisions -- the one the read
model holds and the one before it -- never a range. The earlier value and who caused the
change are the canonical-state store's to state; while no producer reads them into the
console they render unknown, and a change is never attributed to the system. The
unchanged fields are counted here and never hidden, so their count is unknown too rather
than left out. An entity at its first revision has nothing to pair and says so.
"""

from __future__ import annotations

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    UNKNOWN_WORD,
    finish,
    label,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held

PAIRS: tuple[str, ...] = pt.DIFF_PAIRS
ENTITY = pt.DIFF_ENTITY
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "field"),
    ("Enter", "field"),
    ("e", "entity"),
    ("p", "revisions"),
    ("Esc", "back"),
)


def _subject(view: View, spine: SpineView) -> SpineRow | None:
    """Return the one entity the diff is about: the session's subject, else the first record."""
    found = spine.index_of(view.session.subj_id)
    if found is not None:
        return spine.rows[found]
    return spine.rows[0] if spine.rows else None


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return the History diff frame drawn from the corpus the daemon served.

    Args:
        view: The render being built; its session's subject names the entity.
        spine: The diagnostics corpus at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    subject = _subject(view, spine)
    key = subject.key if subject is not None else "no entity"
    paired = subject is not None and subject.revision > 1
    pair = f"rev {subject.revision - 1} → rev {subject.revision}" if paired and subject else ""
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(spine, "History", "Diff"),
        summary=f"{key} · {pair or 'one revision'} · cursor {group(int(spine.source_cursor))}",
    )
    body = [
        label(
            "SUBJECT",
            f"{key} · {subject.title or subject.collection.value}"
            if subject
            else f"{UNAVAILABLE} · no entity is held to diff",
        )
    ]
    grid = Grid([12, 17, 17, 0])
    if subject is not None and paired:
        body.append(label("BETWEEN", f"{pair} · both instants {UNKNOWN_WORD}"))
        body += [thin(w), grid.head(["FIELD", "THEN", "NOW", "CAUSED BY"])]
        dv.sel_in(s, 1)
        now = value_cell(subject.field("status")).slot
        body.append(grid.row(["status", UNKNOWN_WORD, now, UNKNOWN_WORD], s.sel == 0, w))
    else:
        dv.sel_in(s, 0)
        body.append(label("BETWEEN", "one revision only · nothing to pair it with"))
    body += [
        thin(w),
        label("UNCHANGED", f"{UNKNOWN_WORD} fields · counted here, never hidden"),
        label("RULE", "One entity at two revisions — the subject is never a range."),
    ]
    return finish(view, top, body, _KEYS)


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
    """Open the diffed entity on ``e`` and cycle the revision pair on ``p``."""
    s = ctx.s
    if busy(s):
        return False
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
