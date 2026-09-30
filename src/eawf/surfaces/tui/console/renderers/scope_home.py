"""scope.home: the landing route, two regions and a tree.

The outcomes pane is a tree a track expands to its milestones, and the cursor lands on
leaves only. Tab moves to the attention list, and the pane that does not own the arrows
recedes.

The native frame draws the same two regions from the read model: every Track, the one
the cursor is in expanded to the Milestones filed under it, each with its title and
status, and the attention list under a thin rule. A Track's progress is counted off rows
the frame holds -- its completed Milestones -- so it is exact rather than estimated. A
Milestone filed under no Track this scope holds is grouped under a ``no track`` heading
rather than dropped, and a column no producer states wears the unknown token. The name
column keeps the packet's width at 80 columns and widens to a cap beyond it, so the
counts stay beside the names however wide the terminal is.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.kernel.projection.attention import (
    CONSOLE_PRINCIPAL_CLASS,
    AttentionItem,
    build_attention_view,
)
from eawf.kernel.projection.registers import UNWRITTEN_REASON, RegisterView
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import bucket_label, open_actions, top_bucket
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    recede,
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
from eawf.surfaces.tui.console.renderers.attention import NO_DEADLINE
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
from eawf.surfaces.tui.console.width import cell_len, clip, pad

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
    rows: list[str] = [recede(head, w) if attn else head]
    for i, track in enumerate(fx.proto.tracks):
        tn = sum(1 for a in open_ if a.track == track.id)
        on = not attn and i == ti and s.home_ms < 0
        cells = [track.id, str(track.runs), f"!{tn}" if tn else attn_cell(s, 0), track.prog]
        raw = snap_caret(_TRACKS.row(cells, on))
        rows.append(recede(raw, w) if attn else Fixed(pad(raw, w)) if on else raw)
        if i != ti:
            continue
        for k, milestone in enumerate(track.milestones):
            onm = not attn and s.home_ms == k
            leaf = snap_caret(
                _MILESTONES.row([f"{milestone.id} {milestone.name}", milestone.state], onm)
            )
            rows.append(recede(leaf, w) if attn else Fixed(pad(leaf, w)) if onm else leaf)
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
            rows.append(head if attn else recede(head, w))
            last = group
        on = attn and i == s.home_sel
        row = " " + ("▸ " if on else "  ") + pad(action.text, w - 3 - _DUE_W) + " " + action.due
        if cell_len(row) > w:
            row = row[:w]
        rows.append(recede(row, w) if not attn else Fixed(pad(row, w)) if on else row)
    return rows


#: What the group heading unfiled Milestones reads: they are drawn, never dropped.
NO_TRACK = "∅ no track"

#: The attention rows the home frame lists before it counts the rest.
_ATTENTION_ROWS = 4

# The cells the RUNS and ATTENTION columns take beside a tree row's name, and the MINE
# column the attention count splits off under more than one principal.
_RUNS_W, _ATTN_W, _MINE_W, _ALL_W = 7, 12, 7, 16
# The name column: the packet's width below 120 columns, and the cap it widens to above.
_NAME_W, _NAME_CAP = 30, 48


@dataclass(frozen=True, slots=True)
class TreeRow:
    """One row of the native outcome tree.

    Attributes:
        row: The record drawn, or ``None`` for the group heading unfiled Milestones.
        depth: ``0`` for a Track or the group heading, ``1`` for a Milestone under it.
        group: The offset of the group the row belongs to: a Track, or the unfiled group.
    """

    row: SpineRow | None
    depth: int
    group: int = 0


#: One group of the tree: its Track, ``None`` for the unfiled group, and its Milestones.
Group = tuple[SpineRow | None, list[SpineRow]]


def _of(spine: SpineView, collection: Epoch2Collection) -> list[SpineRow]:
    return [row for row in spine.rows if row.collection is collection]


def groups_of(spine: SpineView) -> list[Group]:
    """Return each Track with its Milestones, then the Milestones filed under no held Track."""
    tracks = _of(spine, Epoch2Collection.TRACK)
    milestones = _of(spine, Epoch2Collection.MILESTONE)
    held_keys = {track.key for track in tracks}
    groups: list[Group] = [(t, [m for m in milestones if m.parent_key == t.key]) for t in tracks]
    unfiled = [m for m in milestones if m.parent_key not in held_keys]
    return [*groups, (None, unfiled)] if unfiled else groups


def tree_of(groups: list[Group], focus: int) -> list[TreeRow]:
    """Return the outcome tree: every group's heading, the focused group expanded under it."""
    tree: list[TreeRow] = []
    for index, (head, members) in enumerate(groups):
        tree.append(TreeRow(head, 0, index))
        if index == focus:
            tree.extend(TreeRow(m, 1, index) for m in members)
    return tree


def _focus(groups: list[Group], selected: str | None, last: int) -> int:
    """Return the group the selected record sits in, else the group focused last."""
    for index, (head, members) in enumerate(groups):
        keys = {m.key for m in members} | ({head.key} if head is not None else set())
        if selected in keys:
            return index
    return min(max(last, 0), max(len(groups) - 1, 0))


def leaves_of(groups: list[Group]) -> list[str]:
    """Return every Milestone the tree files, in reading order: the rows the cursor lands on.

    A Track is a container rather than a destination, and the group heading of unfiled
    Milestones names no record, so neither is ever selected.
    """
    return [member.key for _head, members in groups for member in members]


def _filled(groups: list[Group], start: int) -> int:
    """Return the first group from ``start`` on, wrapping, that files a Milestone."""
    for step in range(len(groups)):
        at = (start + step) % len(groups)
        if groups[at][1]:
            return at
    return start


def _done(rows: list[SpineRow], status: str) -> int:
    return sum(1 for row in rows if row.field("status").value == status)


def progress(spine: SpineView, row: SpineRow) -> str:
    """Return a Track's progress, counted off the Milestones filed under it."""
    under = [m for m in _of(spine, Epoch2Collection.MILESTONE) if m.parent_key == row.key]
    if not under:
        return "no milestone filed"
    done = _done(under, str(MilestoneStatus.COMPLETED))
    return f"{done} of {dv.plural(len(under), 'milestone')} done"


def _word(field: TruthField[str]) -> str:
    """Return a count cell: its value, or its truth token beside the state word."""
    cell = value_cell(field, exempt=True)
    return f"{cell.slot} {cell.word}" if cell.word else cell.slot


def _name(row: SpineRow) -> str:
    return f"{row.key} {row.title}" if row.title else row.key


def _runs(row: SpineRow) -> str:
    """Return how many Runs are filed under a tree row, or the unknown token when unread."""
    runs = row.facts.get("runs")
    return runs if runs is not None else _word(row.field("runs"))


@dataclass(frozen=True, slots=True)
class HomeAttention:
    """What the home frame reads off the attention reducer, for one principal.

    Attributes:
        register: The Attention register, when one is held and written.
        principal: The principal key the console acts as, or ``None``.
    """

    register: RegisterView | None
    principal: str | None

    def items(self) -> tuple[AttentionItem, ...]:
        """Return every open item that needs somebody, whoever it is addressed to."""
        return build_attention_view(self.register).blocking() if self.register else ()

    def mine(self) -> tuple[AttentionItem, ...]:
        """Return the open items this principal is in the audience of."""
        if self.register is None or self.principal is None:
            return ()
        return build_attention_view(self.register).open_for(self.principal)

    def cell(self, key: str, *, own: bool) -> str:
        """Return a tree row's attention count: open items filed under ``key``.

        Args:
            key: The Track or Milestone key the count is taken under.
            own: Whether only this principal's items are counted.
        """
        if self.register is None:
            return UNKNOWN_WORD
        facts = {row.key: row.facts for row in self.register.rows}
        items = self.mine() if own else self.items()
        n = sum(
            1 for i in items if key in (facts[i.key].get("track"), facts[i.key].get("milestone"))
        )
        return f"!{n}" if n else "0"

    def holders(self) -> set[str]:
        """Return every principal the register addresses an item to, and this one."""
        rows = self.register.rows if self.register is not None else ()
        return {row.assignee_ref for row in rows if row.assignee_ref} | (
            {self.principal} if self.principal else set()
        )


def _home_attention(view: View) -> HomeAttention:
    register = view.attention
    written = register if register is not None and not register.withheld else None
    return HomeAttention(written, view.principal)


def _tree_lines(
    view: View,
    spine: SpineView,
    tree: list[TreeRow],
    unfiled: list[SpineRow],
    cursor: int | None,
    chrome: int,
) -> list[str]:
    """Return the tree's head, the rows around the cursor, and its window line.

    Under more than one principal the attention column splits in two -- this principal's
    own count and every principal's -- and nothing else in the table moves.
    """
    w = view.w
    attention = _home_attention(view)
    shared = len(attention.holders()) > 1
    first = _NAME_CAP if view.wide else _NAME_W
    widths = [first, _RUNS_W, *([_MINE_W, _ALL_W] if shared else [_ATTN_W]), 0]
    tracks = Table(widths, 2)
    # one grid for the whole tree: a Milestone's name sits two cells further in and ends
    # where a Track's does, so its state sits under RUNS
    leaves = Table([first - 2, 0], 4)
    heads = ["RUNS", *(["MINE", "ALL PRINCIPALS"] if shared else ["ATTENTION"])]
    lines = [tracks.head(["MILESTONES", *heads, "PROGRESS"])]
    win = window_rows(view, total=len(tree), cursor=cursor or 0, chrome=chrome + 2)
    for index in range(win.start, win.stop):
        item, on = tree[index], index == cursor
        if item.row is None:
            filed = dv.plural(len(unfiled), "milestone")
            line = tracks.row([NO_TRACK, "", *([""] if shared else []), "", f"{filed} filed"], on)
        elif item.depth == 0:
            row = item.row
            counts_ = [attention.cell(row.key, own=True)] if shared else []
            counts_.append(attention.cell(row.key, own=False))
            name = clip(_name(row), first - 2)
            line = tracks.row([name, _runs(row), *counts_, progress(spine, row)], on)
        else:
            row = item.row
            name = clip(_name(item.row), first - 4)
            line = leaves.row([name, value_cell(row.field("status")).slot], on)
        lines.append(line if on else Fixed(pad(line, w)))
    if not tree:
        lines.append("   this scope holds no record: no Track and no Milestone")
    if win.hides:
        lines.append(win.line(complete=spine.complete))
    return lines


def principal_line(view: View) -> list[str]:
    """Return the row naming the operator and their class, drawn only under several principals.

    A named principal acts on a console in the operator class, the class the daemon checks
    every console write under; a console acting as nobody has no class, so it wears the
    unknown token there.
    """
    attention = _home_attention(view)
    if len(attention.holders()) < 2:
        return []
    who = view.principal or "nobody"
    klass = f"{CONSOLE_PRINCIPAL_CLASS} class" if view.principal else f"class {UNKNOWN_WORD}"
    n = len(attention.mine())
    yours = f"{dv.plural(n, 'action')} {'is' if n == 1 else 'are'} yours"
    return [f" PRINCIPAL  you are {who} · {klass} · {yours}"]


def attention_lines(
    view: View, register: RegisterView | None, focus: int | None = None
) -> list[str]:
    """Return the home frame's attention region, read off the attention reducer.

    The list is this principal's: each open item in its audience, under the bucket that
    needs the operator, with what it asks and when it is due. An item addressed only to
    another principal is counted on a line of its own rather than listed. A register not
    read yet states no count: the region says so, rather than printing ``nothing is
    waiting``, which would be a claim. While the list holds the focus, ``focus`` is the
    item its caret is on.
    """
    if register is None:
        return [f" ATTENTION  {UNKNOWN_WORD} · the attention register has not been read"]
    if register.withheld:
        return [f" ATTENTION  {UNKNOWN_WORD} · {UNWRITTEN_REASON}"]
    attention = HomeAttention(register, view.principal)
    mine, every = attention.mine(), attention.items()
    others = len(every) - len(mine)
    elsewhere = f"   {dv.plural(others, 'action')} open to other principals" if others else ""
    if not mine:
        return [
            " ATTENTION   nothing here opened itself",
            "   nothing is waiting on you · runs continue without you",
            *([elsewhere] if elsewhere else []),
        ]
    w = view.w
    facts = {row.key: row.facts for row in register.rows}
    lines = [" ATTENTION", f" NEEDS OPERATOR  {group(len(mine))}"]
    first = 0 if focus is None else max(0, focus - _ATTENTION_ROWS + 1)
    for at, item in enumerate(mine[first : first + _ATTENTION_ROWS], start=first):
        fact = facts.get(item.key, {})
        text = f"{fact.get('subject', item.key)} {fact.get('question', '')}".rstrip()
        lead = " ▸ " if at == focus else "   "
        line = lead + pad(text, w - 3 - _DUE_W - 1) + " " + NO_DEADLINE
        lines.append(line if at == focus else Fixed(pad(line, w)))
    rest = len(mine) - first - _ATTENTION_ROWS
    if rest > 0:
        lines.append(f"   … {rest} more on the Attention route")
    if elsewhere:
        lines.append(elsewhere)
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
    groups = groups_of(spine)
    focus = _focus(groups, s.sel_id, s.home_track)
    tree = tree_of(groups, focus)
    keys = [item.row.key if item.row is not None else None for item in tree]
    index = dv.restore_by_id(s, keys)
    if tree and tree[index].group != focus:
        # the cursor stepped onto another group's heading, which expands in its place
        landed = tree[index]
        focus = landed.group
        tree = tree_of(groups, focus)
        keys = [item.row.key if item.row is not None else None for item in tree]
        index = tree.index(landed)
    landing: int | None = index if tree else None
    if tree and tree[index].depth == 0:
        # a Track is a container, never a destination: the cursor lands on a Milestone of
        # the group it stepped onto, or of the next group that files one
        focus = _filled(groups, tree[index].group)
        tree = tree_of(groups, focus)
        keys = [item.row.key if item.row is not None else None for item in tree]
        landing = next((i for i, item in enumerate(tree) if item.depth == 1), None)
    s.home_track = focus
    s.sel = landing or 0
    s.sel_id = keys[landing] if landing is not None else None
    unfiled = groups[-1][1] if groups and groups[-1][0] is None else []
    mine = _home_attention(view).mine()
    # the region the unfocused pane recedes by, which Tab moves when the list holds items
    on_list = s.home_region == ATTENTION_REGION and bool(mine)
    if on_list:
        s.home_sel = max(0, min(len(mine) - 1, s.home_sel))
    top = [
        *native_head(view, spine, crumb_text=route_crumb(view, spine), summary=counts(spine)),
        *principal_line(view),
    ]
    listed = attention_lines(view, view.attention, s.home_sel if on_list else None)
    # the pane that does not own the arrows recedes, so where the focus is reads at a glance
    below = [thin(w), *(listed if on_list else [recede(line, w) for line in listed])]
    caret = None if on_list else landing
    lines = _tree_lines(view, spine, tree, unfiled, caret, len(top) + len(below))
    if on_list:
        lines = [recede(line, w) for line in lines]
    rows = [*top, *lines, *below]
    # the arrows walk the Milestones or the list the focus is on, never a Track row
    s.nav_rows = len(mine) if on_list else len(leaves_of(groups))
    # a page or an end jumps between the tree's leaves, so it has somewhere to go whenever
    # there is more than one leaf, whether or not the tree was cut to fit
    s.windowed = s.windowed or (not on_list and s.nav_rows > 1)
    # Tab only moves the focus when the attention list holds something to focus
    entries = native_keys(s.route, windowed=s.windowed)
    return build(
        view, rows, route_keys_bar(view, [e for e in entries if mine or e.keys != ("Tab",)])
    )


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

    A native frame's keys are the drill module's, which reads the same tree this frame
    draws, so this hook serves only the prototype registers.
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
