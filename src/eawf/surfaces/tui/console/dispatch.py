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
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.action_menu import MenuVerb
from eawf.surfaces.tui.console.clock import QuitStep, arm_prefix, expire_prefix, quit_step
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.keymap import ENTRY_ALLOW, ENTRY_ROUTE, OVERLAY_KEYS, can
from eawf.surfaces.tui.console.navigation import NAV_KEY, Ctx, go, has_renderer
from eawf.surfaces.tui.console.overlays.palette import palette_hits
from eawf.surfaces.tui.console.overlays.question import ANSWERS
from eawf.surfaces.tui.console.overlays.states import OV_MODEL, step_state
from eawf.surfaces.tui.console.registry import OVERLAY_ARROWS, REGISTRY, SECTIONS, route_for_id
from eawf.surfaces.tui.console.renderers import copy_target, cost_ceiling, seam_for, track
from eawf.surfaces.tui.console.renderers import timeline as tl
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import Severity

# The time a confirmed control is stamped with while the console clock is the fixture's.
CONFIRM_STAMP = "14:02:41"
REPLY_LIMIT = 2000
HOME = "scope.home"
_MODIFIERS = frozenset({"Shift", "Control", "Alt", "Meta", "CapsLock"})
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
_ALIASES: Mapping[str, str] = MappingProxyType({"j": "ArrowDown", "k": "ArrowUp"})
_DRAFT_FIELDS: tuple[str, ...] = ("criteria", "owner", "batch")
_PAUSE_RUN = pt.PAUSED_RUN
_PAUSE_TARGETS: Mapping[str, dict[str, str]] = MappingProxyType(
    {
        "n": {
            "verb": "reconcile",
            "id": _PAUSE_RUN,
            "kind": "run",
            "effects": "the daemon is asked again for the outcome of the pause",
            "not": "it does not restart the run and does not fail the task",
        },
        "c": {
            "verb": "cancel",
            "id": _PAUSE_RUN,
            "kind": "run",
            "effects": "cancel is requested; it stays unknown until the run answers",
            "not": "it does not fail the task and does not undo any merged work",
        },
    }
)


def dispatch(ctx: Ctx, key: str, shift: bool = False) -> None:
    """Apply one keystroke to the session.

    Args:
        ctx: The keystroke's context.
        key: The key, named the way the dispatcher matches it (``ArrowDown``, ``.``).
        shift: Whether Shift was held, which only Tab distinguishes.
    """
    expire_prefix(ctx.s, ctx.clock)
    if _frame_keys(ctx, key, shift):
        ctx.s.disarm_quit()
        return
    _console_keys(ctx, key, shift)
    _step_overlay_state(ctx, key)


# ---------- the frame-level keys ----------


def _frame_keys(ctx: Ctx, key: str, shift: bool) -> bool:
    """Return whether an absent frame, a record frame, the route or a reply claimed the key."""
    s = ctx.s
    if key == "Tab" and shift and s.route != "settings":
        return False
    k = _ALIASES.get(key, key)
    if s.absent_frame and not s.overlay and _ABSENT_KEYS.match(k):
        ctx.log(k, "nothing is recorded here — Esc goes back")
        return True
    if s.record_facts is not None and not s.overlay and _record_key(ctx, k):
        return True
    hook = None if ctx.unheld else seam_for(s.route)
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
        ctx.log("S-Tab", "left the canvas — the page has the keyboard again")
        return
    s.keys += 1
    if k != "Escape":
        s.disarm_quit()
    if s.overlay == "palette" and _palette_key(ctx, k):
        return
    owner = _keyboard_owner(s)
    if owner is not None:
        owner(ctx, k)
        return
    if s.route == ENTRY_ROUTE and not s.overlay and _entry_key(ctx, k):
        return
    overlay = s.overlay
    bound = OVERLAY_KEYS[overlay] if overlay is not None and overlay in OVERLAY_KEYS else None
    if k != "-" and bound is not None and k not in bound:
        ctx.noop(k)
        return
    pane = s.overlay == "actions"
    if pane and len(k) == 1 and _menu_key(ctx, k):
        return
    _route_independent(ctx, k, pane=pane)


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
    return None


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
    found = palette_hits(s.pq, ctx.fixture)
    hit = found[min(s.sel, len(found) - 1)] if found else None
    if hit is None or not has_renderer(hit.route):
        s.overlay = None
        s.pq = ""
        ctx.log("Enter", "nothing matched — nowhere to go")
        return True
    s.back.clear()
    dv.fresh_arrival(s, hit.route)
    s.route = hit.route
    s.sel = 0
    s.sel_id = None
    s.overlay = None
    s.pq = ""
    s.pscroll = 0
    s.subj_id = hit.subject
    ctx.log("Enter", f"palette → {s.route}" + (f" · {hit.subject}" if hit.subject else ""))
    return True


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
        if att.gated(s, fx, key=k, verb="answering", row=q):
            return
        att.resolve_action(q, state=att.VERB["a"].state, stamp=CONFIRM_STAMP)
        s.overlay = None
        ctx.log(k, f"{q.id} answered · {ANSWERS[int(k) - 1]}")
    elif k == "x":
        if att.gated(s, fx, key="x", verb="decline", row=q):
            return
        s.verb = "x"
        s.sel_id = q.id
        s.overlay = "consequence"
        ctx.log("x", f"decline {q.id} → consequence preview")
    elif k == "Escape":
        s.overlay = None
        ctx.log("Esc", f"back — {q.id} stays open")
    else:
        ctx.noop(k)


def _pause_key(ctx: Ctx, k: str) -> None:
    """Preview a reconcile or a cancel from the pause overlay, or leave it."""
    s, fx = ctx.s, ctx.fixture
    target = _PAUSE_TARGETS.get(k)
    if target is not None:
        if att.gated(s, fx, key=k, verb=target["verb"], row=None):
            return
        s.c_target = dict(target)
        s.overlay = "consequence"
        ctx.log(k, f"{target['verb']} {_PAUSE_RUN} → consequence preview")
    elif k == "Escape":
        s.overlay = None
        ctx.log("Esc", "back — the pause stays unknown")
    else:
        ctx.noop(k)


def _prefix_key(ctx: Ctx, k: str) -> None:
    """Resolve the armed go prefix: a destination letter, a cancel, or a dropped prefix."""
    s = ctx.s
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
        s.subj_id = None
        s.back.clear()
        ctx.log(f"g {k}", f"→ {s.route} · top level, the path starts here")
    elif k == "Escape":
        s.prefix_cancels += 1
        ctx.log("Esc", "prefix cancelled")
    else:
        ctx.log(f"g {k}", "no such destination — prefix dropped")


def _drawer_key(ctx: Ctx, k: str) -> None:
    """Close the inspect or raw drawer, or copy from it."""
    s = ctx.s
    if k == "Escape":
        s.overlay = None
        ctx.log("Esc", "close pane")
    elif k == "y":
        copied = copy_target(s, ctx.fixture)
        ctx.notify(copied, "copied")
        ctx.log("y", f"copied — {copied}")
    elif k in _MODIFIERS:
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
    return False


def _menu_key(ctx: Ctx, k: str) -> bool:
    """Run the action-menu verb bound to letter ``k``; ``False`` when no verb has that letter."""
    s, fx = ctx.s, ctx.fixture
    verb = fx.menus.verb(s.route, k)
    if verb is None:
        return False
    if att.is_light(verb):
        fire_light(ctx, verb, k)
        return True
    check = att.verb_available(s, fx, verb)
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
    s.overlay = "consequence"
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
    s.overlay = None
    s.pane_sel = 0
    to = verb.target
    if not to:
        copied = copy_target(s, fx)
        ctx.notify(copied, "copied")
        ctx.log(k, f"{verb.verb} · {copied}")
        return
    if not has_renderer(to):
        ctx.notify(f"{to} is designed but not bound here", "unavailable", Severity.ERR)
        ctx.log(k, f"{verb.verb} → {to} is not bound here")
        return
    s.back.push(route=s.route, sel=s.sel, subj=s.subj_id)
    s.route = to
    s.sel = 0
    s.sel_id = None
    s.subj_id = None
    ctx.notify(REGISTRY.route_word(to), "opened")
    ctx.log(k, f"{verb.verb} → {to}")


def _step_overlay_state(ctx: Ctx, k: str) -> None:
    """Step a decision overlay through its state model on ``s``."""
    s = ctx.s
    overlay = s.overlay
    if k != "s" or overlay is None or overlay not in OV_MODEL:
        return
    word = step_state(s, overlay)
    if word is not None:
        ctx.log("s", f"{overlay} → {word}")


# ---------- the route-independent keys ----------


def _drill(ctx: Ctx, dest: str, entity_id: str | None) -> None:
    s = ctx.s
    s.back.push(route=s.route, sel=s.sel, subj=s.subj_id)
    s.route = dest
    s.sel = 0
    s.subj_id = entity_id or None
    s.sel_id = None
    ctx.log("Enter", f"drill → {dest}" + (f" · {entity_id}" if entity_id else ""))


def _nav_at_cursor(s: Session) -> str | None:
    nav = s.record_nav or []
    return nav[s.sel] if s.sel < len(nav) else None


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
        ctx.log(k, _DRAFT_FIELDS[s.draft_field])
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


def _confirm(ctx: Ctx) -> None:
    """Confirm the consequence card: a drawer verb is requested, an attention verb resolves."""
    s, fx = ctx.s, ctx.fixture
    s.overlay = None
    target = s.c_target
    if target:
        s.c_target = None
        ctx.log(
            "Enter",
            f"{target['verb']} requested on {target['id']} — accepted · the ledger is modelled "
            "for attention only here",
        )
        return
    rows = att.attn_list(s, fx) if s.route == att.ATTENTION_ROUTE else []
    action = rows[s.sel] if s.sel < len(rows) else fx.proto.attention[0]
    verb = att.VERB[s.verb or "a"]
    outcome = att.resolve_action(action, state=verb.state, stamp=CONFIRM_STAMP)
    if outcome is att.Resolution.SUPERSEDED:
        ctx.log(
            "Enter", f"{action.id} is already {action.state.lower()} · this answer is superseded"
        )
    else:
        ctx.log("Enter", f"{action.id} → {action.state} · ledger confirmed")


def _enter_overlay(ctx: Ctx) -> bool:
    """Act on Enter inside an overlay that owns it."""
    s = ctx.s
    if s.overlay == "draft":
        field = _DRAFT_FIELDS[s.draft_field]
        ctx.log("Enter", f"set {field} · the field the cursor is on")
        return True
    if s.overlay == "consequence":
        _confirm(ctx)
        return True
    if s.overlay in ("inspect", "raw"):
        s.overlay = None
        ctx.log("Enter", "pane closed — it does not survive a navigation")
        return True
    return False


def _enter_track(ctx: Ctx) -> None:
    s = ctx.s
    drills = track.group_of(s.track_group).drills
    entity_id, dest = drills[min(s.sel, len(drills) - 1)]
    s.back.push(route=s.route, sel=s.sel, subj=s.subj_id)
    s.route = dest
    s.sel = 0
    s.subj_id = entity_id
    s.sel_id = None
    ctx.log("Enter", f"drill → {dest} · {entity_id}")


def _enter_release(ctx: Ctx) -> None:
    s = ctx.s
    if s.rel_reg == "READINESS":
        s.overlay = "readiness"
        s.ov_state["readiness"] = s.rel_sel
        ctx.log("Enter", "readiness matrix · on the signal you were reading")
        return
    target = _nav_at_cursor(s)
    _drill(ctx, route_for_id(target) or "milestone", target)


def _enter_entry(ctx: Ctx) -> None:
    s = ctx.s
    state = ctx.fixture.proto.entry[s.entry_sel]
    first_cell = (
        (state.rows or (("",),))[s.path_sel][0] if state.id in ("ambiguous", "offline") else ""
    )
    notes = {
        "ambiguous": f"attach to {first_cell} · remembered for this session only",
        "failed": "copied: eawf workspace register .",
        "schema": "copied: eawf export --read-only",
        "offline": f"drill → {first_cell} · read-only, controls are gone",
    }
    if state.paths:
        command = state.paths[s.path_sel][0].replace(" ", "-")
        ctx.log("Enter", f"eawf migrate --{command} · shown, never auto-applied")
    else:
        ctx.log("Enter", notes.get(state.id, "nothing to run while attaching"))


def _enter_timeline(ctx: Ctx) -> None:
    s = ctx.s
    region = s.tl_reg or tl.LANES
    if region == tl.LANES:
        lane = tl.lane_name(s.sel)
        s.overlay = "marker"
        s.marker_card = {
            "id": s.timeline_marker or "MLS-0001",
            "lane": lane,
            "glyph": tl.marker_glyph(lane, s.mark),
        }
        ctx.log("Enter", f"marker detail · {s.marker_card['id']} on the {lane} lane")
        return
    rows = (s.tl_regs or {}).get(region, [])
    pick = rows[s.tl_sel] if s.tl_sel < len(rows) else None
    if not pick:
        ctx.log("Enter", f"nothing to open in {region}")
        return
    s.back.push(route=s.route, sel=s.sel, subj=s.subj_id)
    s.route = "milestone" if region == tl.UNDATED else "release"
    s.subj_id = pick[0]
    s.sel = 0
    if region == tl.UNDATED:
        ctx.log("Enter", f"→ {pick[0]} · {pick[1]}")
    else:
        ctx.log("Enter", f"→ {pick[0]} {pick[1]} · {str(pick[3]).lower()}")


def _enter_milestone(ctx: Ctx) -> None:
    s = ctx.s
    target = _nav_at_cursor(s)
    if not target:
        s.overlay = "evidence"
        ctx.log("Enter", "evidence viewer at this digest")
        return
    s.back.push(route=s.route, sel=s.sel, subj=s.subj_id)
    s.route = route_for_id(target) or "batch.detail"
    s.sel = 0
    s.subj_id = target
    s.sel_id = None
    ctx.log("Enter", f"drill → {s.route} · {target}")


def _enter_attention(ctx: Ctx) -> None:
    s = ctx.s
    rows = att.attn_list(s, ctx.fixture)
    row = rows[s.sel] if s.sel < len(rows) else (rows[0] if rows else None)
    row_id = row.id if row else ""
    if row is not None and "question" in row.kind:
        s.overlay = "question"
        ctx.log("Enter", f"question detail · {row_id}")
    elif row is not None and row.bucket == "lost":
        s.overlay = "pause"
        ctx.log("Enter", "pause detail · the outcome is unknown")
    else:
        s.overlay = "consequence"
        ctx.log("Enter", f"action detail · {row_id}")


def _enter_overlay_route(overlay: str, note: str) -> Callable[[Ctx], None]:
    def enter(ctx: Ctx) -> None:
        ctx.s.overlay = overlay
        ctx.log("Enter", note)

    return enter


def _enter_note(note: str) -> Callable[[Ctx], None]:
    def enter(ctx: Ctx) -> None:
        ctx.log("Enter", note)

    return enter


def _enter_nav(default: str) -> Callable[[Ctx], None]:
    def enter(ctx: Ctx) -> None:
        target = _nav_at_cursor(ctx.s)
        _drill(ctx, route_for_id(target) or default, target)

    return enter


def _enter_activity(ctx: Ctx) -> None:
    row = dv.current_fleet_row(ctx.s, ctx.fixture)
    _drill(ctx, "run.detail", row.run if row else None)


def _enter_cost_ceiling(ctx: Ctx) -> None:
    """Open the Run the cursor sits on in the stopped list, which the footer promises."""
    _drill(ctx, "run.detail", cost_ceiling.stopped_run(ctx.s))


_ENTER: Mapping[str, Callable[[Ctx], None]] = MappingProxyType(
    {
        HOME: _enter_note("nothing to drill"),
        "track": _enter_track,
        "task.detail": _enter_nav("run.detail"),
        "release": _enter_release,
        ENTRY_ROUTE: _enter_entry,
        "campaign": _enter_overlay_route(
            "evidence", "evidence viewer · the receipts behind this claim"
        ),
        "backlog": _enter_overlay_route("draft", "draft detail · what it still needs"),
        "timeline": _enter_timeline,
        "history": _enter_overlay_route(
            "resolution", "resolution card — what happened to this target"
        ),
        "settings": _enter_note("the WHY pane below already shows this chain"),
        "milestone": _enter_milestone,
        "activity": _enter_activity,
        "batch.detail": _enter_nav("task.detail"),
        "attention": _enter_attention,
        "cost.ceiling": _enter_cost_ceiling,
    }
)


def _enter(ctx: Ctx, k: str, pane: bool) -> None:
    if pane:
        ctx.noop("Enter")
        return
    if _enter_overlay(ctx):
        return
    handler = _ENTER.get(ctx.s.route)
    if handler is not None:
        handler(ctx)


def _escape(ctx: Ctx, k: str, pane: bool) -> None:
    """Close an overlay, clear a bucket, guard the quit at home, or go back one step."""
    s, fx = ctx.s, ctx.fixture
    if s.overlay:
        closed = s.overlay
        s.overlay = None
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
        _guarded_quit(ctx)
        return
    if not _step_back(ctx):
        ctx.log("Esc", "at scope home · press again within 1.5s to quit")
        s.last_esc = ctx.clock.now()


def _guarded_quit(ctx: Ctx) -> None:
    check = quit_step(ctx.s, ctx.clock)
    if check.step is QuitStep.QUIT:
        ctx.log("Esc Esc", f"quit — guarded: scope home, nothing open, {check.gap_ms}ms apart")
        ctx.host.quit()
    elif check.step is QuitStep.BURST:
        ctx.log("Esc", "too fast to be two presses — still armed")
    else:
        ctx.log("Esc", "at scope home · press again within 1.5s to quit")


def _step_back(ctx: Ctx) -> bool:
    """Walk the back stack one step, or climb one breadcrumb step; ``False`` at the top."""
    s = ctx.s
    entry = s.back.pop()
    if entry is not None:
        s.route = entry.route
        s.sel = entry.sel
        s.subj_id = entry.subj
        s.sel_id = None
        subject = f" · {entry.subj}" if entry.subj else ""
        ctx.log("Esc", f"back → {entry.route}{subject} · selection restored")
        return True
    up = parent_of(s, ctx.fixture)
    if up is None:
        return False
    s.route, s.subj_id = up
    s.sel = 0
    s.sel_id = None
    subject = f" · {up[1]}" if up[1] else ""
    ctx.log("Esc", f"up → {up[0]}{subject} · one step up the breadcrumb")
    return True


_BATCH_ID = re.compile(r"BAT-\d{4}")
_MILESTONE_ID = re.compile(r"MLS-\d{4}")
_TASK_ID = re.compile(r"EAWF-\d{4}")


def parent_of(session: Session, fixture: Fixture) -> tuple[str, str | None] | None:
    """Return the place one breadcrumb step up, for an Escape with an empty back stack.

    A fixed parent comes first, then the subject's own record: a task climbs to its batch,
    a batch to its milestone, a milestone to its track, a Run to its task. Scope home has no
    parent, and every other place climbs to scope home.
    """
    route, subject = session.route, session.subj_id
    if route in REGISTRY.parents:
        return REGISTRY.parents[route]
    climb = _record_parent(fixture, subject) if subject else None
    if climb is not None:
        return climb
    return None if route == HOME else (HOME, None)


def _record_parent(fixture: Fixture, subject: str) -> tuple[str, str] | None:
    steps = (
        ("EAWF-", "BATCH", _BATCH_ID, "batch.detail"),
        ("BAT-", "MILESTONE", _MILESTONE_ID, "milestone"),
        ("RUN-", "SCOPE", _TASK_ID, "task.detail"),
    )
    for prefix, label, pattern, route in steps:
        if subject.startswith(prefix):
            found = pattern.search(dv.field_of(fixture, subject, label) or "")
            if found:
                return (route, found.group(0))
    if subject.startswith("MLS-"):
        track_id = dv.track_id_of(fixture, dv.field_of(fixture, subject, "TRACK") or "")
        if track_id:
            return ("track", track_id)
    return None


def _tab(ctx: Ctx, k: str, pane: bool) -> None:
    handler = _TAB.get(ctx.s.route)
    if handler is None:
        ctx.noop("Tab")
    else:
        handler(ctx)


def _tab_release(ctx: Ctx) -> None:
    s = ctx.s
    s.rel_reg = "READINESS" if (s.rel_reg or "MEMBERSHIP") == "MEMBERSHIP" else "MEMBERSHIP"
    s.rel_sel = 0
    ctx.log("Tab", f"region → {s.rel_reg}")


def _tab_timeline(ctx: Ctx) -> None:
    s = ctx.s
    regions = list(tl.REGIONS)
    s.tl_reg = regions[(regions.index(s.tl_reg or tl.LANES) + 1) % len(regions)]
    s.tl_sel = 0
    ctx.log("Tab", f"region → {s.tl_reg}")


def _tab_activity(ctx: Ctx) -> None:
    s = ctx.s
    keys = [b.key for b in ctx.fixture.proto.buckets]
    at = keys.index(s.bucket) if s.bucket in keys else -1
    s.bucket = None if at + 1 >= len(keys) else keys[at + 1]
    s.sel = 0
    s.scroll = 0
    ctx.log("Tab", f"bucket → {s.bucket or 'all'}")


def _tab_attention(ctx: Ctx) -> None:
    s, fx = ctx.s, ctx.fixture
    keys: list[str | None] = [None]
    for bucket in fx.proto.xbuckets:
        keys.append(bucket.key)
        keys.extend(sub.key for sub in bucket.sub or ())
    at = keys.index(s.bucket) if s.bucket in keys else 0
    s.bucket = keys[(at + 1) % len(keys)]
    s.sel = 0
    s.scroll = 0
    ctx.log("Tab", "bucket → " + (att.bucket_label(fx, s.bucket) if s.bucket else "all"))


def _tab_milestone(ctx: Ctx) -> None:
    s = ctx.s
    s.section = (s.section + 1) % len(SECTIONS)
    ctx.log("Tab", f"section → {SECTIONS[s.section]}")


def _tab_track(ctx: Ctx) -> None:
    s = ctx.s
    groups = list(track.GROUP_IDS)
    current = s.track_group or groups[0]
    s.track_group = groups[(groups.index(current) + 1) % len(groups)]
    s.sel = 0
    s.scroll = 0
    ctx.log("Tab", f"group → {s.track_group}")


_TAB: Mapping[str, Callable[[Ctx], None]] = MappingProxyType(
    {
        "release": _tab_release,
        "timeline": _tab_timeline,
        "activity": _tab_activity,
        "attention": _tab_attention,
        "milestone": _tab_milestone,
        "track": _tab_track,
    }
)


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
    s.overlay = "readiness"
    s.sel = 0
    ctx.log("m", "readiness matrix · every signal with its evidence")


def _actions(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay != "actions" and not ctx.fixture.menus.verbs(s.route):
        ctx.notify("No action is available here.", "NO ACTIONS", Severity.WARN)
        ctx.log(".", "no verb for this route — nothing to open")
        return
    s.overlay = None if s.overlay == "actions" else "actions"
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
        else:
            s.overlay = None if s.overlay == name else name
            ctx.log(k, note)

    return toggle


def _promote(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    if s.overlay != "draft":
        ctx.noop(k)
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
        text = "Deferred · it keeps its due scope and leaves the drafts list."
        ctx.notify(text, "DEFERRED")
        ctx.log("x", "deferred · the draft is not discarded")
    elif s.route == att.ATTENTION_ROUTE:
        _attention_verb(ctx, k, pane)
    else:
        ctx.noop(k)


def _copy(ctx: Ctx, k: str, pane: bool) -> None:
    copied = copy_target(ctx.s, ctx.fixture)
    ctx.notify(copied, "copied")
    ctx.log("y", f"copied — {copied}")


def _copy_urn(ctx: Ctx, k: str, pane: bool) -> None:
    urn = dv.urn(ctx.s, ctx.fixture)
    ctx.notify(urn, "copied URN")
    ctx.log("Y", f"copied URN — {urn}")


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
    s.overlay = None if s.overlay == "help" else "help"
    ctx.log("?", "route help")


def _palette(ctx: Ctx, k: str, pane: bool) -> None:
    s = ctx.s
    s.overlay = "palette"
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
    if s.route != att.ATTENTION_ROUTE:
        return
    rows = att.attn_list(s, fx)
    row = rows[s.sel] if s.sel < len(rows) else None
    name = att.VERB[k].name
    if att.gated(s, fx, key=k, verb=name, row=row):
        return
    s.verb = k
    s.c_target = None
    s.overlay = "consequence"
    ctx.log(k, f"{name} → consequence preview first")


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
        "Home": _ends,
        "End": _ends,
        "ArrowDown": _move,
        "ArrowUp": _move,
        "j": _move,
        "k": _move,
        "Enter": _enter,
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
        "/": _palette,
        "\\": _filter,
        "ctrl+f": _filter,
        "g": _arm,
        "Tab": _tab,
        "ArrowLeft": _marker,
        "ArrowRight": _marker,
        "a": _attention_verb,
        "z": _attention_verb,
        "v": _attention_verb,
        **dict.fromkeys(_MODIFIERS, _modifier),
    }
)


def _route_independent(ctx: Ctx, k: str, *, pane: bool) -> None:
    """Apply a key every route shares; an unbound key is recorded as unclaimed."""
    _KEYS.get(k, _noop)(ctx, k, pane)


__all__ = ["NAV_KEY", "dispatch", "fire_light", "parent_of"]
