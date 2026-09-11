"""ConsoleApp: one screen, three row widgets, one key dispatcher, one clock.

``render()`` is the prototype's ``render()``: the armed prefix or an open drawer keeps the
route frame and replaces its tail; a full-frame overlay replaces the frame; otherwise the
route renderer's frame is shown. The App never caches W/H: it reads ``self.size`` at
render time, so ``pilot.resize_terminal`` re-lays the frame on the next render.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.events import Key, Resize

from ..chassis import rack
from ..chassis.drawers import render_drawer
from ..chassis.fixture import Fixture, load_fixture
from ..chassis.frame import keybar, thin
from ..chassis.keys import DRAWER_PAIRS, Ctx, dispatch, expire_prefix
from ..chassis.overlays import render_overlay
from ..chassis.renderers import render_route
from ..chassis.session import SIZES, Clock, FakeClock, Session
from ..chassis.widgets import Body, Keybar, ProjectionHeader
from ..chassis.width import cell_len, pad
from ..harness.keymap import to_js_key


class ConsoleApp(App[None]):
    """The console. A held ``FakeClock`` registers no timers; a live clock sweeps the rack
    every 250 ms through the App's own tick, the only timer the chassis owns."""

    CSS = """
    Screen { background: $background; }
    """

    def __init__(
        self, fixture: Fixture | None = None, clock: Clock | None = None, *, simulator: bool = True
    ) -> None:
        super().__init__()
        self.fixture = fixture or load_fixture()
        self.clock: Clock = clock or Clock()
        self.session = Session()
        self.session.simulator = simulator
        self.session.reset({}, self.fixture.settings.sectionOrder)
        self.session.rack_last = self.clock.now()
        self.frame_rows: list[str] = []
        self.render_count = 0

    def compose(self) -> ComposeResult:
        yield ProjectionHeader(id="header")
        yield Body(id="body")
        yield Keybar(id="keybar")

    def on_mount(self) -> None:
        self.render_frame()
        if not isinstance(self.clock, FakeClock):
            self.set_interval(0.25, self.tick)

    def on_resize(self, event: Resize) -> None:
        self.render_frame()

    # ---------- host protocol for the dispatcher ----------

    def quit(self) -> None:
        self.exit()

    def cycle_size(self) -> None:
        """The simulator's `w`: the harness resizes the terminal; the App only logs it."""
        s = self.session
        s.size = (s.size + 1) % len(SIZES)
        w, h = SIZES[s.size]
        s.log_key("w", f"size → {w}×{h}")

    # ---------- the frame ----------

    @property
    def frame_size(self) -> tuple[int, int]:
        w, h = self.size.width, self.size.height
        if w <= 0 or h <= 0:
            return SIZES[self.session.size]
        return (w, h)

    def reset(self, setup: dict | None) -> None:
        """The one canonical reset, the harness's entry point."""
        self.session.reset(setup, self.fixture.settings.sectionOrder)
        self.session.rack_last = self.clock.now()

    def notify_toast(self, text: str, title: str = "done", sev: str = "info") -> None:
        rack.notify(self.session, self.clock, text, title, sev)

    def compose_frame(self, w: int, h: int) -> list[str]:
        s = self.session
        fx = self.fixture
        s.record_facts = None
        s.record_nav = None
        s.renders += 1
        inline: list[str] | None = None
        pane_keys: str | None = None
        if s.prefix == "g":
            inline = render_drawer("go", s, fx, w, h)
            pane_keys = keybar(list(DRAWER_PAIRS["go"]), w)
        elif s.overlay in ("actions", "inspect", "raw"):
            inline = render_drawer(s.overlay, s, fx, w, h)
            pane_keys = keybar(list(DRAWER_PAIRS[s.overlay]), w)
        if inline is not None and pane_keys is not None:
            s.reserved = len(inline) + 2
            base = render_route(s, fx, w, h)
            s.reserved = 0
            body_rows = base[: h - 1]
            last_content = -1
            for i, r in enumerate(body_rows):
                if r.strip():
                    last_content = i
            budget = h - 2 - len(inline)
            hidden = max(0, last_content + 1 - budget)
            body = body_rows[: budget - 1 if hidden else budget]
            if hidden:
                hidden = last_content + 1 - len(body)
                body = body + [
                    pad(
                        f"   {hidden} more row{'' if hidden == 1 else 's'} below · Esc closes the pane",
                        w,
                    )
                ]
            rows = body + [pad(thin(w), w)] + [pad(line, w) for line in inline] + [pane_keys]
        elif s.overlay in (
            "consequence",
            "question",
            "pause",
            "evidence",
            "readiness",
            "resolution",
            "draft",
            "marker",
            "help",
            "palette",
        ):
            rows = render_overlay(s.overlay, s, fx, w, h)
        else:
            rows = render_route(s, fx, w, h)
        for i, r in enumerate(rows):
            if cell_len(r) != w:
                raise ValueError(f"frame row {i} is {cell_len(r)} cells, not {w}")
        if len(rows) != h:
            raise ValueError(f"frame has {len(rows)} rows, not {h}")
        return rows

    def render_frame(self) -> None:
        w, h = self.frame_size
        rack.sweep(self.session, self.clock)
        rows = self.compose_frame(w, h)
        self.frame_rows = rows
        self.render_count += 1
        self.query_one("#header", ProjectionHeader).set_rows(rows[:1])
        self.query_one("#body", Body).set_rows(rows[1 : h - 1])
        self.query_one("#keybar", Keybar).set_rows(rows[h - 1 :])

    def tick(self) -> None:
        """The live clock's sweep: expire toasts and the prefix, re-render on change."""
        ctx = Ctx(self.session, self.fixture, self, *self.frame_size)
        changed = rack.sweep(self.session, self.clock) or expire_prefix(ctx)
        if changed:
            self.render_frame()

    # ---------- keys ----------

    def press_js(self, key: str, shift: bool = False) -> None:
        """Dispatch a DOM-named key (what the harness and the journeys use)."""
        w, h = self.frame_size
        dispatch(Ctx(self.session, self.fixture, self, w, h), key, shift)
        self.render_frame()

    def on_key(self, event: Key) -> None:
        event.stop()
        event.prevent_default()
        js = to_js_key(event.key, event.character)
        if js is None:
            return
        key, shift = js
        self.press_js(key, shift)
