"""scope.home: the landing route, two regions and a tree.

The outcomes pane is a tree a track expands to its milestones, and the cursor lands on
leaves only. Tab moves to the attention list, and the pane that does not own the arrows
recedes.

The native frame draws the same two regions from the read model: every Track with the
Milestones filed under it nested beneath, each with its title, status and progress, and
the attention list under a thin rule. Progress is counted off rows the frame holds -- a
Track's completed Milestones, a Milestone's completed Batches -- so it is exact rather than
estimated. A Milestone filed under no Track this scope holds is drawn under a ``no track``
group rather than dropped, and a column no producer states wears the unknown token.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.kernel.projection.registers import UNWRITTEN_REASON, RegisterView
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import bucket_label, open_actions, top_bucket
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    snap_caret,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.reads import attn_cell, prototype_attached, reads
from eawf.surfaces.tui.console.registry import route_of
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
from eawf.surfaces.tui.console.width import cell_len, pad

ATTENTION_REGION = "attention"
OUTCOMES_REGION = "outcomes"
_TRACKS = Table([30, 7, 12, 0], 2)
_MILESTONES = Table([34, 0], 4)
# Cells a due cell takes at the end of an attention row, with its space.
_DUE_W = 7


def _tree(view: View, open_: list[Action], attn: bool) -> list[str]:
    """Return the track table with the focused track's milestones expanded under it."""
    s, fx, w = view.session, view.fixture, view.w
    ti = dv.home_track(s, fx)
    head = _TRACKS.head(["MILESTONES", "RUNS", "ATTENTION", "PROGRESS"])
    rows: list[str] = [Fixed(pad(head, w)) if attn else head]
    for i, track in enumerate(fx.proto.tracks):
        tn = sum(1 for a in open_ if a.track == track.id)
        on = not attn and i == ti and s.home_ms < 0
        cells = [track.id, str(track.runs), f"!{tn}" if tn else attn_cell(s, 0), track.prog]
        raw = snap_caret(_TRACKS.row(cells, on))
        rows.append(Fixed(pad(raw, w)) if attn or on else raw)
        if i != ti:
            continue
        for k, milestone in enumerate(track.milestones):
            onm = not attn and s.home_ms == k
            leaf = snap_caret(
                _MILESTONES.row([f"{milestone.id} {milestone.name}", milestone.state], onm)
            )
            rows.append(Fixed(pad(leaf, w)) if attn or onm else leaf)
    return rows


def _attention(view: View, open_: list[Action], attn: bool) -> list[str]:
    """Return the attention list, grouped by bucket; it recedes unless it holds the focus."""
    s, fx, w = view.session, view.fixture, view.w
    rows = [" ATTENTION" + ("" if open_ else "   nothing here opened itself")]
    if not open_:
        rows.append("   nothing is waiting on you · runs continue without you")
    last: str | None = None
    for i, action in enumerate(open_):
        group = top_bucket(action.bucket)
        if group != last:
            n = sum(1 for x in open_ if top_bucket(x.bucket) == group)
            head = f" {bucket_label(fx, group).upper()}  {n}"
            rows.append(head if attn else Fixed(pad(head, w)))
            last = group
        on = attn and i == s.home_sel
        row = " " + ("▸ " if on else "  ") + pad(action.text, w - 3 - _DUE_W) + " " + action.due
        if cell_len(row) > w:
            row = row[:w]
        rows.append(Fixed(pad(row, w)) if on or not attn else row)
    return rows


#: What the group heading unfiled Milestones reads: they are drawn, never dropped.
NO_TRACK = "∅ no track · filed under none held"

#: The attention rows the home frame lists before it counts the rest.
_ATTENTION_ROWS = 4

# The cells the RUNS, ATTENTION and PROGRESS columns take beside a tree row's name.
_RUNS_W, _ATTN_W, _PROGRESS_W = 11, 12, 22


@dataclass(frozen=True, slots=True)
class TreeRow:
    """One row of the native outcome tree.

    Attributes:
        row: The record drawn, or ``None`` for the group heading unfiled Milestones.
        depth: ``0`` for a Track or the group heading, ``1`` for a Milestone under it.
    """

    row: SpineRow | None
    depth: int


def _of(spine: SpineView, collection: Epoch2Collection) -> list[SpineRow]:
    return [row for row in spine.rows if row.collection is collection]


def tree_of(spine: SpineView) -> list[TreeRow]:
    """Return the outcome tree: each Track, its Milestones under it, then the unfiled ones."""
    tracks = _of(spine, Epoch2Collection.TRACK)
    milestones = _of(spine, Epoch2Collection.MILESTONE)
    held_keys = {track.key for track in tracks}
    tree: list[TreeRow] = []
    for track in tracks:
        tree.append(TreeRow(track, 0))
        tree.extend(TreeRow(m, 1) for m in milestones if m.parent_key == track.key)
    unfiled = [m for m in milestones if m.parent_key not in held_keys]
    if unfiled:
        tree.append(TreeRow(None, 0))
        tree.extend(TreeRow(m, 1) for m in unfiled)
    return tree


def _done(rows: list[SpineRow], status: str) -> int:
    return sum(1 for row in rows if row.field("status").value == status)


def progress(spine: SpineView, row: SpineRow) -> str:
    """Return a tree row's progress, counted off the rows filed under it."""
    if row.collection is Epoch2Collection.TRACK:
        under = [m for m in _of(spine, Epoch2Collection.MILESTONE) if m.parent_key == row.key]
        done, word = _done(under, str(MilestoneStatus.COMPLETED)), "milestone"
    else:
        under = [b for b in _of(spine, Epoch2Collection.BATCH) if b.parent_key == row.key]
        done, word = _done(under, str(BatchStatus.COMPLETED)), "batch"
    if not under:
        return f"no {word} filed" if word == "milestone" else "no batch cut"
    total = dv.plural(len(under), word, "es" if word == "batch" else "s")
    return f"{done} of {total} done"


def _word(field: TruthField[str]) -> str:
    """Return a count cell: its value, or its truth token beside the state word."""
    cell = value_cell(field, exempt=True)
    return f"{cell.slot} {cell.word}" if cell.word else cell.slot


def _name(row: SpineRow) -> str:
    return f"{row.key} {row.title}" if row.title else row.key


def _tree_lines(
    view: View, spine: SpineView, tree: list[TreeRow], cursor: int, chrome: int
) -> list[str]:
    """Return the tree's head, the rows around the cursor, and its window line."""
    w = view.w
    first = max(24, w - 3 - _RUNS_W - _ATTN_W - _PROGRESS_W)
    tracks = Table([first, _RUNS_W, _ATTN_W, 0], 2)
    leaves = Table([first - 2, _RUNS_W + _ATTN_W, 0], 4)
    lines = [tracks.head(["MILESTONES", "RUNS", "ATTENTION", "PROGRESS"])]
    win = window_rows(view, total=len(tree), cursor=cursor, chrome=chrome + 2)
    for index in range(win.start, win.stop):
        item, on = tree[index], index == cursor
        if item.row is None:
            line = tracks.row([NO_TRACK], on)
        elif item.depth == 0:
            row = item.row
            # a count no producer states is the unknown token with its word, never a blank
            runs, attn = (_word(row.field(name)) for name in ("runs", "attention"))
            line = tracks.row([_name(row), runs, attn, progress(spine, row)], on)
        else:
            row = item.row
            status = value_cell(row.field("status")).slot
            line = leaves.row([_name(row), status, progress(spine, row)], on)
        lines.append(line if on else Fixed(pad(line, w)))
    if not tree:
        lines.append("   this scope holds no record: no Track and no Milestone")
    lines.append(win.line(complete=spine.complete))
    return lines


def attention_lines(register: RegisterView | None) -> list[str]:
    """Return the home frame's attention region, read off the Attention register.

    A register nobody writes, or one not read yet, states no count: the region says which,
    rather than printing ``nothing is waiting``, which would be a claim.
    """
    if register is None:
        return [f" ATTENTION  {UNKNOWN_WORD} · the attention register has not been read"]
    if register.withheld:
        return [f" ATTENTION  {UNKNOWN_WORD} · {UNWRITTEN_REASON}"]
    if not register.rows:
        return [
            " ATTENTION   nothing here opened itself",
            "   nothing is waiting on you · runs continue without you",
        ]
    lines = [f" ATTENTION  {dv.plural(len(register.rows), 'action')} open"]
    lines.extend(
        f"   {row.key}  {value_cell(row.status).slot}" for row in register.rows[:_ATTENTION_ROWS]
    )
    rest = len(register.rows) - _ATTENTION_ROWS
    if rest > 0:
        lines.append(f"   … {rest} more on the Attention route")
    return lines


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return the scope-home frame drawn from the read model the daemon served.

    Args:
        view: The render being built; its Attention register fills the attention region.
        spine: The home read model: Tracks, Milestones and Batches at one cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    tree = tree_of(spine)
    keys = [item.row.key if item.row is not None else None for item in tree]
    found = keys.index(s.sel_id) if s.sel_id is not None and s.sel_id in keys else None
    index = found if found is not None else min(max(s.sel, 0), max(len(tree) - 1, 0))
    if tree and keys[index] is None:
        # the group heading holds no record, so the cursor steps onto its first Milestone
        index = min(index + 1, len(tree) - 1)
    s.sel = index
    s.sel_id = keys[index] if tree else None
    top = native_head(view, spine, crumb_text=route_crumb(spine), summary=counts(spine))
    below = [thin(w), *attention_lines(view.attention)]
    rows = [*top, *_tree_lines(view, spine, tree, index, len(top) + len(below)), *below]
    return build(view, rows, route_keys_bar(view, native_keys(s.route)))


def render(view: View) -> list[str]:
    """Return the scope-home frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    proto = fx.proto
    open_ = open_actions(fx)
    attn = s.home_region == ATTENTION_REGION and bool(open_)
    if attn:
        s.home_sel = max(0, min(len(open_) - 1, s.home_sel))
    s.sel = dv.home_track(s, fx)
    rd = reads(s)
    rows = [
        header(view, f" Eä ▸ {proto.scope}"),
        f" {len(proto.tracks)} tracks · {len(proto.fleet)} runs"
        + ("" if rd.complete else f" · {rd.label}"),
        bar(w),
    ]
    if not rd.complete:
        rows.extend([f" ATTACHED  {prototype_attached(rd, fx)}", thin(w)])
    rows.extend(_tree(view, open_, attn))
    rows.append(thin(w))
    rows.extend(_attention(view, open_, attn))
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["scope.home"]))


def _attention_key(ctx: Ctx, key: str, open_: list[Action]) -> bool:
    """Handle a key while the attention list holds the focus."""
    s = ctx.s
    if key in ("ArrowDown", "ArrowUp"):
        step = 1 if key == "ArrowDown" else len(open_) - 1
        s.home_sel = (s.home_sel + step) % len(open_)
        ctx.log(key, open_[s.home_sel].text[:44])
        return True
    if key == "Enter":
        action = open_[s.home_sel] if s.home_sel < len(open_) else None
        entity = dv.entity_id_in(action.text if action else "")
        to = (entity and route_of(entity)) or "attention"
        s.sel_id = entity or None
        go(ctx, to, f"what wants you · {entity or 'row'}", entity)
        return True
    if key == "Escape":
        s.home_region = OUTCOMES_REGION
        ctx.log("Esc", "focus → outcomes")
        return True
    return False


def _tree_key(ctx: Ctx, key: str) -> bool:
    """Handle a key while the outcome tree holds the focus."""
    s = ctx.s
    tracks = ctx.fixture.proto.tracks
    if key in ("ArrowDown", "ArrowUp"):
        dv.home_step(s, ctx.fixture, 1 if key == "ArrowDown" else -1)
        track = tracks[s.home_track]
        at = s.home_ms
        leaf = track.milestones[at] if at >= 0 else None
        ctx.log(key, track.id if leaf is None else f"  ↳ {leaf.id} {leaf.name}")
        return True
    if key == "Enter" and s.home_ms >= 0:
        milestone = tracks[s.home_track].milestones[s.home_ms].id
        go(ctx, route_of(milestone) or "milestone", f"tree · {milestone}", milestone)
        return True
    return False


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Swap regions on Tab, walk the focused region on the arrows, open on Enter.

    A native frame draws the spine's rows rather than the tree, so its keys fall through
    to the dispatcher, which walks and pages those rows.
    """
    s = ctx.s
    if s.route != "scope.home" or busy(s) or isinstance(ctx.projection, SpineView):
        return False
    open_ = open_actions(ctx.fixture)
    if key == "Tab":
        if not open_:
            ctx.log("Tab", "nothing is waiting — no list to focus")
            return True
        on_list = s.home_region != ATTENTION_REGION
        s.home_region = ATTENTION_REGION if on_list else OUTCOMES_REGION
        ctx.log(
            "Tab",
            "focus → what wants you · ↑↓ picks · Enter opens it" if on_list else "focus → outcomes",
        )
        return True
    if s.home_region == ATTENTION_REGION and open_:
        return _attention_key(ctx, key, open_)
    if s.home_region != ATTENTION_REGION:
        return _tree_key(ctx, key)
    return False
