"""The dispatcher: one keystroke, through the route's own keys and then the console's.

The order is fixed. An expired go prefix is dropped first. Then the frame-level keys run:
an absent or record frame's keys, the route's own key hook, and the question overlay's
reply field. What they leave goes to the console keys, where whatever owns the keyboard
(the palette, the filter field, a decision overlay, the armed prefix, a drawer, the entry
layer, a full-frame overlay, the action menu) is asked before the route-independent keys.
A decision overlay's state step on ``s`` runs last, after whatever the console keys did.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console import attention as att
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import drill
from eawf.surfaces.tui.console import keymap as km
from eawf.surfaces.tui.console.action_menu import MenuVerb, light_opening
from eawf.surfaces.tui.console.attach import ONBOARDING, skip_step
from eawf.surfaces.tui.console.clock import (
    arm_prefix,
    disarm,
    expire_prefix,
    guarded_quit,
    prompt_quit,
)
from eawf.surfaces.tui.console.enter_keys import DRAFT_FIELDS, enter, refused, send_verb
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.header import CrumbRun
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.keymap import DISMISS, ENTRY_ALLOW, ENTRY_ROUTE, OVERLAY_KEYS, can
from eawf.surfaces.tui.console.mutation import adopt, card_key, menu_key, select
from eawf.surfaces.tui.console.navigation import (
    NAV_KEY,
    Ctx,
    busy,
    close_overlay,
    copied,
    focus_target,
    go,
    has_renderer,
    leave_overlay,
    open_overlay,
    recall,
    remember,
    return_focus,
)
from eawf.surfaces.tui.console.notices import notice_verb
from eawf.surfaces.tui.console.operations import (
    QUESTION_OPTIONS,
    AnswerRequest,
)
from eawf.surfaces.tui.console.overlays import help_card, is_overlay
from eawf.surfaces.tui.console.overlays.bound_keys import bound_key
from eawf.surfaces.tui.console.overlays.chassis import holds, unheld_keys
from eawf.surfaces.tui.console.overlays.palette import palette_hits
from eawf.surfaces.tui.console.overlays.pause import RUN as _PAUSE_RUN
from eawf.surfaces.tui.console.overlays.pause import TARGETS as _PAUSE_TARGETS
from eawf.surfaces.tui.console.overlays.question import ANSWERS
from eawf.surfaces.tui.console.registry import (
    DRILL_PREFIXES,
    OVERLAY_ARROWS,
    REGISTRY,
    route_for_id,
)
from eawf.surfaces.tui.console.renderers import (
    copy_for,
    seam_for,
)
from eawf.surfaces.tui.console.renderers import timeline as tl
from eawf.surfaces.tui.console.session import FocusTarget, Session
from eawf.surfaces.tui.console.tab_keys import TAB, owns_region_cycle, tab, tab_region
from eawf.surfaces.tui.console.tokens import Severity

REPLY_LIMIT = 2000
HOME = "scope.home"
# The key-log key a breadcrumb activation is recorded under: it is a pointer, not a key.
CRUMB_KEY = "click"
# The most steps a walk to scope home takes; the back stack is capped well below it.
_CRUMB_WALK = 64
_ABSENT_KEYS = re.compile(r"^(y|r|i|\.|Enter|ArrowUp|ArrowDown|Tab)$")
_PALETTE_TEXT = re.compile(r"[a-z0-9.\- ]", re.IGNORECASE)
# What a record frame must hold before a key can act on it.
_RECORD_NEEDS: Mapping[str, str] = MappingProxyType(
    {
        "y": "bundle",
        "r": "timeline",
        "Enter": "row",
        "ArrowUp": "row",
        "ArrowDown": "row",
        "Tab": "section",
    }
)
# What still acts while the action drawer is open, beside its verb letters: its toggle,
# Escape, and the modifiers the key count ignores.
_PANE_KEYS: frozenset[str] = frozenset({".", "Escape", *km.MODIFIERS})


def dispatch(ctx: Ctx, key: str, shift: bool = False) -> None:
    """Apply one keystroke to the session.

    Args:
        ctx: The keystroke's context.
        key: The key, named the way the dispatcher matches it (``ArrowDown``, ``.``).
        shift: Whether Shift was held, which only Tab distinguishes.
    """
    s = ctx.s
    expire_prefix(s, ctx.clock)
    opened_from = focus_target(s) if s.overlay is None else None
    if _frame_keys(ctx, key, shift):
        disarm(s, key)
    else:
        _console_keys(ctx, key, shift)
    adopt(ctx)
    _settle_focus(s, opened_from)


def _settle_focus(s: Session, opened_from: FocusTarget | None) -> None:
    """Record where an overlay opened from, and return focus there when it closes.

    Whatever closes it -- a dismissal, a refusal or a confirmed verb -- focus lands on
    the row and region that invoked it rather than at the top of the frame, even when
    the overlay borrowed the row cursor for its own list, as the palette does.

    Args:
        s: The session after the key.
        opened_from: Where focus stood before the key, when no overlay was open.
    """
    if s.overlay is not None:
        if opened_from is not None:
            s.focus_return = opened_from
    elif s.focus_return is not None:
        return_focus(s)


# ---------- the frame-level keys ----------


def _frame_keys(ctx: Ctx, key: str, shift: bool) -> bool:
    """Return whether an absent frame, a record frame, the route or a reply claimed the key."""
    s = ctx.s
    if key == "Tab" and shift and s.route != "settings" and not drill.is_native(ctx):
        return False
    k = km.ALIASES.get(key, key)
    if s.absent_frame and not s.overlay and _ABSENT_KEYS.match(k):
        ctx.log(k, "nothing is recorded here — Esc goes back")
        return True
    if s.record_facts is not None and not s.overlay and _record_key(ctx, k):
        return True
    if bound_key(ctx, k):
        return True
    # an overlay, a drawer or the armed prefix owns the keyboard, so the route beneath hears
    # nothing: a key never acts from the route's table through a surface above it
    above = s.overlay is not None or s.prefix is not None
    if not above and drill.claim(ctx, k, shift):
        return True
    hook = None if ctx.unheld or above else seam_for(s.route)
    if hook is not None and hook(ctx, k, shift):
        return True
    if s.overlay == "question" and not s.reply and k == "w":
        s.reply = {"text": ""}
        ctx.log("w", "reply field open · ≤ 2,000 characters · Enter sends")
        return True
    return _reply_key(ctx, k) if s.reply is not None else False


def _record_key(ctx: Ctx, k: str) -> bool:
    """Walk or open a record frame's rows, or refuse a key the record cannot serve."""
    s = ctx.s
    facts = " ".join(s.record_facts or [])
    nav = s.record_nav or []
    if nav and k in ("ArrowUp", "ArrowDown"):
        s.sel = (s.sel + (1 if k == "ArrowDown" else len(nav) - 1)) % len(nav)
        ctx.log(k, str(nav[s.sel]))
        return True
    if nav and k == "Enter":
        to = nav[s.sel]
        go(ctx, route_for_id(to) or "milestone", f"record · {to}", to)
        return True
    if k == "Tab" and owns_region_cycle(s):
        # a route that declares regions tabs between them whatever its record holds
        return False
    need = _RECORD_NEEDS.get(k)
    if need is not None and need not in facts:
        ctx.log(k, f"no {need} is recorded for this entity")
        return True
    return False


def _reply_key(ctx: Ctx, k: str) -> bool:
    """Type into the question overlay's reply field, send it on Enter, drop it on Escape."""
    s = ctx.s
    reply = s.reply
    if reply is None:
        return False
    if k == "Escape":
        s.reply = None
        ctx.log("Esc", "reply discarded · the question is still open")
    elif k == "Enter":
        ctx.log("Enter", f"reply sent verbatim · immutable · {len(reply['text'])} chars")
        s.reply = None
    elif k == "Backspace":
        reply["text"] = reply["text"][:-1]
    elif len(k) == 1 and len(reply["text"]) < REPLY_LIMIT:
        reply["text"] += k
    return True


# ---------- the console keys ----------


def _console_keys(ctx: Ctx, k: str, shift: bool) -> None:
    s = ctx.s
    if k == "Tab" and shift:
        handler = None if busy(s) else TAB.get(s.route)
        if handler is not None:
            handler(ctx, back=True)
        elif not (owns_region_cycle(s) and tab_region(ctx, back=True)):
            drill.no_cycle(ctx, back=True)
        return
    s.keys += 1
    if k != "Escape" or not _quit_press(s):
        # an Escape that closes or goes back is not a quit press: the prompt promised the
        # next press at a quiet scope home would quit, and a back-step is not that press
        disarm(s, k)
    owner_key = _OVERLAY_OWNS.get(s.overlay or "")
    if owner_key is not None and owner_key(ctx, k):
        return
    if _console_claims(ctx, k):
        return
    owner = _keyboard_owner(s)
    if owner is not None:
        owner(ctx, k)
        return
    if s.route == ENTRY_ROUTE and not s.overlay and _entry_key(ctx, k):
        return
    if _surface_claims(ctx, k):
        return
    _route_independent(ctx, k, pane=s.overlay == "actions")


def _quit_press(s: Session) -> bool:
    """Return whether an Escape now is a press of the guarded quit: at a quiet scope home."""
    return s.route == HOME and not s.back and not busy(s) and s.reply is None


def _console_claims(ctx: Ctx, k: str) -> bool:
    """Return whether the rack clear or an overlay holding nothing claimed the key."""
    s = ctx.s
    if k == DISMISS and not s.typing:
        # the rack is the console's, so clearing it passes every overlay and drawer gate
        _dismiss(ctx, k, False)
        return True
    overlay = s.overlay
    return (
        overlay is not None
        and is_overlay(overlay)
        and not holds(overlay, s, ctx.fixture)
        and _unheld_overlay_key(ctx, overlay, k)
    )


def _surface_claims(ctx: Ctx, k: str) -> bool:
    """Return whether the open overlay or drawer claimed the key, refusing one it does not bind.

    With nothing open, the refusal gate is the route's key table and the global grammar.
    """
    s = ctx.s
    bound = OVERLAY_KEYS.get(s.overlay) if s.overlay else km.allowlist(s, ctx.fixture)
    if bound is not None and k not in bound:
        ctx.noop(k)
        return True
    if s.overlay != "actions":
        return False
    if len(k) == 1 and _menu_key(ctx, k):
        return True
    if k not in _PANE_KEYS:
        # the drawer owns the keyboard: only the keys on its own footer act
        ctx.noop(k)
        return True
    return False


def _unheld_overlay_key(ctx: Ctx, overlay: str, k: str) -> bool:
    """Claim a key on an overlay that holds nothing it draws, or pass on one it still binds.

    Escape closes it. A key its held keybar still names, such as the consequence card's
    confirm, which answers the row the link holds, passes on; every other key has no row
    to act on and is refused.

    Returns:
        Whether the key was claimed here.
    """
    if k == "Escape":
        closed = close_overlay(ctx.s)
        ctx.log("Esc", f"close {closed} · nothing was held, nothing was written")
        return True
    if k in unheld_keys(overlay):
        return False
    ctx.noop(k)
    return True


def _keyboard_owner(s: Session) -> Callable[[Ctx, str], None] | None:
    """Return the handler of whatever holds the whole keyboard, if anything does."""
    if s.typing:
        return _filter_key
    if s.overlay == "question":
        return _question_key
    if s.overlay == "pause":
        return _pause_key
    if s.prefix == "g":
        return _prefix_key
    if s.overlay in ("inspect", "raw"):
        return _drawer_key
    return card_key if s.overlay == "consequence" and s.mutation is not None else None


def _palette_key(ctx: Ctx, k: str) -> bool:
    """Move, type into or open from the palette; Escape and the rest fall through."""
    s = ctx.s
    if k in ("ArrowDown", "ArrowUp"):
        s.sel = s.sel + 1 if k == "ArrowDown" else max(0, s.sel - 1)
        ctx.log(k, "palette row")
        return True
    if (len(k) == 1 and _PALETTE_TEXT.match(k)) or k == "Backspace":
        s.pq = s.pq[:-1] if k == "Backspace" else s.pq + k.lower()
        s.sel = 0
        s.pscroll = 0
        ctx.log("Backspace" if k == "Backspace" else k, "palette search")
        return True
    if k != "Enter":
        return False
    found = palette_hits(s.pq, ctx.fixture, ctx.rows)
    hit = found[min(s.sel, len(found) - 1)] if found else None
    if hit is None or not has_renderer(hit.route):
        close_overlay(s)
        s.pq = ""
        ctx.log("Enter", "nothing matched — nowhere to go")
        return True
    s.back.clear()
    dv.fresh_arrival(s, hit.route)
    s.route = hit.route
    s.sel = 0
    s.sel_id = None
    s.region = None
    # a palette pick is a jump: it arrives fresh, not back on the row it was opened from
    s.focus_return = None
    leave_overlay(s)
    s.pq = ""
    s.pscroll = 0
    s.subj_id = hit.subject
    ctx.log("Enter", f"palette → {s.route}" + (f" · {hit.subject}" if hit.subject else ""))
    return True


# The overlays that claim some keys before the global grammar hears them: the palette its
# text and rows, help its paging.
_OVERLAY_OWNS: Mapping[str, Callable[[Ctx, str], bool]] = MappingProxyType(
    {"palette": _palette_key, "help": help_card.scroll_key}
)


def _filter_key(ctx: Ctx, k: str) -> None:
    """Type into the route's filter field; the arrows still move the row."""
    s = ctx.s
    if k in ("Escape", "Enter"):
        s.typing = False
        if k == "Escape":
            dv.set_filter(s, "")
            s.sel = 0
            s.scroll = 0
            ctx.log("Esc", "filter cleared")
        else:
            ctx.log("Enter", f"filter kept · {dv.filter_of(s) or 'none'}")
    elif k in ("ArrowDown", "ArrowUp"):
        s.sel = s.sel + 1 if k == "ArrowDown" else max(0, s.sel - 1)
        ctx.log("↓" if k == "ArrowDown" else "↑", "row · filter still open")
    elif k == "Backspace" or len(k) == 1:
        text = dv.filter_of(s)
        dv.set_filter(s, text[:-1] if k == "Backspace" else text + k)
        s.sel = 0
        s.scroll = 0
        if k == "Backspace":
            ctx.log("Backspace", f"filter {dv.filter_of(s) or 'empty'}")
        else:
            ctx.log(k, f"filter \\{dv.filter_of(s)}")
    else:
        ctx.log(k, "ignored while the filter field is open")


def _question_key(ctx: Ctx, k: str) -> None:
    """Answer, decline or leave the question overlay."""
    s, fx = ctx.s, ctx.fixture
    q = att.question_row(s, fx)
    if k in ("1", "2", "3"):
        if att.gated(
            s, fx, key=k, verb="answering", row=q, principal_refusal=ctx.principal_refusal
        ):
            return
        leave_overlay(s)
        choice = int(k) - 1
        answer = AnswerRequest(target=q.id, option_id=QUESTION_OPTIONS[choice])
        send_verb(ctx, k, answer, f"answer · {ANSWERS[choice]} ·")
    elif k == "x":
        if att.gated(
            s, fx, key="x", verb="decline", row=q, principal_refusal=ctx.principal_refusal
        ):
            return
        s.verb = "x"
        s.sel_id = q.id
        open_overlay(s, "consequence", subject=q.id)
        ctx.log("x", f"decline {q.id} → consequence preview")
    elif k == "Escape":
        close_overlay(s)
        ctx.log("Esc", f"back — {q.id} stays open")
    else:
        ctx.noop(k)


def _pause_key(ctx: Ctx, k: str) -> None:
    """Preview a reconcile or a cancel from the pause overlay, or leave it."""
    s, fx = ctx.s, ctx.fixture
    target = _PAUSE_TARGETS.get(k)
    if target is not None:
        if att.gated(
            s, fx, key=k, verb=target["verb"], row=None, principal_refusal=ctx.principal_refusal
        ):
            return
        s.c_target = dict(target)
        open_overlay(s, "consequence", subject=_PAUSE_RUN)
        ctx.log(k, f"{target['verb']} {_PAUSE_RUN} → consequence preview")
    elif k == "Escape":
        close_overlay(s)
        ctx.log("Esc", "back — the pause stays unknown")
    else:
        ctx.noop(k)


def _prefix_key(ctx: Ctx, k: str) -> None:
    """Resolve the armed go prefix: a destination letter, a cancel, or a dropped prefix."""
    s = ctx.s
    deadline = s.prefix_deadline
    s.prefix = None
    s.prefix_deadline = None
    dest = REGISTRY.go_map.get(k)
    if dest and has_renderer(dest):
        if dest == s.route and not s.subj_id:
            ctx.log(f"g {k}", f"already here · {s.route}")
            return
        dv.fresh_arrival(s, dest)
        s.route = dest
        s.sel = 0
        s.sel_id = None
        s.region = None
        s.subj_id = None
        s.back.clear()
        ctx.log(f"g {k}", f"→ {s.route} · top level, the path starts here")
    elif k == "Escape":
        s.prefix_cancels += 1
        ctx.log("Esc", "prefix cancelled")
    elif k == km.HELP_KEY:  # the prefix state's help is the destination drawer it prints
        s.prefix, s.prefix_deadline = "g", deadline
        ctx.log("?", "help · the drawer below is the keymap while g is armed")
    else:
        ctx.log(f"g {k}", "no such destination — prefix dropped")


def _drawer_key(ctx: Ctx, k: str) -> None:
    """Close the inspect or raw drawer, or copy from it."""
    s = ctx.s
    if k == "Escape":
        close_overlay(s)
        ctx.log("Esc", "close pane")
    elif k == "y":
        text = copy_for(ctx)
        ctx.log("y", f"{copied(ctx.copy(text))} — {text}")
    elif k in km.MODIFIERS:
        s.keys -= 1
    else:
        ctx.noop(k)


def _entry_key(ctx: Ctx, k: str) -> bool:
    """Leave the entry layer on Escape, or swallow a key the entry state does not bind."""
    s = ctx.s
    states = ctx.fixture.proto.entry
    state = states[s.entry_sel] if s.entry_sel < len(states) else states[0]
    if k == "Escape":
        if state.exit == "terminal":
            ctx.log("Esc", "exit 4 · session ended · nothing was read, nothing was changed")
            if not ctx.fixture.prototype:
                # A console the attach path opened ends here; the launcher hands the
                # state's commands over once it has, so the exit is not a dead end.
                ctx.host.quit()
        elif not ctx.fixture.prototype and state.exit == "cancel":
            ctx.log("Esc", "cancelled · not attached · nothing was changed")
            ctx.host.quit()
        elif state.exit == "session":
            s.route = HOME
            s.conn = "OFFLINE SNAPSHOT"
            s.sel = 0
            s.sel_id = None
            ctx.log("Esc", "attached read-only · scope home under OFFLINE SNAPSHOT")
        else:
            s.entry_sel = 1
            s.path_sel = 0
            ctx.log("Esc", "cancelled · not attached")
        return True
    if k not in {key for key, _label in state.keys} | set(ENTRY_ALLOW):
        ctx.noop(k)
        return True
    return k == "s" and state.id == ONBOARDING and skip_step(ctx, len(state.rows or ()))


def _menu_key(ctx: Ctx, k: str) -> bool:
    """Run the action-menu verb bound to letter ``k``; ``False`` when no verb has that letter."""
    s, fx = ctx.s, ctx.fixture
    verb = drill.offered_verb(ctx, k)
    if menu_key(ctx, k):
        return True
    if verb is None:
        return False
    if att.is_light(verb):
        fire_light(ctx, verb, k)
        return True
    check = att.verb_available(s, fx, verb, principal_refusal=ctx.principal_refusal)
    if not check.ok:
        ctx.log(k, f"refused: {check.why}")
        return True
    if s.route == att.ATTENTION_ROUTE and k in att.VERB:
        s.verb = k
        s.c_target = None
    else:
        s.c_target = {
            "verb": verb.verb,
            "state": None,
            "id": dv.target_id(s, fx),
            "kind": s.route,
            "effects": verb.effects,
            "not": verb.non_effects,
        }
    open_overlay(s, "consequence", subject=dv.target_id(s, fx))
    ctx.log(k, f"{verb.verb} → consequence preview")
    return True


def fire_light(ctx: Ctx, verb: MenuVerb, k: str) -> None:
    """Run a light verb: it opens its route or fills the clipboard, answering in the rack."""
    s, fx = ctx.s, ctx.fixture
    check = att.verb_available(s, fx, verb)
    if not check.ok:
        ctx.notify(check.why, "refused", Severity.ERR)
        ctx.log(k, f"refused: {check.why}")
        return
    leave_overlay(s)
    s.pane_sel = 0
    to = verb.target
    if not to:
        text = copy_for(ctx)
        ctx.log(k, f"{verb.verb} · {copied(ctx.copy(text))} · {text}")
        return
    if not has_renderer(to):
        ctx.notify(f"{to} is designed but not bound here", "unavailable", Severity.ERR)
        ctx.log(k, f"{verb.verb} → {to} is not bound here")
        return
    subject, opened = light_opening(verb, s.subj_id, prototype=fx.prototype)
    s.back.record(remember(s))
    s.region = None
    s.route = to
    s.sel = 0
    s.sel_id = None
    s.subj_id = subject
    ctx.notify(opened, "opened")
    ctx.log(k, f"{verb.verb} → {to}")


# ---------- the route-independent keys ----------


def _move(ctx: Ctx, k: str, pane: bool) -> None:
    """Move the cursor for an arrow key, ``j`` or ``k``."""
    s = ctx.s
    down = k in ("ArrowDown", "j")
    if pane and k in ("j", "k"):
        ctx.noop(k)
        return
    if k in ("ArrowDown", "ArrowUp") and _move_in_region(ctx, k, down):
        return
    if pane or (s.overlay and s.overlay not in OVERLAY_ARROWS):
        ctx.noop(k)
        return
    if s.route == ENTRY_ROUTE:
        _move_entry(ctx, k, down)
        return
    s.sel = s.sel + 1 if down else max(0, s.sel - 1)
    s.sel_id = None
    ctx.log(k, "down" if down else "up")


def _move_in_region(ctx: Ctx, k: str, down: bool) -> bool:
    """Move the draft field, a roadmap region row or a readiness row, if one owns the arrows."""
    s = ctx.s
    step = 1 if down else -1
    if s.overlay == "draft":
        s.draft_field = max(0, min(2, s.draft_field + step))
        ctx.log(k, DRAFT_FIELDS[s.draft_field])
        return True
    if s.route == "timeline" and (s.tl_reg or tl.LANES) != tl.LANES:
        rows = (s.tl_regs or {}).get(s.tl_reg, [])
        s.tl_sel = min(len(rows) - 1, s.tl_sel + 1) if down else max(0, s.tl_sel - 1)
        ctx.log(k, rows[s.tl_sel][0] if 0 <= s.tl_sel < len(rows) else "")
        return True
    if s.route == "release" and s.rel_reg == "READINESS":
        s.rel_sel = min(3, s.rel_sel + 1) if down else max(0, s.rel_sel - 1)
        ctx.log(k, f"readiness row {s.rel_sel + 1}")
        return True
    return False


def _move_entry(ctx: Ctx, k: str, down: bool) -> None:
    s = ctx.s
    states = ctx.fixture.proto.entry
    state = states[s.entry_sel] if s.entry_sel < len(states) else states[0]
    if not down:
        s.path_sel = max(0, s.path_sel - 1)
        ctx.log(k, "row")
        return
    rows = len(state.paths) or len(state.rows or ())
    if rows:
        s.path_sel = min(rows - 1, s.path_sel + 1)
        ctx.log(k, "row")
    else:
        ctx.log(k, "this state has no rows")


def _escape(ctx: Ctx, k: str, pane: bool) -> None:
    """Close an overlay, clear a bucket, guard the quit at home, or go back one step."""
    s, fx = ctx.s, ctx.fixture
    if s.overlay:
        closed = close_overlay(s)
        s.pq = ""
        if closed == "consequence":
            target = dv.target_id(s, fx)
            ctx.log("Esc", f"cancelled — {target} unchanged, nothing was written")
        else:
            ctx.log("Esc", "close overlay")
        return
    if s.route in ("activity", "attention") and s.bucket:
        s.bucket = None
        s.sel = 0
        s.scroll = 0
        ctx.log("Esc", "bucket cleared — every bucket shows")
        return
    if s.route == HOME and not s.back:
        if guarded_quit(s, ctx.clock, outstanding=ctx.outstanding, at=ctx.pressed_at):
            ctx.host.quit()
        return
    if not _step_back(ctx):
        ctx.log("Esc", "at scope home · press again within 1.5s to quit")
        s.last_esc = ctx.clock.now()
        prompt_quit(s, ctx.clock)


def _step_back(ctx: Ctx) -> bool:
    """Walk the back stack one step, or climb one breadcrumb step; ``False`` at the top."""
    s = ctx.s
    entry = s.back.pop()
    if entry is not None:
        recall(s, entry)
        subject = f" · {entry.subj}" if entry.subj else ""
        ctx.log("Esc", f"back → {entry.route}{subject} · selection restored")
        return True
    up = drill.parent(ctx) if drill.is_native(ctx) else parent_of(s, ctx.fixture)
    if up is None:
        return False
    s.route, s.subj_id = up
    s.sel = 0
    s.sel_id = None
    s.region = None
    subject = f" · {up[1]}" if up[1] else ""
    ctx.log("Esc", f"up → {up[0]}{subject} · one step up the breadcrumb")
    return True


def _up(ctx: Ctx, k: str, pane: bool) -> None:
    """Climb the containment chain one step, whatever the history says.

    Escape is history first; ``u`` never is, so from a Run reached through Activity it
    opens the Task the Run is an attempt at, and the place it left is pushed like any
    other navigation.
    """
    s = ctx.s
    if pane or s.overlay:
        ctx.noop(k)
        return
    up = drill.parent(ctx) if drill.is_native(ctx) else parent_of(s, ctx.fixture)
    if up is None:
        ctx.log("u", "at the top of the containment chain · nothing above")
        return
    go(ctx, up[0], "u · containment parent", up[1])


def _sibling(ctx: Ctx, k: str, pane: bool) -> None:
    """Open the previous or next sibling of the subject, at the same depth.

    The siblings are the records that name the subject's own containment parent, in id
    order, and the walk wraps. A lateral step is not a drill, so it pushes nothing: Escape
    still returns to wherever the walk started from.
    """
    s = ctx.s
    if pane or s.overlay:
        ctx.noop(k)
        return
    peers = drill.siblings(ctx) if drill.is_native(ctx) else siblings_of(s, ctx.fixture)
    if len(peers) < 2 or s.subj_id not in peers:
        ctx.log(k, "no sibling at this depth")
        return
    step = 1 if k == "]" else -1
    s.subj_id = peers[(peers.index(s.subj_id) + step) % len(peers)]
    s.sel = 0
    s.sel_id = None
    s.region = None
    ctx.log(k, f"sibling → {s.subj_id}")


def siblings_of(session: Session, fixture: Fixture) -> list[str]:
    """Return the subject and its siblings under one containment parent, in id order.

    A route whose Escape parent reads its subject ``via`` a record field has siblings:
    every record sharing the subject's id prefix whose own field resolves to the same
    parent, by the same rule the climb reads it with.

    Returns:
        The sibling ids, the subject among them; empty when the route has no such parent,
        the subject has no record, or its record names no parent.
    """
    subject = session.subj_id
    escape = REGISTRY.escapes.get(session.route)
    if subject is None or escape is None or escape.via is None:
        return []
    via = escape.via
    parent = _record_subject(fixture, subject, escape.route, via)
    prefixes = sorted(DRILL_PREFIXES.get(session.route, ()), key=len, reverse=True)
    prefix = next((p for p in prefixes if subject.startswith(p)), None)
    if parent is None or prefix is None:
        return []
    return sorted(
        x
        for x in fixture.detail
        if x.startswith(prefix) and _record_subject(fixture, x, escape.route, via) == parent
    )


def activate_crumb(ctx: Ctx, step: CrumbRun) -> None:
    """Walk the breadcrumb up to ``step``, as that many Escapes would, restoring each place.

    The breadcrumb is the path Escape walks, so a step is reached by walking it: while
    history exists each step names a back-stack entry and the walk stops on it; without
    history each step is one climb up the containment chain. The scope step walks to scope
    home. A step that is not a link -- the brand, a fold, the leaf -- does nothing, because
    going where the operator already is is not a step; nor does any step while an overlay
    owns the frame, whose crumb names the overlay rather than the path.

    Args:
        ctx: The activation's context.
        step: The crumb run that was activated.
    """
    s = ctx.s
    if not step.link or s.overlay:
        return
    ctx.log(CRUMB_KEY, f"crumb → {step.text}")
    if step.text == ctx.fixture.proto.scope:
        for _ in range(_CRUMB_WALK):
            if s.route == HOME or not _step_back(ctx):
                return
        return
    if s.back:
        for _ in range(len(s.back)):
            if not _step_back(ctx) or REGISTRY.step_leaf(s.route, s.subj_id) == step.text:
                return
        return
    for _ in range(step.back):
        if not _step_back(ctx):
            return


def parent_of(session: Session, fixture: Fixture) -> tuple[str, str | None] | None:
    """Return the place one breadcrumb step up, for an Escape with an empty back stack.

    The route registry's Escape parent decides it. A parent read ``via`` a record field
    takes its subject from the route subject's own record -- a task climbs to its batch, a
    batch to its milestone, a milestone to its track, a Run to its task -- and a record
    that names no such parent climbs to scope home instead. The root has no parent.
    """
    route, subject = session.route, session.subj_id
    escape = REGISTRY.escapes.get(route)
    if escape is None:
        return None
    if escape.via is None:
        # a fixed parent subject is a prototype record; a live tree climbs to the route alone
        return (escape.route, escape.subject if fixture.prototype else None)
    climb = _record_subject(fixture, subject, escape.route, escape.via) if subject else None
    return (escape.route, climb) if climb is not None else (HOME, None)


def _record_subject(fixture: Fixture, subject: str, route: str, via: str) -> str | None:
    """Return the id the ``via`` field of ``subject``'s record names for ``route``, if any.

    The id is the first token carrying one of the prefixes a drill resolves to ``route``.
    A track is filed under its register name rather than a prefixed id, so a field naming
    a track the register holds resolves too. A receipt has no record of its own: it is a
    row of the receipt register, which states the field under the same name.
    """
    receipt = fixture.registers.receipts.get(subject, {})
    value = dv.field_of(fixture, subject, via) or str(receipt.get(via.lower(), ""))
    prefixes = sorted(DRILL_PREFIXES.get(route, ()), key=len, reverse=True)
    if prefixes:
        alternatives = "|".join(re.escape(prefix) for prefix in prefixes)
        found = re.search(rf"(?<![\w-])(?:{alternatives})[\w-]*", value)
        if found:
            return found.group(0)
    return dv.track_id_of(fixture, value) if route == "track" else None


def _page(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    s.sel = s.sel + s.visible if k == "PageDown" else max(0, s.sel - s.visible)
    s.sel_id = None
    ctx.log(k, "page down" if k == "PageDown" else "page up")


def _ends(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if pane:
        ctx.noop(k)
        return
    s.sel = 0 if k == "Home" else 1_000_000
    s.sel_id = None
    ctx.log(k, "first row" if k == "Home" else "last row")


def _readiness(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.route != "release":
        ctx.noop(k)
        return
    open_overlay(s, "readiness", subject=s.subj_id)
    s.sel = 0
    ctx.log("m", "readiness matrix · every signal with its evidence")


def _actions(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay != "actions" and not ctx.fixture.menus.verbs(s.route):
        ctx.notify("No action is available here.", "NO ACTIONS", Severity.WARN)
        ctx.log(".", "no verb for this route — nothing to open")
        return
    if s.overlay == "actions":
        close_overlay(s)
    else:
        open_overlay(s, "actions")
    s.c_target = None
    ctx.log(".", "action menu")


def _drawer(name: str, note: str) -> Callable[[Ctx, str, bool], None]:
    entry = KEY[name]

    def toggle(ctx: Ctx, k: str, pane: bool) -> None:
        s = ctx.s
        if s.overlay == "consequence":
            ctx.log(k, "not offered on a consequence card — Enter confirms, Esc cancels")
        elif not can(s, ctx.fixture, entry):
            ctx.noop(k)
        elif s.overlay == name:
            close_overlay(s)
            ctx.log(k, note)
        else:
            open_overlay(s, name)
            ctx.log(k, note)

    return toggle


def _promote(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay != "draft":
        ctx.noop(k)
        return
    if refused(ctx, k, "promote"):
        return
    missing = s.draft_miss or []
    if missing:
        needs = _list_of(missing)
        ctx.notify(f"It still needs {needs}.", "NOT PROMOTED", Severity.WARN)
        ctx.log("p", f"refused · {needs} unanswered")
    else:
        text = "Promoted into BAT-0002 · the batch decides when it runs."
        ctx.notify(text, "PROMOTED", Severity.OK)
        ctx.log("p", "promoted · nothing was dispatched")


def _defer(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay == "draft":
        if refused(ctx, k, "defer"):
            return
        text = "Deferred · it keeps its due scope and leaves the drafts list."
        ctx.notify(text, "DEFERRED")
        ctx.log("x", "deferred · the draft is not discarded")
    elif s.route == att.ATTENTION_ROUTE:
        _attention_verb(ctx, k, pane)
    else:
        ctx.noop(k)


def _copy(ctx: Ctx, k: str, pane: bool) -> None:
    text = copy_for(ctx)
    ctx.log("y", f"{copied(ctx.copy(text))} — {text}")


def _copy_urn(ctx: Ctx, k: str, pane: bool) -> None:
    urn = dv.urn(ctx.s, ctx.fixture, rows=ctx.rows, scope=ctx.scope)
    ctx.log("Y", f"{copied(ctx.copy(urn, 'copied URN'))} URN — {urn}")


def _dismiss(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    n = len(s.toasts)
    if not n:
        ctx.noop(k)
        return
    s.toasts = []
    ctx.log("-", f"{dv.plural(n, 'notification')} dismissed")


def _help(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay == "help":
        close_overlay(s)
    else:
        open_overlay(s, "help", subject=s.route)
        s.help_top = 0
    ctx.log("?", "route help")


def _palette(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    open_overlay(s, "palette")
    s.pq = ""
    s.sel = 0
    s.pscroll = 0
    ctx.log("/", "command palette")


def _filter(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if not can(s, ctx.fixture, KEY["filter"]):
        ctx.noop("\\")
        return
    s.typing = True
    dv.set_filter(s, "")
    s.sel = 0
    s.scroll = 0
    ctx.log("\\", "filter — type to narrow, Esc clears")


def _arm(ctx: Ctx, k: str, pane: bool) -> None:
    arm_prefix(ctx.s, ctx.clock)
    ctx.log("g", "prefix armed")


def _marker(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    marks = s.timeline_marks
    if s.route != "timeline" or not marks:
        ctx.noop(k)
        return
    right = k == "ArrowRight"
    s.mark = (s.mark + (1 if right else marks - 1)) % marks
    ctx.log("→" if right else "←", f"marker → {s.mark + 1} of {marks} on this lane")


def _attention_verb(ctx: Ctx, k: str, pane: bool) -> None:
    s, fx = ctx.s, ctx.fixture
    if s.route != att.ATTENTION_ROUTE or att.held_refusal(ctx, k) or notice_verb(ctx, k):
        return
    rows = att.attn_list(s, fx)
    row = rows[s.sel] if s.sel < len(rows) else None
    name = att.VERB[k].name
    card = (att.answer_card(row) if k == "a" else "") or "consequence"
    refused = card == "consequence" and att.gated(
        s, fx, key=k, verb=name, row=row, principal_refusal=ctx.principal_refusal
    )
    if refused:
        return
    s.verb = k
    s.c_target = None
    open_overlay(s, card, subject=row.id if row is not None else None)
    ctx.log(k, f"{name} → {card} preview first")


def _modifier(ctx: Ctx, k: str, pane: bool) -> None:
    ctx.s.keys -= 1


def _noop(ctx: Ctx, k: str, pane: bool) -> None:
    ctx.noop(k)


def _list_of(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_KEYS: Mapping[str, Callable[[Ctx, str, bool], None]] = MappingProxyType(
    {
        "PageDown": _page,
        "PageUp": _page,
        **dict.fromkeys((" ", ","), select),
        "Home": _ends,
        "End": _ends,
        "ArrowDown": _move,
        "ArrowUp": _move,
        "j": _move,
        "k": _move,
        "Enter": enter,
        "Escape": _escape,
        "m": _readiness,
        ".": _actions,
        "i": _drawer("inspect", "inspect the focused field"),
        "r": _drawer("raw", "bounded raw segment"),
        "p": _promote,
        "x": _defer,
        "y": _copy,
        "Y": _copy_urn,
        "-": _dismiss,
        "?": _help,
        km.ATTENTION_JUMP_KEY: att.top_attention,
        "/": _palette,
        "\\": _filter,
        "ctrl+f": _filter,
        "g": _arm,
        "Tab": tab,
        "u": _up,
        "[": _sibling,
        "]": _sibling,
        "ArrowLeft": _marker,
        "ArrowRight": _marker,
        "a": _attention_verb,
        "z": _attention_verb,
        "v": _attention_verb,
        **dict.fromkeys(km.MODIFIERS, _modifier),
    }
)


def _route_independent(ctx: Ctx, k: str, *, pane: bool) -> None:
    """Apply a key every route shares; an unbound key is recorded as unclaimed."""
    _KEYS.get(k, _noop)(ctx, k, pane)


__all__ = ["NAV_KEY", "dispatch", "fire_light", "parent_of", "siblings_of"]
