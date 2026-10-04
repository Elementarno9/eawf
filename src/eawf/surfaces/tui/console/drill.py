"""The keys of a frame drawn from a read model: every drill carries the row the caret names.

A native frame publishes the key of the row under its caret as the session's ``sel_id``,
so a drill opens exactly that record and nothing else: never the row at the same offset in
another list, never a prototype id, and never nothing, which would leave the destination to
guess its own subject. A frame whose caret names no row -- an empty list, a filter that
hides every row -- drills nowhere and says so.

Scope home walks a tree whose Track rows are containers rather than destinations, so its
cursor lands on Milestone leaves only; Tab moves the focus to the attention list when the
list holds something, and says there is nothing to focus when it does not. A Run frame is
about one Run for as long as it is open: its arrows never step to the next Run in the
register, and while no event of the Run is recorded they say so rather than moving.

The containment chain is the one the read model states: each row names the record it is
filed under, so ``u`` and an Escape with no history climb a Run to its Task, a Task to its
Batch, a Batch to its Milestone and a Milestone to its Track, and ``[ ]`` walk the rows
filed under the same parent. A surface opened on a record -- Git or Trust on the Run or
Milestone it was opened from -- climbs back to that record. Nothing on this path reads a
prototype record.

A key some handler claims but that changed nothing on screen raises a toast naming why,
because a key-log line the frame never draws reads as a key that does nothing. A motion
key the bar does not offer is refused instead, and one at the edge of its list is plainly
at the edge.

The prototype registers the golden contract replays keep their own keys in the
dispatcher; nothing here runs for them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.kernel.projection.attention import AttentionItem
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.route_view import RouteRecord
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.action_menu import MenuVerb
from eawf.surfaces.tui.console.clock import DISARMED
from eawf.surfaces.tui.console.keybar import KEY_NAMES
from eawf.surfaces.tui.console.keymap import ALIASES, unserved
from eawf.surfaces.tui.console.navigation import Ctx, busy, go, open_overlay, remember
from eawf.surfaces.tui.console.registry import COLLECTION_ROUTES, REGISTRY, route_for_id
from eawf.surfaces.tui.console.renderers.cost_ceiling import held_ceiling, stopped_key
from eawf.surfaces.tui.console.renderers.run_detail import open_transcript
from eawf.surfaces.tui.console.renderers.scope_home import (
    ATTENTION_REGION,
    OUTCOMES_REGION,
    HomeAttention,
    groups_of,
    leaves_of,
)
from eawf.surfaces.tui.console.renderers.spine import offered_verbs
from eawf.surfaces.tui.console.renderers.timeline import LANES, UNDATED
from eawf.surfaces.tui.console.session import LogEntry
from eawf.surfaces.tui.console.tokens import Severity

HOME = "scope.home"
RUN_DETAIL = "run.detail"
#: What Enter says when the caret names no row: an empty list, or a filter that hides all.
NOTHING_SELECTED = "nothing is selected · nothing to drill"
#: What Tab says on scope home when the attention list holds nothing to focus.
NOTHING_WAITING = "nothing is waiting — no list to focus"
#: What the Run frame's event keys say: the frame draws no timeline, the transcript does.
NO_EVENTS = "this frame walks no event · the transcript draws them · Enter opens it"
#: What Tab and Shift-Tab say on a frame with nothing for either to cycle.
NOTHING_CYCLES = "nothing here cycles · Shift-Tab steps back where Tab moves on"
#: How a toast answering a key names it in its title: as the keybar spells it.
KEY_WORDS: Mapping[str, str] = MappingProxyType({**KEY_NAMES, " ": "Space"})

_ARROWS = ("ArrowUp", "ArrowDown")
_PAGING = ("PageUp", "PageDown", "Home", "End")
_EVENT_KEYS = frozenset({*_ARROWS, *_PAGING})

Held = ProjectionRow | RouteRecord | SpineRow

#: Where Enter on a list route opens the row under the caret when neither the row's
#: collection nor its id prefix names a route of its own.
ROW_DRILLS: Mapping[str, str] = MappingProxyType(
    {
        "activity": RUN_DETAIL,
        "unattended": RUN_DETAIL,
        "track": "milestone",
        "milestone": "batch.detail",
        "batch.detail": "task.detail",
        "task.detail": RUN_DETAIL,
        "release": "milestone",
        "search": "milestone",
        HOME: "milestone",
    }
)


def is_native(ctx: Ctx) -> bool:
    """Return whether the frame under the key was drawn from a read model.

    A console holding the prototype registers and no read model is the golden replay,
    whose keys stay the dispatcher's.
    """
    return ctx.projection is not None or not ctx.fixture.prototype


def drill_to(ctx: Ctx, dest: str, subject: str | None) -> None:
    """Open ``dest`` onto ``subject``, pushing the current place so Escape returns to it."""
    s = ctx.s
    s.back.record(remember(s))
    s.region = None
    s.route = dest
    s.sel = 0
    s.subj_id = subject or None
    s.sel_id = None
    ctx.log("Enter", f"drill → {dest}" + (f" · {subject}" if subject else ""))


def claim(ctx: Ctx, key: str, shift: bool) -> bool:
    """Handle a key a native frame owns, before the route's own hook and the dispatcher.

    Args:
        ctx: The keystroke's context.
        key: The key, alias already resolved.
        shift: Whether Shift was held.

    Returns:
        Whether the key was claimed here.
    """
    s = ctx.s
    if busy(s) or ctx.unheld or (shift and key != "Tab") or not is_native(ctx):
        return False
    if s.route == RUN_DETAIL and key in _EVENT_KEYS:
        ctx.notify(NO_EVENTS, KEY_WORDS.get(key, key))
        ctx.log(key, NO_EVENTS)
        return True
    offered = s.bar_keys
    paged = key in _PAGING and s.windowed
    if key in _EVENT_KEYS and offered is not None and key not in offered and not paged:
        # the frame draws no cursor for the key to move, so it is not bound here; a table
        # cut to its window still pages where a narrow bar gave the pairs up for width
        ctx.noop(key)
        return True
    if key == "." and ctx.fixture.menus.verbs(s.route) and offered_verb(ctx, None) is None:
        # a finished subject whose menu holds only lifecycle verbs has nothing to open
        ctx.noop(key)
        return True
    if s.route == HOME and isinstance(ctx.projection, SpineView):
        return _home_key(ctx, key, ctx.projection)
    if key in unserved(s.route, s.subj_id):
        ctx.noop(key)
        return True
    if key == "Enter":
        handler = _ENTER.get(s.route)
        return handler is not None and handler(ctx)
    return False


# ---------- scope home ----------


def _mine(ctx: Ctx) -> tuple[AttentionItem, ...]:
    """Return the open attention items this principal may act on, as the home list shows."""
    held = ctx.attention
    register = build_register_view(held) if held is not None else None
    written = register if register is not None and not register.withheld else None
    return HomeAttention(written, ctx.principal).mine()


def _home_key(ctx: Ctx, key: str, spine: SpineView) -> bool:
    """Walk the tree's leaves or the attention list, swap them on Tab, open on Enter."""
    s = ctx.s
    mine = _mine(ctx)
    if key == "Tab":
        if not mine:
            ctx.notify(NOTHING_WAITING, "attention")
            ctx.log("Tab", NOTHING_WAITING)
            return True
        on_list = s.home_region != ATTENTION_REGION
        s.home_region = ATTENTION_REGION if on_list else OUTCOMES_REGION
        s.home_sel = 0
        note = "what wants you · ↑↓ picks · Enter opens it" if on_list else "outcomes"
        ctx.log("Tab", f"focus → {note}")
        return True
    if s.home_region == ATTENTION_REGION and mine:
        return _attention_key(ctx, key, mine)
    return _tree_key(ctx, key, spine)


def _attention_key(ctx: Ctx, key: str, mine: tuple[AttentionItem, ...]) -> bool:
    """Walk, open or leave the attention list while it holds the focus."""
    s = ctx.s
    s.home_sel = max(0, min(s.home_sel, len(mine) - 1))
    if key in _ARROWS:
        step = 1 if key == "ArrowDown" else -1
        s.home_sel = max(0, min(len(mine) - 1, s.home_sel + step))
        ctx.log(key, mine[s.home_sel].key)
        return True
    if key == "Enter":
        item = mine[s.home_sel]
        if go(ctx, "attention", f"what wants you · {item.key}"):
            # the Attention frame restores its cursor by id, so it opens on this item
            s.sel_id = item.key
        return True
    if key == "Escape":
        s.home_region = OUTCOMES_REGION
        ctx.log("Esc", "focus → outcomes")
        return True
    return False


def _tree_key(ctx: Ctx, key: str, spine: SpineView) -> bool:
    """Step the tree's cursor between Milestone leaves, or open the one it is on.

    Reading down crosses into the next Track's first Milestone, which the frame expands in
    place, and the walk wraps at either end.
    """
    s = ctx.s
    leaves = leaves_of(groups_of(spine, s.pinned_track))
    if key in _ARROWS:
        if not leaves:
            ctx.log(key, "no Milestone is filed in this scope")
            return True
        down = key == "ArrowDown"
        at = leaves.index(s.sel_id) if s.sel_id in leaves else (-1 if down else len(leaves))
        s.sel_id = leaves[(at + (1 if down else -1)) % len(leaves)]
        ctx.log(key, f"↳ {s.sel_id}")
        return True
    if key in _PAGING and leaves:
        # a page or an end lands on a leaf too, never on a Track heading between them
        at = leaves.index(s.sel_id) if s.sel_id in leaves else 0
        page = max(1, s.visible)
        to = {"Home": 0, "End": len(leaves) - 1, "PageUp": at - page, "PageDown": at + page}
        s.sel_id = leaves[max(0, min(len(leaves) - 1, to[key]))]
        ctx.log(key, f"↳ {s.sel_id}")
        return True
    if key == "Enter":
        target = s.sel_id if s.sel_id in leaves else None
        if target is None:
            ctx.log("Enter", NOTHING_SELECTED)
            return True
        if go(ctx, destination(ctx, target), f"tree · {target}", target):
            s.sel_id = target
        return True
    return False


# ---------- Enter on a list ----------


def _row_drill(ctx: Ctx) -> bool:
    """Open the row under the caret on the route its id names, or say nothing is selected."""
    s = ctx.s
    target = s.sel_id
    if target is None:
        ctx.log("Enter", NOTHING_SELECTED)
        return True
    if target == s.subj_id:
        return _own_subject(ctx, target)
    drill_to(ctx, destination(ctx, target), target)
    # the destination lists its subject among its rows, so its caret opens on that row
    s.sel_id = target
    return True


def destination(ctx: Ctx, key: str) -> str:
    """Return the route that opens record ``key``: its collection's, else its id prefix's.

    The collection the read model states is the authority, because a key such as a Track's
    or an imported Task's carries no prefix the id table knows.
    """
    return _route_of(ctx, key) or ROW_DRILLS[ctx.s.route]


def _held(ctx: Ctx, key: str | None) -> Held | None:
    """Return the row ``key`` names in the frame's read model or the link's held rows."""
    if key is None:
        return None
    return next((row for row in _rows(ctx) if row.key == key), None)


def _rows(ctx: Ctx) -> tuple[Held, ...]:
    """Return the frame's own rows, then every row the link's held projections carry."""
    model = ctx.projection
    return (*(model.rows if model is not None else ()), *ctx.rows)


def _route_of(ctx: Ctx, key: str) -> str | None:
    """Return the route that opens record ``key`` by its held collection, else its id prefix."""
    row = _held(ctx, key)
    routed = COLLECTION_ROUTES.get(row.collection) if row is not None else None
    return routed or route_for_id(key)


def parent(ctx: Ctx) -> tuple[str, str | None] | None:
    """Return the place one step up the containment chain, as the read model states it.

    A route climbing through a record field climbs to the record its subject's row is
    filed under. A surface opened on a record of its parent's kind -- Git on a Run, Trust
    on a Milestone -- climbs back to that record. A parent that opens only onto a record
    and has none to open onto is passed for scope home, which is always a real place.

    Returns:
        The parent route and its subject; ``None`` at scope home, which has no parent.
    """
    s = ctx.s
    escape = REGISTRY.escapes.get(s.route)
    if escape is None:
        return None
    subject = s.subj_id
    row = _held(ctx, subject)
    if escape.via is not None and row is not None and row.parent_key:
        return (escape.route, row.parent_key)
    if subject is not None and _route_of(ctx, subject) == escape.route:
        return (escape.route, subject)
    if REGISTRY.by_id[escape.route].subject_required:
        return (HOME, None)
    return (escape.route, None)


def siblings(ctx: Ctx) -> list[str]:
    """Return the subject and the records filed under the same parent as it, in id order.

    Returns:
        The sibling keys, the subject among them; empty when the subject's row is not held
        or names no parent.
    """
    row = _held(ctx, ctx.s.subj_id)
    if row is None or row.parent_key is None:
        return []
    return sorted(
        {
            r.key
            for r in _rows(ctx)
            if r.collection is row.collection and r.parent_key == row.parent_key
        }
    )


def offered_verb(ctx: Ctx, letter: str | None) -> MenuVerb | None:
    """Return the menu verb bound to ``letter`` on this frame, or its first verb for ``None``.

    A finished subject's menu keeps only its light verbs, so a lifecycle letter finds
    nothing there, as the drawer lists nothing for it.
    """
    states = ctx.decisions.run_states if ctx.decisions is not None else None
    verbs = offered_verbs(ctx.s, ctx.fixture, ctx.projection, states)
    return next((verb for verb in verbs if letter is None or verb.key == letter), None)


def no_cycle(ctx: Ctx, *, back: bool) -> None:
    """Answer a Tab or Shift-Tab on a frame where neither has anything to cycle.

    A native frame answers both alike; the prototype replay keeps its forward Tab
    unclaimed, as its recorded key log has it.
    """
    if back or is_native(ctx):
        ctx.log("S-Tab" if back else "Tab", NOTHING_CYCLES)
    else:
        ctx.noop("Tab")


def say_why(ctx: Ctx, key: str, *, head: list[LogEntry], toasts: int, still: bool) -> bool:
    """Raise a toast for a key a handler claimed on a native frame that changed nothing.

    A motion key at the edge of its list is plainly at the edge, so it raises none, and a
    key whose only note is the quit guard standing down raises none either: the guard's
    own prompt is the one quit toast.

    Args:
        ctx: The keystroke's context.
        key: The key as it was pressed, before its alias is resolved.
        head: The newest key-log entry before the key, as a one-item list or empty.
        toasts: How many toasts the rack held before the key.
        still: Whether the frame drawn after the key is the frame drawn before it.

    Returns:
        Whether a toast was raised, so the frame has to be drawn again.
    """
    s = ctx.s
    log = s.log
    claimed = bool(log) and (not head or log[0] is not head[0])
    if claimed and log[0].note.startswith(DISARMED):
        return False
    motion = ALIASES.get(key, key) in _EVENT_KEYS
    if not (is_native(ctx) and still and claimed and not motion and len(s.toasts) <= toasts):
        return False
    ctx.notify(log[0].note, KEY_WORDS.get(key, key))
    return True


def _own_subject(ctx: Ctx, target: str) -> bool:
    """Act on Enter when the caret rests on the record the frame is about.

    A Milestone's own row opens its acceptance evidence; any other record has nothing
    under it in the rows the frame holds, which the rack says rather than going nowhere.
    """
    s = ctx.s
    if s.route == "milestone":
        open_overlay(s, "acceptance", subject=target)
        s.sel = 0
        ctx.log("Enter", "acceptance evidence at this digest")
        return True
    note = f"{target} is this frame's own record · nothing under it is held here"
    ctx.notify(note, "nothing to drill", Severity.WARN)
    ctx.log("Enter", note)
    return True


def _release(ctx: Ctx) -> bool:
    """Drill the membership row under the caret; the readiness region stays the dispatcher's."""
    if ctx.s.rel_reg == "READINESS":
        return False
    return _row_drill(ctx)


def _history(ctx: Ctx) -> bool:
    """Open what changed on the record under the caret, between two of its revisions."""
    target = ctx.s.sel_id
    if target is None:
        ctx.log("Enter", NOTHING_SELECTED)
        return True
    drill_to(ctx, "history.diff", target)
    return True


def _backlog(ctx: Ctx) -> bool:
    """Open the draft card on the backlog row under the caret."""
    s = ctx.s
    if s.sel_id is None:
        ctx.log("Enter", NOTHING_SELECTED)
        return True
    open_overlay(s, "draft", subject=s.sel_id)
    ctx.log("Enter", f"draft detail · {s.sel_id} · what it still needs")
    return True


def _timeline(ctx: Ctx) -> bool:
    """Drill the marked Milestone, or the undated Milestone or Release under the caret."""
    s = ctx.s
    region = s.tl_reg or LANES
    if region == LANES:
        if s.timeline_marker is None:
            ctx.log("Enter", "no dated milestone on this lane · nothing to open")
        else:
            drill_to(ctx, "milestone", s.timeline_marker)
        return True
    rows = (s.tl_regs or {}).get(region, [])
    pick = rows[s.tl_sel] if region != LANES and 0 <= s.tl_sel < len(rows) else None
    if pick is None:
        ctx.log("Enter", f"nothing to open in {region.lower()}")
        return True
    drill_to(ctx, "milestone" if region == UNDATED else "release", str(pick[0]))
    return True


def _transcript(ctx: Ctx) -> bool:
    """Open the transcript of the Run the frame is about, which its timeline pane names."""
    open_transcript(ctx)
    return True


def _stopped_run(ctx: Ctx) -> bool:
    """Open the stopped Run under the cursor, from the spend read the frame drew its list from."""
    ceiling = held_ceiling(ctx.live)
    key = stopped_key(ctx.s, ceiling) if ceiling is not None else None
    if key is None:
        ctx.log("Enter", "no stopped Run is held · nothing to open")
        return True
    drill_to(ctx, RUN_DETAIL, key)
    return True


def _needs_rows(what: str) -> Callable[[Ctx], bool]:
    """Return an Enter that leaves a held row to the route's own hook and names an empty read."""

    def enter(ctx: Ctx) -> bool:
        model = ctx.projection
        if model is not None and model.rows:
            return False
        ctx.log("Enter", f"no {what} is held · nothing to open")
        return True

    return enter


def _needs_claim(ctx: Ctx) -> bool:
    """Leave Enter to the ladder's own hook while a Claim is held, else name its absence."""
    model = ctx.projection
    if model is not None and any(r.collection is Epoch2Collection.CLAIM for r in model.rows):
        return False
    ctx.log("Enter", "no Claim is held · the ladder has no rung to open")
    return True


_ENTER: Mapping[str, Callable[[Ctx], bool]] = MappingProxyType(
    {
        **dict.fromkeys(
            ("activity", "unattended", "track", "milestone", "batch.detail", "task.detail"),
            _row_drill,
        ),
        "search": _row_drill,
        "timeline": _timeline,
        "run.detail": _transcript,
        "history": _history,
        "release": _release,
        "backlog": _backlog,
        "cost.ceiling": _stopped_run,
        "trust": _needs_rows("truth field"),
        "evidence": _needs_claim,
    }
)


__all__ = [
    "COLLECTION_ROUTES",
    "KEY_WORDS",
    "NOTHING_CYCLES",
    "NOTHING_SELECTED",
    "NOTHING_WAITING",
    "NO_EVENTS",
    "ROW_DRILLS",
    "claim",
    "destination",
    "drill_to",
    "is_native",
    "no_cycle",
    "offered_verb",
    "parent",
    "say_why",
    "siblings",
]
