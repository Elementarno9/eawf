"""Session: every cursor the console keeps, one canonical reset, the back stack, the clock.

``Session.reset(setup)`` is the prototype's ``resetSession`` field for field. A cursor that
is not a declared field here cannot exist, which is the reset audit ``regen.js`` runs by
grepping, made structural. Two fields live outside the reset on purpose: ``verbose`` (a
launch flag) and ``rack_hold`` (the harness's clock hold); ``simulator`` is the spike's own
flag that reproduces the pack's simulator rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

SIZES: tuple[tuple[int, int], ...] = ((80, 24), (120, 30), (160, 40))
BACK_CAP = 32
PREFIX_TIMEOUT = 1.5
QUIT_FLOOR = 0.080
QUIT_CEILING = 1.5


class Clock:
    """Monotonic console clock in seconds; every timed behaviour reads this one."""

    def now(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    """A held clock: it moves only when a test advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@dataclass(frozen=True, slots=True)
class BackEntry:
    route: str
    sel: int
    subj: str | None


class BackStack:
    """The path Esc walks back: capped, restoring route, row and subject."""

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[BackEntry] = []

    def push(self, route: str, sel: int, subj: str | None) -> None:
        self._items.append(BackEntry(route, sel, subj))
        if len(self._items) > BACK_CAP:
            del self._items[0 : len(self._items) - BACK_CAP]

    def pop(self) -> BackEntry | None:
        return self._items.pop() if self._items else None

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __getitem__(self, i: int) -> BackEntry:
        return self._items[i]

    def top(self) -> BackEntry | None:
        return self._items[-1] if self._items else None

    def items(self) -> list[BackEntry]:
        return list(self._items)


@dataclass(slots=True)
class Toast:
    title: str
    text: str
    sev: str
    at: float


@dataclass(slots=True)
class LogEntry:
    key: str
    note: str


@dataclass(slots=True)
class Session:
    # projection fields
    route: str = "scope.home"
    sel: int = 0
    subj_id: str | None = None
    sel_id: str | None = None
    overlay: str | None = None
    size: int = 0
    conn: str = "LIVE"
    section: int = 0
    entry_sel: int = 0
    # cursors
    path_sel: int = 0
    pane_sel: int = 0
    scroll: int = 0
    back: BackStack = field(default_factory=BackStack)
    prefix: str | None = None
    prefix_seq: int = 0
    prefix_deadline: float | None = None
    filter: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    bucket: str | None = None
    pq: str = ""
    pscroll: int = 0
    typing: bool = False
    edit: dict[str, Any] | None = None
    evt: str | None = None
    reply: dict[str, Any] | None = None
    c_target: dict[str, Any] | None = None
    verb: str = "a"
    home_track: int = 0
    home_ms: int = 0
    home_region: str | None = None
    home_sel: int = 0
    track_group: str | None = None
    toasts: list[Toast] = field(default_factory=list)
    trace: str | None = None
    follow: bool = True
    bound: Any = None
    diff_pair: str | None = None
    mark: int = 0
    timeline_marker: str | None = None
    timeline_marks: int = 0
    marker_card: dict[str, Any] | None = None
    rung: int = 0
    bl_draft: int = 0
    cam_sec: str = "PLAN"
    artifact: int = 0
    art_scroll: int = 0
    cam_step: int = 0
    step_reg: str = "HISTORY"
    hist_sel: int = 0
    tr_sel: int | None = None
    tr_fold: dict[int, bool] | None = None
    tr_t0: Any = None
    rec_at: Any = None
    rec_seen: dict[str, int] | None = None
    draft_field: int = 0
    draft_miss: list[str] | None = None
    tr_pal: str | None = None
    bl_group: str = "DRAFTS"
    tl_reg: str = "LANES"
    tl_sel: int = 0
    tl_regs: dict[str, Any] | None = None
    rel_reg: str = "MEMBERSHIP"
    rel_sel: int = 0
    art_max: int = 0
    promote: dict[str, Any] | None = None
    visible: int = 0
    count: int = 0
    # published by the renderer during render(), read by build() and the dispatcher
    record_facts: list[str] | None = None
    record_nav: list[str | None] | None = None
    absent_frame: bool = False
    absent: bool = False
    reserved: int = 0
    ov_state: dict[str, int] = field(
        default_factory=lambda: {"question": 0, "pause": 0, "evidence": 0, "readiness": 0}
    )
    # settings rail
    set_sec: int = 0
    set_key: int = 0
    set_filter: str = ""
    set_typing: bool = False
    lens: str = "repo"
    # timers, all on the console clock
    last_esc: float = 0.0
    rack_last: float = 0.0
    # evidence counters
    keys: int = 0
    prefix_cancels: int = 0
    auto_opens: int = 0
    renders: int = 0
    log: list[LogEntry] = field(default_factory=list)
    # outside the reset by design
    verbose: bool = False
    rack_hold: bool = False
    simulator: bool = True

    def reset(
        self, setup: dict[str, Any] | None, settings_section_order: tuple[str, ...] = ()
    ) -> None:
        o = setup or {}
        self.log.clear()
        self.route = o.get("route") or "scope.home"
        self.sel = o.get("sel") or 0
        self.subj_id = o.get("subjId") or None
        self.sel_id = None
        self.overlay = o.get("overlay") or None
        self.size = o.get("size") or 0
        self.conn = o.get("conn") or "LIVE"
        self.section = o.get("section") or 0
        self.entry_sel = o.get("entrySel") or 0
        # the packet's pane/go states were recorded without arming the prefix; the setup
        # field the regen fix will add lands here
        self.prefix = o.get("prefix") or None
        self.path_sel = 0
        self.pane_sel = 0
        self.scroll = 0
        self.back = BackStack()
        self.filter = ""
        self.filters = {}
        self.bucket = None
        self.pq = ""
        self.pscroll = 0
        self.typing = False
        self.edit = None
        self.evt = None
        self.reply = None
        self.c_target = None
        self.verb = "a"
        self.home_track = 0
        self.home_ms = 0
        self.home_region = None
        self.home_sel = 0
        self.track_group = None
        self.toasts = []
        self.trace = None
        self.follow = True
        self.bound = None
        self.diff_pair = None
        self.mark = 0
        self.timeline_marker = None
        self.timeline_marks = 0
        self.marker_card = None
        self.rung = 0
        self.bl_draft = 0
        self.cam_sec = "PLAN"
        self.artifact = 0
        self.art_scroll = 0
        self.prefix_seq = 0
        self.prefix_deadline = None
        self.cam_step = 0
        self.step_reg = "HISTORY"
        self.hist_sel = 0
        self.tr_sel = None
        self.tr_fold = None
        self.tr_t0 = None
        self.rec_at = None
        self.rec_seen = None
        self.draft_field = 0
        self.draft_miss = None
        self.tr_pal = None
        self.bl_group = "DRAFTS"
        self.tl_reg = "LANES"
        self.tl_sel = 0
        self.tl_regs = None
        self.rel_reg = "MEMBERSHIP"
        self.rel_sel = 0
        self.art_max = 0
        self.promote = None
        self.visible = 0
        self.count = 0
        self.record_facts = None
        self.record_nav = None
        self.absent_frame = False
        self.absent = False
        self.reserved = 0
        self.last_esc = 0.0
        self.ov_state = {"question": 0, "pause": 0, "evidence": 0, "readiness": 0}
        self.set_sec = (
            settings_section_order.index("planning") if "planning" in settings_section_order else 0
        )
        self.set_key = 0
        self.set_filter = ""
        self.set_typing = False
        self.lens = "repo"

    @property
    def w(self) -> int:
        return SIZES[self.size][0]

    @property
    def h(self) -> int:
        return SIZES[self.size][1]

    def log_key(self, key: str, note: str = "") -> None:
        """The diagnostics log names the handler that claimed a key by the note it writes."""
        self.log.insert(0, LogEntry(key, note))
        del self.log[9:]
        self.trace = f"{key} → {note or 'claimed, no note'}"

    def noop(self, key: str) -> None:
        """An unclaimed key does nothing and says nothing; only --verbose names it."""
        if self.verbose:
            self.log_key(key, "unclaimed · no handler on this route")
        else:
            self.trace = f"{key} → unclaimed"

    def disarm_quit(self) -> None:
        self.last_esc = 0.0

    def projection(self) -> dict[str, Any]:
        """The nine projected keys the journey contract compares."""
        return {
            "route": self.route,
            "overlay": self.overlay,
            "sel": self.sel,
            "subjId": self.subj_id,
            "conn": self.conn,
            "bucket": self.bucket or None,
            "back_depth": len(self.back),
            "toasts": len(self.toasts),
            "mark": self.mark or 0,
        }
