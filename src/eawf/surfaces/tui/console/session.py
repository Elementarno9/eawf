"""Session: every cursor the console remembers, and the one reset that restores them.

:meth:`Session.reset` is the prototype's session reset field for field, and it is the only
reset. It rebuilds the model from the declared defaults plus the golden ``setup`` and
copies every declared field back, so a cursor that is not a declared field cannot exist
and a declared field cannot survive a reset. The model forbids extra fields, which makes a
misspelled cursor fail on assignment instead of living beside the session. Only the
harness's clock hold and the ``--verbose`` launch flag live outside the session.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.projection.connection import ConnectionValue
from eawf.surfaces.tui.console.tokens import Severity

# The three frame sizes a session renders at, as (columns, rows).
SIZES: tuple[tuple[int, int], ...] = ((80, 24), (120, 30), (160, 40))
BACK_CAP = 32
# The key log keeps this many entries, newest first.
LOG_CAP = 9

# The header label each of the nine wire values prints, in the packet's own words. A
# console keeps this one translation rather than a second vocabulary for the link: the
# wire spells it ``live_complete``, the header spells it ``LIVE``.
_CONNECTION_LABELS: dict[ConnectionValue, str] = {
    ConnectionValue.LIVE_COMPLETE: "LIVE",
    ConnectionValue.LIVE_PARTIAL: "LIVE / PARTIAL",
    ConnectionValue.GAP: "GAP DETECTED",
    ConnectionValue.REPLAYING: "REPLAYING",
    ConnectionValue.SNAPSHOT_REQUIRED: "SNAPSHOT REQUIRED",
    ConnectionValue.SNAPSHOT_LOADING: "SNAPSHOT LOADING",
    ConnectionValue.OFFLINE_SNAPSHOT: "OFFLINE SNAPSHOT",
    ConnectionValue.DISCONNECTED: "DISCONNECTED",
    ConnectionValue.DEGRADED: "DEGRADED",
}


def conn_label(value: ConnectionValue | None) -> str:
    """Return the header's connection label for the seam's own connection value.

    A console with no seam at all has read nothing, which is what ``DISCONNECTED``
    means; a console never claims ``LIVE`` on its own account, because that is a claim
    only a seam that has actually reached the daemon may make.

    Args:
        value: The seam's connection value; ``None`` when the console holds no seam.

    Returns:
        One of the nine header labels.
    """
    if value is None:
        return _CONNECTION_LABELS[ConnectionValue.DISCONNECTED]
    return _CONNECTION_LABELS[value]


class BackEntry(BaseModel):
    """One step the back stack restores."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: str
    sel: int
    subj: str | None


class BackStack(BaseModel):
    """The path Escape walks back, capped at ``BACK_CAP`` steps."""

    model_config = ConfigDict(extra="forbid")

    entries: list[BackEntry] = Field(default_factory=list)

    def push(self, *, route: str, sel: int, subj: str | None) -> None:
        """Record a step, dropping the oldest ones beyond the cap."""
        self.entries.append(BackEntry(route=route, sel=sel, subj=subj))
        del self.entries[:-BACK_CAP]

    def pop(self) -> BackEntry | None:
        """Remove and return the newest step, or ``None`` on an empty stack."""
        return self.entries.pop() if self.entries else None

    def clear(self) -> None:
        """Forget every step."""
        self.entries.clear()

    def items(self) -> list[BackEntry]:
        """Return a copy of the steps, oldest first."""
        return list(self.entries)

    def __len__(self) -> int:
        """Return the number of steps."""
        return len(self.entries)

    def __bool__(self) -> bool:
        """Return whether any step is recorded."""
        return bool(self.entries)


class Toast(BaseModel):
    """One rack notice and the console-clock time it was raised."""

    model_config = ConfigDict(extra="forbid")

    title: str
    text: str
    sev: Severity
    at: float


class LogEntry(BaseModel):
    """One key-log line: the key and the note its handler wrote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    note: str


class SessionSetup(BaseModel):
    """The golden ``setup`` block a frame or journey starts from.

    Every key is optional, and a falsy value falls back to the session default, as the
    prototype's reset reads it. The camel-case aliases are the golden files' own keys.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True)

    route: str | None = None
    sel: int = 0
    subj_id: str | None = Field(default=None, alias="subjId")
    overlay: str | None = None
    size: int = Field(default=0, ge=0, lt=len(SIZES))
    conn: str | None = None
    section: int = 0
    entry_sel: int = Field(default=0, alias="entrySel")
    prefix: str | None = None


def _default_overlay_steps() -> dict[str, int]:
    """Return the step each stepped overlay starts at."""
    return {"question": 0, "pause": 0, "evidence": 0, "readiness": 0}


class Session(BaseModel):
    """Every cursor the console remembers.

    Fields are grouped the way the prototype's reset lists them: the projected fields a
    journey compares, the navigation and per-route cursors, the fields a renderer
    publishes during a render for the dispatcher to read, the settings rail, the timers
    on the console clock, and the diagnostic counters the replay harness reads.
    Assignments are validated, so a cursor holds only its declared type.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # projected fields
    route: str = "scope.home"
    sel: int = 0
    subj_id: str | None = None
    sel_id: str | None = None
    overlay: str | None = None
    size: int = Field(default=0, ge=0, lt=len(SIZES))
    conn: str = "LIVE"
    section: int = 0
    entry_sel: int = 0
    # navigation and per-route cursors
    path_sel: int = 0
    pane_sel: int = 0
    scroll: int = 0
    back: BackStack = Field(default_factory=BackStack)
    prefix: str | None = None
    prefix_seq: int = 0
    prefix_deadline: float | None = None
    filter: str = ""
    filters: dict[str, str] = Field(default_factory=dict)
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
    toasts: list[Toast] = Field(default_factory=list)
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
    tr_t0: float | None = None
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
    ov_state: dict[str, int] = Field(default_factory=_default_overlay_steps)
    log: list[LogEntry] = Field(default_factory=list)
    # published by the renderer during a render, read by the frame builder and dispatcher
    record_facts: list[str] | None = None
    record_nav: list[str | None] | None = None
    absent_frame: bool = False
    absent: bool = False
    reserved: int = 0
    # settings rail
    set_sec: int = 0
    set_key: int = 0
    set_filter: str = ""
    set_typing: bool = False
    lens: str = "repo"
    # timers, all on the console clock
    last_esc: float = 0.0
    rack_last: float = 0.0
    # diagnostic counters
    keys: int = 0
    prefix_cancels: int = 0
    auto_opens: int = 0
    renders: int = 0

    def reset(
        self,
        setup: SessionSetup | None,
        *,
        settings_section_order: Sequence[str] = (),
        now: float = 0.0,
    ) -> None:
        """Restore every declared field from its default and the golden ``setup``.

        Args:
            setup: The frame or journey setup; ``None`` starts from the defaults.
            settings_section_order: The settings catalog's section order; the rail opens
                on ``planning`` when the catalog has it, else on the first section.
            now: The console clock reading the rack starts counting from.
        """
        start = setup if setup is not None else SessionSetup()
        order = list(settings_section_order)
        fresh = Session(
            route=start.route or "scope.home",
            sel=start.sel,
            subj_id=start.subj_id or None,
            overlay=start.overlay or None,
            size=start.size,
            conn=start.conn or "LIVE",
            section=start.section,
            entry_sel=start.entry_sel,
            prefix=start.prefix or None,
            set_sec=order.index("planning") if "planning" in order else 0,
            rack_last=now,
        )
        for name in type(self).model_fields:
            setattr(self, name, getattr(fresh, name))

    @property
    def w(self) -> int:
        """Return the frame width in cells."""
        return SIZES[self.size][0]

    @property
    def h(self) -> int:
        """Return the frame height in rows."""
        return SIZES[self.size][1]

    def log_key(self, key: str, note: str = "") -> None:
        """Log the handler that claimed ``key`` by the note it writes, newest first."""
        self.log.insert(0, LogEntry(key=key, note=note))
        del self.log[LOG_CAP:]
        self.trace = f"{key} → {note or 'claimed, no note'}"

    def noop(self, key: str, *, verbose: bool) -> None:
        """Record an unclaimed key: silent in the log unless the console runs verbose."""
        if verbose:
            self.log_key(key, "unclaimed · no handler on this route")
        else:
            self.trace = f"{key} → unclaimed"

    def disarm_quit(self) -> None:
        """Forget a pending Escape, so the next one does not quit."""
        self.last_esc = 0.0

    def projection(self) -> dict[str, Any]:
        """Return the nine projected keys a journey step compares, under their golden names."""
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
