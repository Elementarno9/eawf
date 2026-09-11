"""The one key table and the one dispatcher.

``KEY`` is the shared vocabulary, ``ROUTE_KEYS`` the per-route ordered tables the keybar,
the help overlay and the gate all read, ``OVERLAY_KEYS`` the allowlist a full-frame
overlay accepts and ``DRAWER_KEYS`` the same for drawers. ``dispatch`` is the prototype's
two keydown handlers (the Phase G seam, then the core) ported in order, with every
route-local block delegated to the route module's optional ``seam`` hook so a new route
never edits this file.

Keys are named the way the DOM names them (``ArrowDown``, ``Escape``, ``.``, ``Y``); the
harness keymap translates Textual's names on the way in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..chassis import attention as att
from ..chassis import derive as dv
from ..chassis import rack
from ..chassis.registry import (
    GO_MAP,
    OVERLAY_ARROWS,
    SECTIONS,
    parent_of,
    route_for_id,
    route_word,
    subj_now,
)
from ..chassis.session import PREFIX_TIMEOUT, QUIT_CEILING, QUIT_FLOOR, Session

if TYPE_CHECKING:
    from ..chassis.fixture import Fixture
    from ..chassis.session import Clock


@dataclass(frozen=True, slots=True)
class KeyEntry:
    token: str
    label: str | None
    keys: tuple[str, ...]

    def pair(self) -> tuple[str, str]:
        return (self.token, str(self.label))


def _k(token: str, label: str | None, *keys: str) -> KeyEntry:
    return KeyEntry(token, label, keys or (token,))


KEY: dict[str, KeyEntry] = {
    "up": _k("↑↓", "row", "ArrowUp", "ArrowDown"),
    "upEv": _k("↑↓", "event", "ArrowUp", "ArrowDown"),
    "page": _k("PgUp PgDn", "page", "PageUp", "PageDown"),
    "enter": _k("Enter", "drill", "Enter"),
    "esc": _k("Esc", "back", "Escape"),
    "tab": _k("Tab", "buckets", "Tab"),
    "tabSec": _k("Tab", "section", "Tab"),
    "go": _k("g", "go", "g"),
    "filter": _k("\\", "filter", "\\"),
    "actions": _k(".", "actions", "."),
    "help": _k("?", "help", "?"),
    "inspect": _k("i", "inspect", "i"),
    "raw": _k("r", "raw", "r"),
    "copy": _k("y", "copy", "y"),
    "digest": _k("y", "copy digest", "y"),
    "answer": _k("a", "answer", "a"),
    "deny": _k("x", "deny", "x"),
    "snooze": _k("z", "snooze", "z"),
    "resolve": _k("v", "resolve", "v"),
    "ctrlFilter": _k("ctrl+f", "filter", "ctrl+f"),
}
K = KEY

# Every route's ordered key table, ported from proto-app.js ROUTE_KEYS and proto-g.js RK
# (the later registration wins where both files declare one route).
ROUTE_KEYS: dict[str, tuple[KeyEntry, ...]] = {
    "scope.home": (
        _k("↑↓", "tree", "ArrowUp", "ArrowDown"),
        K["enter"],
        _k("Tab", "list", "Tab"),
        K["go"],
        K["actions"],
        K["help"],
    ),
    "activity": (K["up"], K["page"], K["enter"], K["tab"], K["filter"], K["actions"]),
    "run.detail": (K["upEv"], K["actions"], K["raw"], K["inspect"], K["esc"]),
    "attention": (
        K["up"],
        K["tab"],
        K["answer"],
        K["deny"],
        K["snooze"],
        K["resolve"],
        K["actions"],
        K["esc"],
    ),
    "batch.detail": (K["up"], K["enter"], K["actions"], K["inspect"], K["esc"]),
    "milestone": (K["up"], K["enter"], K["tabSec"], K["actions"], K["digest"], K["esc"]),
    "track": (K["up"], _k("Tab", "group", "Tab"), K["enter"], K["actions"], K["inspect"], K["esc"]),
    "task.detail": (K["up"], K["enter"], K["actions"], K["inspect"], K["esc"]),
    "release": (
        K["up"],
        K["tabSec"],
        K["enter"],
        _k("m", "readiness", "m"),
        K["actions"],
        K["esc"],
    ),
    "timeline": (
        K["up"],
        _k("←→", "marker", "ArrowLeft", "ArrowRight"),
        K["tabSec"],
        K["enter"],
        K["actions"],
        K["esc"],
    ),
    "backlog": (K["up"], K["enter"], K["esc"]),
    "campaign": (K["up"], K["tabSec"], K["enter"], K["actions"], K["inspect"], K["esc"]),
    "history": (K["up"], K["filter"], K["enter"], K["copy"], K["esc"]),
    "settings": (K["up"], K["enter"], K["filter"], K["inspect"], K["esc"]),
    "trust": (K["up"], K["enter"], K["actions"], K["inspect"], K["esc"]),
    "evidence": (K["up"], K["enter"], K["copy"], K["esc"]),
    "evidence.digest": (K["copy"], K["esc"]),
    "health": (K["up"], K["enter"], K["filter"], K["esc"]),
    "sandbox.log": (K["up"], K["enter"], K["filter"], _k("p", None, "p"), K["esc"]),
    "unattended": (K["up"], K["enter"], K["esc"]),
    "search": (K["up"], K["enter"], K["filter"], K["esc"]),
    "transcript": (K["up"], K["enter"], _k("f", None, "f"), K["copy"], K["esc"]),
    "receipt": (K["copy"], K["esc"]),
    "cost.ceiling": (K["up"], K["enter"], K["esc"]),
    "crash.recovery": (K["up"], K["enter"], K["inspect"], K["esc"]),
    "git.pr": (K["up"], K["enter"], K["copy"], K["esc"]),
    "history.diff": (K["up"], K["enter"], K["esc"]),
    "settings.stack": (K["up"], K["esc"]),
    "notifications": (K["up"], K["esc"]),
    "merge.conflict": (K["up"], K["copy"], K["esc"]),
    "export": (K["up"], K["enter"], K["esc"]),
    "campaign.step": (K["up"], K["enter"], K["copy"], K["esc"]),
    "campaign.artifact": (_k("↑↓", "scroll", "ArrowUp", "ArrowDown"), K["copy"], K["esc"]),
}

# the keys a full-frame overlay accepts (the prototype's FULL); `-` always passes
OVERLAY_KEYS: dict[str, tuple[str, ...]] = {
    "help": ("Escape",),
    "palette": ("Escape",),
    "consequence": ("Enter", "Escape"),
    "question": ("1", "2", "3", "x", "Escape"),
    "pause": ("n", "c", "Escape"),
    "evidence": ("ArrowUp", "ArrowDown", "k", "j", "y", "Escape"),
    "readiness": ("ArrowUp", "ArrowDown", "k", "j", "Escape"),
    "resolution": ("Y", "Escape"),
    "draft": ("ArrowUp", "ArrowDown", "k", "j", "Enter", "p", "x", "Escape"),
    "marker": ("Escape",),
}
# the overlay's own keybar pairs, for the help and contract tests
OVERLAY_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "help": (("Esc", "close"),),
    "palette": (("type", "search"), ("↑↓", "row"), ("Enter", "go"), ("Esc", "close")),
    "consequence": (("Enter", "confirm"), ("Esc", "cancel")),
    "question": (("1 2 3", "pick an answer"), ("x", "decline"), ("Esc", "back — it stays open")),
    "pause": (("n", "reconcile"), ("c", "cancel"), ("Esc", "back")),
    "evidence": (("↑↓", "receipt"), ("y", "copy"), ("Esc", "back")),
    "readiness": (("↑↓", "signal"), ("Esc", "back")),
    "resolution": (("Y", "copy URN"), ("Esc", "back")),
    "draft": (("↑↓", "field"), ("Enter", "set"), ("p", "promote"), ("x", "defer"), ("Esc", "back")),
    "marker": (("Esc", "back"),),
}
# drawers: the keys a drawer owns and the keybar it swaps in
DRAWER_KEYS: dict[str, tuple[str, ...]] = {
    "go": tuple(GO_MAP) + ("Escape",),
    "actions": ("Escape", ".") + tuple("abcdefghijklmnopqrstuvwxyz*"),
    "inspect": ("y", "Escape", "Enter"),
    "raw": ("y", "Escape", "Enter"),
}
DRAWER_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "go": (("g …", "destination"), ("Esc", "cancel")),
    "actions": (("key", "run · consequence first"), ("Esc", "close")),
    "inspect": (("y", "copy"), ("Esc", "close")),
    "raw": (("y", "copy"), ("Esc", "close")),
}
# the keys the entry layer lets through beside the state's own keys
ENTRY_ALLOW: tuple[str, ...] = (
    "[",
    "]",
    "ArrowUp",
    "ArrowDown",
    "k",
    "j",
    "w",
    "?",
    "Escape",
    "Enter",
    "/",
)
GLOBAL_HELP: tuple[tuple[str, str, str], ...] = (
    ("g …", "go-prefix · Esc or 1.5s cancels, visibly", "g"),
    ("/", "command palette", "/"),
    (".", "action menu — disabled verbs stay visible, with reasons", "."),
    ("Esc", "back · restores scope, row and filter exactly", "Escape"),
    ("y", "copy the value · answers in the notification rack", "y"),
    ("Y", "copy the stable URN", "Y"),
    ("-", "dismiss the notifications — nothing else clears them", "-"),
    ("w", "cycle frame size (a property of this document)", "w"),
)
_MODIFIERS = frozenset({"Shift", "Control", "Alt", "Meta", "CapsLock"})


def entry_keys(session: Session, fixture: Fixture) -> tuple[KeyEntry, ...]:
    """The pre-session key table: the state's own keys plus the simulator pair and `/`."""
    P = fixture.proto
    e = P.entry[session.entry_sel] if session.entry_sel < len(P.entry) else P.entry[0]
    own = [KeyEntry(k, label, (k,)) for k, label in e.keys]
    if session.simulator:
        own.append(KeyEntry("[ ]", "state", ("[", "]")))
    own.append(KeyEntry("/", "attach later", ("/",)))
    return tuple(own)


def route_keys(
    session: Session, fixture: Fixture, route: str | None = None
) -> tuple[KeyEntry, ...]:
    r = route or session.route
    if r == "entry":
        return entry_keys(session, fixture)
    return ROUTE_KEYS.get(r, ())


def can(session: Session, fixture: Fixture, entry: KeyEntry) -> bool:
    return any(e.token == entry.token for e in route_keys(session, fixture))


def busy(session: Session) -> bool:
    return bool(session.overlay or session.typing or session.prefix or session.edit)


class Host(Protocol):
    """What the dispatcher needs from the app: a clock, a quit and the size cycle."""

    clock: Clock

    def quit(self) -> None: ...
    def cycle_size(self) -> None: ...


@dataclass(slots=True)
class Ctx:
    session: Session
    fixture: Fixture
    host: Host
    w: int
    h: int

    @property
    def s(self) -> Session:
        return self.session

    @property
    def clock(self) -> Clock:
        return self.host.clock

    def notify(self, text: str, title: str = "done", sev: str = "info") -> None:
        rack.notify(self.session, self.clock, text, title, sev)

    def log(self, key: str, note: str = "") -> None:
        self.session.log_key(key, note)

    def noop(self, key: str) -> None:
        self.session.noop(key)


def has_renderer(route: str) -> bool:
    from ..chassis.renderers import has_renderer as _has

    return _has(route)


def go(ctx: Ctx, route: str, why: str, entity_id: str | None = None) -> bool:
    """The Phase G navigation seam: push, move, clear the overlay."""
    s = ctx.s
    if not has_renderer(route):
        ctx.log("—", f"{why} → {route} is designed but not bound here")
        return False
    if s.route == route and subj_now(route, s.subj_id) == subj_now(route, entity_id):
        ctx.log("—", f"{why} · already here")
        return False
    s.back.push(s.route, s.sel, s.subj_id)
    s.route = route
    s.sel = 0
    s.sel_id = None
    s.subj_id = entity_id or None
    s.overlay = None
    ctx.log("—", f"{why} → {route}")
    return True


def _route_seam(ctx: Ctx, key: str, shift: bool) -> bool:
    from ..chassis.renderers import seam_for

    hook = seam_for(ctx.s.route)
    return bool(hook and hook(ctx, key, shift))


def expire_prefix(ctx: Ctx) -> bool:
    """The go-prefix deadline, checked on the console clock rather than a timer."""
    s = ctx.s
    if (
        s.prefix == "g"
        and s.prefix_deadline is not None
        and not s.rack_hold
        and ctx.clock.now() >= s.prefix_deadline
    ):
        s.prefix = None
        s.prefix_deadline = None
        s.prefix_cancels += 1
        ctx.log("—", "prefix timed out after 1.5s")
        return True
    return False


def dispatch(ctx: Ctx, key: str, shift: bool = False) -> None:
    """One keystroke: the seam first, then the core, in the prototype's order."""
    expire_prefix(ctx)
    if _seam(ctx, key, shift):
        ctx.s.disarm_quit()
        return
    _core(ctx, key, shift)
    # the Phase F state-model probe is a second listener: it runs after the core whatever
    # path the core took, and never after a key the seam claimed
    _cycle_overlay_state(ctx, key)


# ---------- the Phase G seam (window capture) ----------


def _seam(ctx: Ctx, key: str, shift: bool) -> bool:
    s = ctx.s
    if key == "Tab" and shift and s.route != "settings":
        return False
    k = key
    if k == "j":
        k = "ArrowDown"
    elif k == "k":
        k = "ArrowUp"
    if (
        s.absent_frame
        and not s.overlay
        and re.match(r"^(y|r|i|\.|Enter|ArrowUp|ArrowDown|Tab)$", k)
    ):
        ctx.log(k, "nothing is recorded here — Esc goes back")
        return True
    if s.record_facts is not None and not s.overlay:
        facts = " ".join(s.record_facts)
        nav = s.record_nav or []
        if nav and re.match(r"^(Enter|ArrowUp|ArrowDown)$", k):
            if k != "Enter":
                s.sel = ((s.sel or 0) + (1 if k == "ArrowDown" else len(nav) - 1)) % len(nav)
                ctx.log(k, str(nav[s.sel]))
                return True
            to = nav[s.sel or 0]
            dest = route_for_id(to) or "milestone"
            go(ctx, dest, f"record · {to}", to)
            return True
        needs = {
            "y": "bundle",
            "r": "timeline",
            "Enter": "row",
            "ArrowUp": "row",
            "ArrowDown": "row",
            "Tab": "section",
        }
        if k in needs and needs[k] not in facts:
            ctx.log(k, f"no {needs[k]} is recorded for this entity")
            return True
    if _route_seam(ctx, k, shift):
        return True
    if s.overlay == "question" and not s.reply and k == "w":
        s.reply = {"text": ""}
        ctx.log("w", "reply field open · ≤ 2,000 characters · Enter sends")
        return True
    if s.reply is not None:
        if k == "Escape":
            s.reply = None
            ctx.log("Esc", "reply discarded · the question is still open")
        elif k == "Enter":
            ctx.log("Enter", f"reply sent verbatim · immutable · {len(s.reply['text'])} chars")
            s.reply = None
        elif k == "Backspace":
            s.reply["text"] = s.reply["text"][:-1]
        elif len(k) == 1 and len(s.reply["text"]) < 2000:
            s.reply["text"] += k
        return True
    return False


# ---------- the core handler (document capture) ----------


def _palette_hits(ctx: Ctx) -> list[Any]:
    from ..chassis.overlays import module_for

    mod = module_for("palette")
    hits = getattr(mod, "hits", None)
    return list(hits(ctx.s, ctx.fixture)) if hits else []


def fire_light(ctx: Ctx, a: tuple[str, ...], k: str) -> None:
    """A light verb answers in the rack: it opens its destination or names the clipboard."""
    s, fx = ctx.s, ctx.fixture
    ok, why = att.verb_available(s, fx, a)
    if not ok:
        ctx.notify(why, "refused", "err")
        ctx.log(k, f"refused: {why}")
        return
    s.overlay = None
    s.pane_sel = 0
    to = a[8] if len(a) > 8 else None
    if not to:
        ctx.notify(dv.copy_target(s, fx), "copied")
        ctx.log(k, f"{a[1]} · {dv.copy_target(s, fx)}")
        return
    if not has_renderer(to):
        ctx.notify(f"{to} is designed but not bound here", "unavailable", "err")
        ctx.log(k, f"{a[1]} → {to} is not bound in this prototype")
        return
    s.back.push(s.route, s.sel, s.subj_id)
    s.route = to
    s.sel = 0
    s.sel_id = None
    s.subj_id = None
    ctx.notify(route_word(to), "opened")
    ctx.log(k, f"{a[1]} → {to}")


def _drill(ctx: Ctx, dst: str, did: str | None) -> None:
    s = ctx.s
    s.back.push(s.route, s.sel, s.subj_id)
    s.route = dst
    s.sel = 0
    s.subj_id = did or None
    s.sel_id = None
    ctx.log("Enter", f"drill → {dst}" + (f" · {did}" if did else ""))


def _core(ctx: Ctx, key: str, shift: bool) -> None:
    s, fx = ctx.s, ctx.fixture
    P = fx.proto
    k = key
    if k == "Tab" and shift:
        ctx.log("S-Tab", "left the canvas — the page has the keyboard again")
        return
    s.keys += 1
    if k != "Escape":
        s.last_esc = 0.0
    # palette typing
    if s.overlay == "palette":
        if k in ("ArrowDown", "ArrowUp"):
            s.sel = s.sel + 1 if k == "ArrowDown" else max(0, s.sel - 1)
            ctx.log(k, "palette row")
            return
        if len(k) == 1 and re.match(r"[a-z0-9.\- ]", k, re.I):
            s.pq += k.lower()
            s.sel = 0
            s.pscroll = 0
            ctx.log(k, "palette search")
            return
        if k == "Backspace":
            s.pq = s.pq[:-1]
            s.sel = 0
            s.pscroll = 0
            ctx.log("Bksp", "palette search")
            return
        if k == "Enter":
            hits = _palette_hits(ctx)
            h = hits[min(s.sel, len(hits) - 1)] if hits else None
            if h is None:
                s.overlay = None
                s.pq = ""
                ctx.log("Enter", "nothing matched — nowhere to go")
                return
            if not has_renderer(h["to"]):
                s.overlay = None
                s.pq = ""
                ctx.log("Enter", f"{h['to']} is designed but not built in this prototype")
                return
            s.back.clear()
            dv.fresh_arrival(s, h["to"])
            s.route = h["to"]
            s.sel = 0
            s.sel_id = None
            s.overlay = None
            s.pq = ""
            s.pscroll = 0
            s.subj_id = h["id"] if h["kind"] == "entity" else None
            ctx.log(
                "Enter",
                f"palette → {s.route}"
                + (
                    f" · {h['id']}"
                    if h["kind"] == "entity"
                    else (" · every hit" if h["kind"] == "search" else "")
                ),
            )
            return
    # inline filter typing
    if s.typing:
        if k == "Escape":
            s.typing = False
            dv.set_filter(s, "")
            s.sel = 0
            s.scroll = 0
            ctx.log("Esc", "filter cleared")
        elif k == "Enter":
            s.typing = False
            ctx.log("Enter", f"filter kept · {dv.filter_of(s) or 'none'}")
        elif k == "Backspace":
            dv.set_filter(s, dv.filter_of(s)[:-1])
            s.sel = 0
            s.scroll = 0
            ctx.log("Bksp", f"filter {dv.filter_of(s) or 'empty'}")
        elif k == "ArrowDown":
            s.sel += 1
            ctx.log("↓", "row · filter still open")
        elif k == "ArrowUp":
            s.sel = max(0, s.sel - 1)
            ctx.log("↑", "row · filter still open")
        elif len(k) == 1:
            dv.set_filter(s, dv.filter_of(s) + k)
            s.sel = 0
            s.scroll = 0
            ctx.log(k, f"filter \\{dv.filter_of(s)}")
        else:
            ctx.log(k, "ignored while the filter field is open")
        return
    # overlay-local verbs, before the route switch
    if s.overlay == "question":
        q = att.question_row(s, fx)
        if k in ("1", "2", "3"):
            if att.gated(s, fx, k, "answering", q):
                return
            ans = ["the wave shim only", "the alias table only", "both — they are independent"][
                int(k) - 1
            ]
            if q is not None:
                stamp = "14:02:41"
                q.ledger = [("requested", stamp), ("accepted", stamp), ("confirmed", stamp)]
                q.state = "ANSWERED"
            s.overlay = None
            ctx.log(k, f"{q.id if q else ''} answered · {ans}")
            return
        if k == "x":
            if att.gated(s, fx, "x", "decline", q):
                return
            s.verb = "x"
            s.sel_id = q.id if q else None
            s.overlay = "consequence"
            ctx.log("x", f"decline {q.id if q else ''} → consequence preview")
            return
        if k == "Escape":
            s.overlay = None
            ctx.log("Esc", f"back — {q.id if q else ''} stays open")
            return
        ctx.noop(k)
        return
    if s.overlay == "pause":
        if k in ("n", "c"):
            if att.gated(s, fx, k, "reconcile" if k == "n" else "cancel", None):
                return
            s.c_target = (
                {
                    "verb": "reconcile",
                    "id": "RUN-a708a7d6",
                    "kind": "run",
                    "effects": "the daemon is asked again for the outcome of the pause",
                    "not": "it does not restart the run and does not fail the task",
                }
                if k == "n"
                else {
                    "verb": "cancel",
                    "id": "RUN-a708a7d6",
                    "kind": "run",
                    "effects": "cancel is requested; it stays unknown until the run answers",
                    "not": "it does not fail the task and does not undo any merged work",
                }
            )
            s.overlay = "consequence"
            ctx.log(k, f"{s.c_target['verb']} RUN-a708a7d6 → consequence preview")
            return
        if k == "Escape":
            s.overlay = None
            ctx.log("Esc", "back — the pause stays unknown")
            return
        ctx.noop(k)
        return
    # the armed go prefix
    if s.prefix == "g":
        s.prefix = None
        s.prefix_deadline = None
        dest = GO_MAP.get(k)
        if dest and has_renderer(dest):
            if dest == s.route and not s.subj_id:
                ctx.log(f"g {k}", f"already here · {s.route}")
            else:
                dv.fresh_arrival(s, dest)
                s.route = dest
                s.sel = 0
                s.subj_id = None
                s.back.clear()
                ctx.log(f"g {k}", f"→ {s.route} · top level, the path starts here")
        elif k == "Escape":
            s.prefix_cancels += 1
            ctx.log("Esc", "prefix cancelled")
        else:
            ctx.log(f"g {k}", "no such destination — prefix dropped")
        return
    # a drawer owns the keyboard
    if s.overlay in ("inspect", "raw"):
        if k == "Escape":
            s.overlay = None
            ctx.log("Esc", "close pane")
            return
        if k == "y":
            ctx.notify(dv.copy_target(s, fx), "copied")
            ctx.log("y", f"copied — {dv.copy_target(s, fx)}")
            return
        if k in _MODIFIERS:
            s.keys -= 1
            return
        ctx.noop(k)
        return
    # the pre-session layer owns the keyboard too
    if s.route == "entry" and not s.overlay and not s.typing and k == "Escape":
        es = P.entry[s.entry_sel] if s.entry_sel < len(P.entry) else P.entry[0]
        if es.exit == "terminal":
            ctx.log("Esc", "exit 4 · session ended · nothing was read, nothing was changed")
            return
        if es.exit == "session":
            s.route = "scope.home"
            s.conn = "OFFLINE SNAPSHOT"
            s.sel = 0
            s.sel_id = None
            ctx.log("Esc", "attached read-only · scope home under OFFLINE SNAPSHOT")
            return
        s.entry_sel = 1
        s.path_sel = 0
        ctx.log("Esc", "cancelled · not attached")
        return
    if s.route == "entry" and not s.overlay and not s.typing:
        e = P.entry[s.entry_sel] if s.entry_sel < len(P.entry) else P.entry[0]
        allow = [p[0] for p in e.keys] + list(ENTRY_ALLOW)
        if k not in allow:
            ctx.noop(k)
            return
    # a full-frame overlay owns the keyboard
    if k != "-" and s.overlay in OVERLAY_KEYS and k not in OVERLAY_KEYS[s.overlay]:
        ctx.noop(k)
        return
    # the action drawer is letter-driven
    pane = s.overlay == "actions"
    if pane and len(k) == 1:
        for a in att.menu_order(s, fx):
            if a[0] == k:
                if att.is_light(a):
                    fire_light(ctx, a, k)
                    return
                ok, why = att.verb_available(s, fx, a)
                if not ok:
                    ctx.log(k, f"refused: {why}")
                    return
                if s.route == "attention" and k in att.VERB:
                    s.verb = k
                    s.c_target = None
                else:
                    s.c_target = {
                        "verb": a[1],
                        "state": None,
                        "id": dv.target_id(s, fx),
                        "kind": s.route,
                        "effects": a[5] if len(a) > 5 else "",
                        "not": a[6] if len(a) > 6 else "",
                    }
                s.overlay = "consequence"
                ctx.log(k, f"{a[1]} → consequence preview")
                return
    _switch(ctx, k, pane)


def _arrow_down(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay == "draft":
        s.draft_field = min(2, (s.draft_field or 0) + 1)
        ctx.log(k, ["criteria", "owner", "batch"][s.draft_field])
        return
    if s.route == "timeline" and (s.tl_reg or "LANES") != "LANES":
        rl = (s.tl_regs or {}).get(s.tl_reg, [])
        s.tl_sel = min(len(rl) - 1, (s.tl_sel or 0) + 1)
        ctx.log("ArrowDown", rl[s.tl_sel][0] if 0 <= s.tl_sel < len(rl) else "")
        return
    if s.route == "release" and s.rel_reg == "READINESS":
        s.rel_sel = min(3, (s.rel_sel or 0) + 1)
        ctx.log("ArrowDown", f"readiness row {s.rel_sel + 1}")
        return
    _arrow_common(ctx, k, pane, +1)


def _arrow_up(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay == "draft":
        s.draft_field = max(0, (s.draft_field or 0) - 1)
        ctx.log(k, ["criteria", "owner", "batch"][s.draft_field])
        return
    if s.route == "timeline" and (s.tl_reg or "LANES") != "LANES":
        rl = (s.tl_regs or {}).get(s.tl_reg, [])
        s.tl_sel = max(0, (s.tl_sel or 0) - 1)
        ctx.log("ArrowUp", rl[s.tl_sel][0] if 0 <= s.tl_sel < len(rl) else "")
        return
    if s.route == "release" and s.rel_reg == "READINESS":
        s.rel_sel = max(0, (s.rel_sel or 0) - 1)
        ctx.log("ArrowUp", f"readiness row {s.rel_sel + 1}")
        return
    _arrow_common(ctx, k, pane, -1)


def _arrow_common(ctx: Ctx, k: str, pane: bool, direction: int) -> None:
    s = ctx.s
    P = ctx.fixture.proto
    if pane:
        ctx.noop(k)
        return
    if s.overlay and s.overlay not in OVERLAY_ARROWS:
        ctx.noop(k)
        return
    if s.route == "entry":
        e = P.entry[s.entry_sel] if s.entry_sel < len(P.entry) else P.entry[0]
        if direction > 0:
            ps = len(e.paths or ()) or len(e.rows or ())
            if ps:
                s.path_sel = min(ps - 1, s.path_sel + 1)
                ctx.log(k, "row")
            else:
                ctx.log(k, "this state has no rows")
        else:
            s.path_sel = max(0, s.path_sel - 1)
            ctx.log(k, "row")
        return
    if direction > 0:
        s.sel += 1
        s.sel_id = None
        ctx.log(k, "down")
    else:
        s.sel = max(0, s.sel - 1)
        s.sel_id = None
        ctx.log(k, "up")


def _enter(ctx: Ctx, pane: bool) -> None:
    s, fx = ctx.s, ctx.fixture
    P = fx.proto
    if pane:
        ctx.noop("Enter")
        return
    if s.overlay == "draft":
        ctx.log(
            "Enter",
            f"set {['criteria', 'owner', 'batch'][s.draft_field or 0]} · the field the cursor is on",
        )
        return
    if s.overlay == "consequence":
        if s.c_target:
            t = s.c_target
            s.c_target = None
            s.overlay = None
            ctx.log(
                "Enter",
                f"{t['verb']} requested on {t['id']} — accepted · the ledger is modelled for attention only in this prototype",
            )
            return
        rows = att.attn_list(s, fx) if s.route == "attention" else []
        act = rows[s.sel] if s.sel < len(rows) else P.attention[0]
        stamp = "14:02:41"
        v = att.VERB[s.verb or "a"]
        act.ledger = [("requested", stamp), ("accepted", stamp), ("confirmed", stamp)]
        act.state = v["state"]
        s.overlay = None
        ctx.log("Enter", f"{act.id} → {act.state} · ledger confirmed")
        return
    if s.overlay in ("inspect", "raw"):
        s.overlay = None
        ctx.log("Enter", "pane closed — it does not survive a navigation")
        return
    r = s.route
    if r == "scope.home":
        ctx.log("Enter", "nothing to drill")
    elif r == "track":
        tsel = {
            "MILESTONES": [
                ("MLS-0001", "milestone"),
                ("MLS-0003", "milestone"),
                ("MLS-0000", "milestone"),
            ],
            "CAMPAIGNS": [("CAM-0001", "campaign")],
            "SHAPED QUEUE": [("EAWF-0091", "task.detail"), ("EAWF-0092", "task.detail")],
        }
        grp = tsel.get(s.track_group or "MILESTONES", tsel["MILESTONES"])
        pick = grp[min(s.sel, len(grp) - 1)]
        s.back.push(s.route, s.sel, s.subj_id)
        s.route = pick[1]
        s.sel = 0
        s.subj_id = pick[0]
        s.sel_id = None
        ctx.log("Enter", f"drill → {pick[1]} · {pick[0]}")
    elif r == "task.detail":
        nav = s.record_nav or []
        did = nav[s.sel] if s.sel < len(nav) else None
        _drill(ctx, route_for_id(did) or "run.detail", did)
    elif r == "release" and s.rel_reg == "READINESS":
        s.overlay = "readiness"
        s.ov_state["readiness"] = s.rel_sel or 0
        ctx.log("Enter", "readiness matrix · on the signal you were reading")
    elif r == "release":
        nav = s.record_nav or []
        did = nav[s.sel] if s.sel < len(nav) else None
        _drill(ctx, route_for_id(did) or "milestone", did)
    elif r == "entry":
        e = P.entry[s.entry_sel]
        if e.paths:
            ctx.log(
                "Enter",
                f"eawf migrate --{e.paths[s.path_sel][0].replace(' ', '-')} · shown, never auto-applied",
            )
        elif e.id == "ambiguous":
            ctx.log(
                "Enter",
                f"attach to {(e.rows or (('',),))[s.path_sel][0]} · remembered for this session only",
            )
        elif e.id == "failed":
            ctx.log("Enter", "copied: eawf workspace register .")
        elif e.id == "schema":
            ctx.log("Enter", "copied: eawf export --read-only")
        elif e.id == "offline":
            ctx.log(
                "Enter",
                f"drill → {(e.rows or (('',),))[s.path_sel][0]} · read-only, controls are gone",
            )
        else:
            ctx.log("Enter", "nothing to run while attaching")
    elif r == "campaign":
        s.overlay = "evidence"
        ctx.log("Enter", "evidence viewer · the receipts behind this claim")
    elif r == "backlog":
        s.overlay = "draft"
        ctx.log("Enter", "draft detail · what it still needs")
    elif r == "timeline" and (s.tl_reg or "LANES") != "LANES":
        rl = (s.tl_regs or {}).get(s.tl_reg, [])
        pick = rl[s.tl_sel or 0] if (s.tl_sel or 0) < len(rl) else None
        if not pick:
            ctx.log("Enter", f"nothing to open in {s.tl_reg}")
        elif s.tl_reg == "UNDATED":
            s.back.push(s.route, s.sel, s.subj_id)
            s.route = "milestone"
            s.subj_id = pick[0]
            s.sel = 0
            ctx.log("Enter", f"→ {pick[0]} · {pick[1]}")
        else:
            s.back.push(s.route, s.sel, s.subj_id)
            s.route = "release"
            s.subj_id = pick[0]
            s.sel = 0
            ctx.log("Enter", f"→ {pick[0]} {pick[1]} · {str(pick[3]).lower()}")
    elif r == "timeline":
        from ..chassis.renderers import module_for

        tl = module_for("timeline")
        names = getattr(tl, "TL_NAMES", ("Runtime",))
        marker_glyph = getattr(tl, "marker_glyph", lambda lane, ix: "●")
        s.overlay = "marker"
        mlane = names[s.sel] if s.sel < len(names) else names[0]
        s.marker_card = {
            "id": s.timeline_marker or "MLS-0001",
            "lane": mlane,
            "glyph": marker_glyph(mlane, s.mark),
        }
        ctx.log("Enter", f"marker detail · {s.marker_card['id']} on the {mlane} lane")
    elif r == "history":
        s.overlay = "resolution"
        ctx.log("Enter", "resolution card — what happened to this target")
    elif r == "settings":
        ctx.log("Enter", "the WHY pane below already shows this chain")
    elif r == "milestone":
        nav = s.record_nav or []
        mtar = nav[s.sel] if s.sel < len(nav) else None
        if mtar:
            s.back.push(s.route, s.sel, s.subj_id)
            s.route = route_for_id(mtar) or "batch.detail"
            s.sel = 0
            s.subj_id = mtar
            s.sel_id = None
            ctx.log("Enter", f"drill → {s.route} · {mtar}")
        else:
            s.overlay = "evidence"
            ctx.log("Enter", "evidence viewer at this digest")
    elif r == "activity":
        row = dv.current_fleet_row(s, fx)
        _drill(ctx, "run.detail", row.run if row else None)
    elif r == "batch.detail":
        nav = s.record_nav or []
        did = nav[s.sel] if s.sel < len(nav) else None
        _drill(ctx, route_for_id(did) or "task.detail", did)
    elif r == "attention":
        rows = att.attn_list(s, fx)
        sel2 = rows[s.sel] if s.sel < len(rows) else (rows[0] if rows else None)
        kind = (sel2.kind if sel2 else "") or ""
        if "question" in kind:
            s.overlay = "question"
            ctx.log("Enter", f"question detail · {sel2.id if sel2 else ''}")
        elif sel2 and sel2.bucket == "lost":
            s.overlay = "pause"
            ctx.log("Enter", "pause detail · the outcome is unknown")
        else:
            s.overlay = "consequence"
            ctx.log("Enter", f"action detail · {sel2.id if sel2 else ''}")


def _escape(ctx: Ctx) -> None:
    s, fx = ctx.s, ctx.fixture
    now = ctx.clock.now()
    if s.overlay:
        was = s.overlay
        s.overlay = None
        s.pq = ""
        if was == "consequence":
            ctx.log("Esc", f"cancelled — {dv.target_id(s, fx)} unchanged, nothing was written")
        else:
            ctx.log("Esc", "close overlay")
        return
    if s.route in ("activity", "attention") and s.bucket:
        s.bucket = None
        s.sel = 0
        s.scroll = 0
        ctx.log("Esc", "bucket cleared — every bucket shows")
        return
    if s.route == "scope.home" and not s.back:
        gap = now - s.last_esc
        if QUIT_FLOOR < gap < QUIT_CEILING:
            ctx.log(
                "Esc Esc", f"quit — guarded: scope home, nothing open, {int(gap * 1000)}ms apart"
            )
            s.last_esc = 0.0
            ctx.host.quit()
            return
        if gap <= QUIT_FLOOR:
            ctx.log("Esc", "too fast to be two presses — still armed")
            return
        ctx.log("Esc", "at scope home · press again within 1.5s to quit")
        s.last_esc = now
        return
    b = s.back.pop()
    if b:
        s.route = b.route
        s.sel = b.sel
        s.subj_id = b.subj
        s.sel_id = None
        ctx.log(
            "Esc",
            f"back → {b.route}" + (f" · {b.subj}" if b.subj else "") + " · selection restored",
        )
        return
    up = parent_of(s, fx)
    if up:
        s.route = up[0]
        s.sel = 0
        s.subj_id = up[1]
        s.sel_id = None
        ctx.log(
            "Esc",
            f"up → {up[0]}" + (f" · {up[1]}" if up[1] else "") + " · one step up the breadcrumb",
        )
        return
    ctx.log("Esc", "at scope home · press again within 1.5s to quit")
    s.last_esc = now


def _tab(ctx: Ctx) -> None:
    s, fx = ctx.s, ctx.fixture
    P = fx.proto
    if s.route == "release":
        s.rel_reg = "READINESS" if (s.rel_reg or "MEMBERSHIP") == "MEMBERSHIP" else "MEMBERSHIP"
        s.rel_sel = 0
        ctx.log("Tab", f"region → {s.rel_reg}")
    elif s.route == "timeline":
        tr = ["LANES", "UNDATED", "RELEASES"]
        s.tl_reg = tr[(tr.index(s.tl_reg or "LANES") + 1) % len(tr)]
        s.tl_sel = 0
        ctx.log("Tab", f"region → {s.tl_reg}")
    elif s.route == "activity":
        keys = [b.key for b in P.buckets]
        ix = keys.index(s.bucket) if s.bucket in keys else -1
        s.bucket = None if ix + 1 >= len(keys) else keys[ix + 1]
        s.sel = 0
        s.scroll = 0
        ctx.log("Tab", f"bucket → {s.bucket or 'all'}")
    elif s.route == "attention":
        xk: list[str | None] = [None]
        for b in P.xbuckets:
            xk.append(b.key)
            xk.extend(sb.key for sb in b.sub or ())
        xi = xk.index(s.bucket) if s.bucket in xk else 0
        s.bucket = xk[(xi + 1) % len(xk)]
        s.sel = 0
        s.scroll = 0
        ctx.log("Tab", "bucket → " + (att.xlabel(fx, s.bucket) if s.bucket else "all"))
    elif s.route == "milestone":
        s.section = (s.section + 1) % len(SECTIONS)
        ctx.log("Tab", f"section → {SECTIONS[s.section]}")
    elif s.route == "track":
        tg = ["MILESTONES", "CAMPAIGNS", "SHAPED QUEUE"]
        s.track_group = tg[(tg.index(s.track_group or "MILESTONES") + 1) % len(tg)]
        s.sel = 0
        s.scroll = 0
        ctx.log("Tab", f"group → {s.track_group}")
    else:
        ctx.noop("Tab")


def _switch(ctx: Ctx, k: str, pane: bool) -> None:
    s, fx = ctx.s, ctx.fixture
    P = fx.proto
    if k == "PageDown":
        s.sel += s.visible
        s.sel_id = None
        ctx.log("PgDn", "page down")
    elif k == "PageUp":
        s.sel = max(0, s.sel - s.visible)
        s.sel_id = None
        ctx.log("PgUp", "page up")
    elif k == "Home":
        if pane:
            ctx.noop("Home")
        else:
            s.sel = 0
            s.sel_id = None
            ctx.log("Home", "first row")
    elif k == "End":
        if pane:
            ctx.noop("End")
        else:
            s.sel = 1_000_000
            s.sel_id = None
            ctx.log("End", "last row")
    elif k in ("ArrowDown", "j"):
        if k == "j" and pane:
            ctx.noop(k)
        else:
            _arrow_down(ctx, k, pane) if k == "ArrowDown" else _arrow_common(ctx, k, pane, +1)
    elif k in ("ArrowUp", "k"):
        if k == "k" and pane:
            ctx.noop(k)
        else:
            _arrow_up(ctx, k, pane) if k == "ArrowUp" else _arrow_common(ctx, k, pane, -1)
    elif k == "Enter":
        _enter(ctx, pane)
    elif k == "Escape":
        _escape(ctx)
    elif k == "m":
        if s.route == "release":
            s.overlay = "readiness"
            s.sel = 0
            ctx.log("m", "readiness matrix · every signal with its evidence")
        else:
            ctx.noop("m")
    elif k == ".":
        if s.overlay != "actions" and not P.actions.get(s.route):
            ctx.notify("No action is available here.", "NO ACTIONS", "warn")
            ctx.log(".", "no verb for this route — nothing to open")
        else:
            s.overlay = None if s.overlay == "actions" else "actions"
            s.c_target = None
            ctx.log(".", "action menu")
    elif k == "i":
        if s.overlay == "consequence":
            ctx.log("i", "not offered on a consequence card — Enter confirms, Esc cancels")
        elif not can(s, fx, KEY["inspect"]):
            ctx.noop("i")
        else:
            s.overlay = None if s.overlay == "inspect" else "inspect"
            ctx.log("i", "inspect the focused field")
    elif k == "r":
        if s.overlay == "consequence":
            ctx.log("r", "not offered on a consequence card — Enter confirms, Esc cancels")
        elif not can(s, fx, KEY["raw"]):
            ctx.noop("r")
        else:
            s.overlay = None if s.overlay == "raw" else "raw"
            ctx.log("r", "bounded raw segment")
    elif k == "p":
        if s.overlay == "draft":
            miss = s.draft_miss or []
            if miss:
                ctx.notify(f"It still needs {_list_of(miss)}.", "NOT PROMOTED", "warn")
                ctx.log("p", f"refused · {_list_of(miss)} unanswered")
            else:
                ctx.notify(
                    "Promoted into BAT-0002 · the batch decides when it runs.", "PROMOTED", "ok"
                )
                ctx.log("p", "promoted · nothing was dispatched")
        else:
            ctx.noop("p")
    elif k == "x":
        if s.overlay == "draft":
            ctx.notify("Deferred · it keeps its due scope and leaves the drafts list.", "DEFERRED")
            ctx.log("x", "deferred · the draft is not discarded")
        elif s.route == "attention":
            _attention_verb(ctx, k)
        else:
            ctx.noop("x")
    elif k == "y":
        ctx.notify(dv.copy_target(s, fx), "copied")
        ctx.log("y", f"copied — {dv.copy_target(s, fx)}")
    elif k == "Y":
        u = dv.urn(s, fx)
        ctx.notify(u, "copied URN")
        ctx.log("Y", f"copied URN — {u}")
    elif k == "-":
        if not s.toasts:
            ctx.noop("-")
        else:
            n = rack.clear(s)
            ctx.log("-", f"{n} notification{'' if n == 1 else 's'} dismissed")
    elif k == "?":
        s.overlay = None if s.overlay == "help" else "help"
        ctx.log("?", "route help")
    elif k == "/":
        s.overlay = "palette"
        s.pq = ""
        s.sel = 0
        s.pscroll = 0
        ctx.log("/", "command palette")
    elif k in ("\\", "ctrl+f"):
        if not can(s, fx, KEY["filter"]):
            ctx.noop("\\")
        else:
            s.typing = True
            dv.set_filter(s, "")
            s.sel = 0
            s.scroll = 0
            ctx.log("\\", "filter — type to narrow, Esc clears")
    elif k == "g":
        s.prefix = "g"
        s.prefix_seq += 1
        s.prefix_deadline = ctx.clock.now() + PREFIX_TIMEOUT
        ctx.log("g", "prefix armed")
    elif k == "Tab":
        _tab(ctx)
    elif k in ("[", "]"):
        if s.route == "entry" and s.simulator:
            n = len(P.entry)
            s.entry_sel = (s.entry_sel + (1 if k == "]" else n - 1)) % n
            s.path_sel = 0
            ctx.log(k, f"entry state → {P.entry[s.entry_sel].id}")
        else:
            ctx.noop(k)
    elif k in ("ArrowLeft", "ArrowRight"):
        if s.route == "timeline":
            nm = s.timeline_marks or 0
            if not nm:
                ctx.noop(k)
            else:
                s.mark = ((s.mark or 0) + (1 if k == "ArrowRight" else nm - 1)) % nm
                ctx.log(
                    "→" if k == "ArrowRight" else "←", f"marker → {s.mark + 1} of {nm} on this lane"
                )
        else:
            ctx.noop(k)
    elif k == "w":
        if s.simulator:
            ctx.host.cycle_size()
        else:
            ctx.noop("w")
    elif k in ("a", "z", "v"):
        if s.route == "attention":
            _attention_verb(ctx, k)
    elif k in _MODIFIERS:
        s.keys -= 1
    else:
        ctx.noop(k)


def _attention_verb(ctx: Ctx, k: str) -> None:
    s, fx = ctx.s, ctx.fixture
    rows = att.attn_list(s, fx)
    sel = rows[s.sel] if s.sel < len(rows) else None
    if att.gated(s, fx, k, att.VERB[k]["name"], sel):
        return
    s.verb = k
    s.c_target = None
    s.overlay = "consequence"
    ctx.log(k, f"{att.VERB[k]['name']} → consequence preview first")


OV_MODEL_NAMES: tuple[str, ...] = ("question", "pause", "evidence", "readiness")


def _cycle_overlay_state(ctx: Ctx, k: str) -> None:
    """The `s` key steps a decision overlay through its state model (the Phase F probe)."""
    from ..chassis.overlays import module_for

    s = ctx.s
    if k != "s" or s.overlay not in OV_MODEL_NAMES:
        return
    mod = module_for(s.overlay)
    model = getattr(mod, "OV_MODEL", None)
    n = len(model) if model else 0
    if not n:
        return
    s.ov_state[s.overlay] = ((s.ov_state.get(s.overlay, 0)) + 1) % n
    ctx.log("s", f"{s.overlay} → {model[s.ov_state[s.overlay]][0]}")


def _list_of(xs: list[str]) -> str:
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def keybar_pairs(session: Session, fixture: Fixture) -> list[tuple[str, str]]:
    """The route's advertised pairs, in table order (the help overlay reads the same)."""
    return [e.pair() for e in route_keys(session, fixture)]


def allowlist(session: Session, fixture: Fixture) -> frozenset[str]:
    """Every key the active surface consumes; the gate refuses the rest in silence."""
    s = session
    if s.overlay in OVERLAY_KEYS:
        return frozenset(OVERLAY_KEYS[s.overlay]) | {"-"}
    if s.overlay in DRAWER_KEYS:
        return frozenset(DRAWER_KEYS[s.overlay])
    if s.prefix == "g":
        return frozenset(DRAWER_KEYS["go"])
    keys: set[str] = set()
    for e in route_keys(session, fixture):
        keys.update(e.keys)
    keys.update(x[2] for x in GLOBAL_HELP)
    keys.update({"?", "Escape", "j", "k"})
    return frozenset(keys)


__all__ = [
    "DRAWER_KEYS",
    "DRAWER_PAIRS",
    "ENTRY_ALLOW",
    "GLOBAL_HELP",
    "KEY",
    "OVERLAY_KEYS",
    "OVERLAY_PAIRS",
    "ROUTE_KEYS",
    "Ctx",
    "Host",
    "KeyEntry",
    "allowlist",
    "busy",
    "can",
    "dispatch",
    "entry_keys",
    "expire_prefix",
    "fire_light",
    "go",
    "has_renderer",
    "keybar_pairs",
    "route_keys",
]
