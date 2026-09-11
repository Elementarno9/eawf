"""settings: one route over the real config surface, read through the active lens layer.

The data model (proto-settings.js: show, resolve, inert, state, fallback, editorKind,
catOf, choices) is re-implemented here over ``fixture.settings``; the render and the seam
are proto-g's ``R['settings']`` and its window-capture block. JS distinguishes ``undefined``
(a layer holds nothing) from ``null`` (an explicit null default), so a sentinel stands in
for ``undefined`` wherever the fixture is read.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...chassis import keys
from ...chassis.frame import Fixed, g_frame, g_pad, rule_n
from ...chassis.width import cell_len

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture, Settings
    from ...chassis.session import Session

_UNDEF: Any = object()
_RULE_ROW = re.compile(r"^─+\s*$")
_NUM_KEY = re.compile(r"^[0-9.\-]$")
_INT_HEAD = re.compile(r"^\s*[-+]?\d+")
_FLOAT_HEAD = re.compile(r"^\s*[-+]?(\d+\.?\d*|\.\d+)")


# ---------- proto-settings.js: the model over the data tables ----------


def js_str(v: Any) -> str:
    """JS ``String(v)`` for the scalar kinds the config carries."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if v is None:
        return "null"
    return str(v)


def show(cfg: Settings, v: Any) -> str:
    if v is None or v is _UNDEF:
        return cfg.UNSET
    if isinstance(v, bool):
        return "true" if v else "false"
    if v == "":
        return '""'
    if isinstance(v, list):
        return "[" + ", ".join(js_str(x) for x in v) + "]" if v else "[]"
    if isinstance(v, dict):
        return "{" + str(len(v)) + "}" if v else "{}"
    return js_str(v)


@dataclass(frozen=True, slots=True)
class StackRow:
    layer: str
    value: Any
    kind: str
    where: str
    set_: bool


@dataclass(frozen=True, slots=True)
class Resolved:
    stack: tuple[StackRow, ...]
    winner_layer: str
    winner_value: Any
    full: str


@dataclass(frozen=True, slots=True)
class FieldState:
    g: str
    value: Any
    winner_layer: str
    winner_value: Any
    why: str


def _set_at(cfg: Settings, full: str, layer: str) -> Any:
    return cfg.SET.get(full, {}).get(layer, _UNDEF)


def resolve(cfg: Settings, section: str, key: str, default: Any) -> Resolved:
    full = f"{section}.{key}"
    stack: list[StackRow] = []
    winner_layer, winner_value = "built-in", default
    for layer in cfg.LAYERS:
        lid = layer["id"]
        if lid == "built-in":
            v = default
        elif _set_at(cfg, full, lid) is not _UNDEF:
            v = _set_at(cfg, full, lid)
        elif cfg.RUNTIME.get(full, {}).get(lid, _UNDEF) is not _UNDEF:
            v = cfg.RUNTIME[full][lid]
        else:
            v = _UNDEF
        stack.append(StackRow(lid, v, layer["kind"], layer["where"], v is not _UNDEF))
        if v is not _UNDEF:
            winner_layer, winner_value = lid, v
    return Resolved(tuple(stack), winner_layer, winner_value, full)


def inert(cfg: Settings, full: str, value: Any) -> tuple[Any, str] | None:
    """A floor makes a looser stored value inert: (the floor that applies, who set it)."""
    f = cfg.FLOOR.get(full)
    if not f or value is _UNDEF:
        return None
    order = list(f["order"])
    if value not in order or f["floor"] not in order:
        return None
    i, j = order.index(value), order.index(f["floor"])
    if i >= j:
        return None
    return (f["floor"], f["by"])


def state(cfg: Settings, section: str, key: str, default: Any, lens: str) -> FieldState:
    r = resolve(cfg, section, key, default)
    here = _set_at(cfg, r.full, lens)
    iner = inert(cfg, r.full, here)
    if here is not _UNDEF:
        if iner:
            return FieldState(
                "≠",
                here,
                r.winner_layer,
                r.winner_value,
                f"set here · the profile floor wins, holding {show(cfg, iner[0])}",
            )
        if r.winner_layer == lens:
            return FieldState("=", here, r.winner_layer, r.winner_value, "set here, winning")
        return FieldState(
            "≠",
            here,
            r.winner_layer,
            r.winner_value,
            f"set here · the {r.winner_layer} layer wins, holding {show(cfg, r.winner_value)}",
        )
    if r.winner_layer != "built-in":
        return FieldState(
            "·",
            r.winner_value,
            r.winner_layer,
            r.winner_value,
            f"not set here · inherits {show(cfg, r.winner_value)} from the {r.winner_layer} layer",
        )
    return FieldState(
        "–", default, r.winner_layer, r.winner_value, "not set anywhere · built-in default"
    )


def fallback(
    cfg: Settings, section: str, key: str, default: Any, lens: str
) -> tuple[str, Any] | None:
    """What x falls back to once this layer's line is removed: (layer, value)."""
    full = f"{section}.{key}"
    save = _set_at(cfg, full, lens)
    if save is _UNDEF:
        return None
    del cfg.SET[full][lens]
    r = resolve(cfg, section, key, default)
    cfg.SET[full][lens] = save
    return (r.winner_layer, r.winner_value)


def doc(cfg: Settings, section: str, key: str) -> list[str] | None:
    d = cfg.DOC.get(f"{section}.{key}")
    return list(d) if d else None


def choices(cfg: Settings, section: str, key: str) -> list[str] | None:
    c = cfg.CHOICES.get(f"{section}.{key}")
    return list(c) if c else None


def editor_kind(cfg: Settings, section: str, key: str, typ: str) -> str:
    if typ in ("b", "e"):
        return "pick"
    if typ in ("i", "f"):
        return "num"
    if typ in ("l", "o"):
        return "check" if choices(cfg, section, key) else "list"
    if typ in ("k", "m"):
        return "file"
    return "text"


def cat_of(cfg: Settings, section: str) -> str:
    for cat, members in cfg.CATS:
        if section in members:
            return cat
    return ""


# ---------- proto-g.js: the route's own helpers ----------


def sec(s: Session, cfg: Settings) -> str:
    return cfg.sectionOrder[min(s.set_sec or 0, len(cfg.sectionOrder) - 1)]


def section_keys(s: Session, cfg: Settings) -> list[list[Any]]:
    all_keys = list(cfg.SECTIONS.get(sec(s, cfg), []))
    q = (s.set_filter or "").lower()
    return [k for k in all_keys if q in str(k[0]).lower()] if q else all_keys


def cur_key(s: Session, cfg: Settings) -> list[Any] | None:
    ks = section_keys(s, cfg)
    if not ks:
        return None
    return ks[min(s.set_key or 0, max(0, len(ks) - 1))]


def lens(s: Session) -> str:
    return s.lens or "repo"


def layer_where(cfg: Settings, lid: str) -> str:
    for layer in cfg.LAYERS:
        if layer["id"] == lid:
            return layer["where"]
    return ""


def as_text(v: Any) -> str:
    if v is None or v is _UNDEF:
        return ""
    if isinstance(v, list):
        return ", ".join(js_str(x) for x in v)
    if isinstance(v, dict):
        return ""
    return js_str(v)


def empty_unsets(typ: str) -> bool:
    return typ not in ("s", "n")


def write_value(cfg: Settings, s: Session, section: str, key: str, value: Any) -> None:
    full = f"{section}.{key}"
    cfg.SET.setdefault(full, {})[lens(s)] = value


def unset_value(cfg: Settings, s: Session, section: str, key: str) -> None:
    full = f"{section}.{key}"
    if full in cfg.SET:
        cfg.SET[full].pop(lens(s), None)


def _at(k: list[Any], i: int) -> Any:
    return k[i] if i < len(k) else None


def _parse_num(text: str, is_float: bool) -> float | int | None:
    """JS parseFloat / parseInt: the numeric head of the text, None for NaN."""
    m = (_FLOAT_HEAD if is_float else _INT_HEAD).match(text)
    if not m:
        return None
    return float(m.group(0)) if is_float else int(m.group(0))


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


# ---------- R['settings'] ----------


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    cfg = fixture.settings
    name = sec(s, cfg)
    ks = section_keys(s, cfg)
    wide = w >= 120
    if (s.set_key or 0) >= len(ks):
        s.set_key = max(0, len(ks) - 1)
    k = cur_key(s, cfg)
    rail_w = 14 if w < 120 else 17
    col = w - rail_w - 2
    body: list[str] = []
    kw = min(38, max(24, col - 24))
    vw = col - kw - 3

    def key_cell(nm: str) -> str:
        room = kw - 4
        return g_pad(nm[: room - 2] + "…" if cell_len(nm) > room - 1 else nm, room)

    total = len(cfg.SECTIONS.get(name, []))
    fh = (
        ("   \\" + (s.set_filter or "") + ("▏" if s.set_typing else ""))
        if (s.set_filter or s.set_typing)
        else ""
    )
    strip_raw = " › ".join(cfg.WRITABLE)
    lead = (
        g_pad(" " + name.upper(), 14 if w < 120 else 22)
        + ("" if len(ks) == total else f"{len(ks)} of {total}")
        + fh
    )
    if cell_len(lead) + 3 + cell_len(strip_raw) <= col:
        gap = col - cell_len(lead) - cell_len(strip_raw)
        body.append(g_pad(lead + " " * gap + strip_raw, col))
    else:
        body.append(g_pad(lead[:col] if cell_len(lead) > col else lead, col))
        body.append(g_pad(" " + strip_raw, col))
    body.append(g_pad(g_pad("     KEY", kw) + "VALUE", col))
    # the notes are docked to the bottom, so their height is what the key window spends around
    notes: list[str] = []

    def note(t: str) -> None:
        line = " "
        for wd in str(t).split(" "):
            bare = line in (" ", "   ")
            if cell_len(line) + cell_len(wd) + (0 if bare else 1) > col and line.strip():
                notes.append(line)
                line = "   "
                bare = True
            line += ("" if bare else " ") + wd
            while cell_len(line) > col:
                notes.append(line[:col])
                line = "   " + line[col:]
        if line.strip():
            notes.append(line)

    if k:
        d = doc(cfg, name, k[0])
        kind = editor_kind(cfg, name, k[0], k[1])
        st2 = state(cfg, name, k[0], _at(k, 2), lens(s))
        note(f"{k[0]} · {cfg.TYPE[k[1]]}" + (f" · {_at(k, 4)}" if _at(k, 4) else ""))
        if d:
            note("")
            note(d[0])
        ed = s.edit
        if kind == "pick":
            vals = ["true", "false"] if k[1] == "b" else str(_at(k, 3) or "").split(" · ")
            means: dict[str, str] = {}
            if d and len(d) > 1 and d[1]:
                for cl in d[1].split(" · "):
                    v = cl.split(" ")[0]
                    if v in vals:
                        means[v] = cl[len(v) + 1 :]
            w_opt = max(cell_len(v) for v in vals) + 1
            for i, v in enumerate(vals):
                cur = js_str(st2.value) == v
                pointed = bool(ed) and ed["idx"] == i
                lead_ = (
                    "  " + (("▸ " if pointed else "  ") if ed else "  ") + ("● " if cur else "○ ")
                )
                mean = means.get(v, "")
                if not mean:
                    note(lead_ + v)
                elif cell_len(lead_) + w_opt + cell_len(mean) + 1 <= col:
                    note(lead_ + g_pad(v, w_opt) + mean)
                else:
                    note(lead_ + v)
                    note("       " + mean)
        elif kind == "check":
            cat0 = choices(cfg, name, k[0]) or []
            on = list(ed["on"]) if ed else (list(st2.value) if isinstance(st2.value, list) else [])
            if ed:
                draw = list(ed["opts"])
            elif k[1] == "o":
                draw = [v for v in on if v in cat0] + [v for v in cat0 if v not in on]
            else:
                draw = list(cat0)
            rank = 0
            for i, v in enumerate(draw):
                has = v in on
                pointed = bool(ed) and ed["idx"] == i
                if has:
                    rank += 1
                num = (g_pad(str(rank), 3) if has else "   ") if k[1] == "o" else ""
                note(
                    ((" ▸ " if pointed else "   ") if ed else "   ")
                    + num
                    + "["
                    + ("×" if has else " ")
                    + "] "
                    + v
                )
            if k[1] == "o":
                note("the numbers are failover order")
        elif kind == "list" and ed:
            for i, v in enumerate(ed["items"] or ["‹empty›"]):
                pointed = ed["idx"] == i
                note(
                    (" ▸ " if pointed else "   ")
                    + ((ed["text"] + "▏") if pointed and ed["text"] is not None else v)
                )
        elif kind in ("num", "text") and ed:
            note(
                "   typing   "
                + ed["text"]
                + "▏"
                + (f"      range {_at(k, 3)}" if kind == "num" and _at(k, 3) else "")
            )
        if kind == "file":
            note(
                "read-only here · entries merge across layers by id, so a row removed in one layer "
                "only stops contributing fields — edit it in " + layer_where(cfg, lens(s))
            )
    # the key window spends what the notes leave
    avail = h - 5
    lead_len = cell_len(
        g_pad(" " + name.upper(), 22) + ("" if len(ks) == total else f"{len(ks)} of {total}") + fh
    )
    strip_own_row = 1 if lead_len + 3 + cell_len(strip_raw) > col else 0
    note_cap = max(2, avail - 2 - strip_own_row - 1 - 3 - 1)
    if len(notes) > note_cap:
        notes = notes[:note_cap]
    key_win = max(2, min(len(ks), avail - 2 - strip_own_row - 1 - len(notes) - 1))
    more = len(ks) > key_win
    if not more:
        key_win = min(len(ks), key_win + 1)
    top = max(0, min((s.set_key or 0) - key_win // 2, max(0, len(ks) - key_win)))
    for i, row in enumerate(ks[top : top + key_win]):
        idx = top + i
        st = state(cfg, name, row[0], _at(row, 2), lens(s))
        mark = "▸" if idx == (s.set_key or 0) else " "
        read_only = editor_kind(cfg, name, row[0], row[1]) == "file"
        val = show(cfg, st.value) + ("   read-only" if read_only else "")
        body.append(g_pad(mark + " " + st.g + " " + key_cell(row[0]) + g_pad(val, vw), col))
    if not ks:
        body.append(
            "  nothing matches \\" + s.set_filter
            if s.set_filter
            else "  this section holds no keys · kept for schema shape"
        )
    if more:
        body.append(f"  {top + 1}–{top + key_win} of {len(ks)}")
    while len(body) < avail - len(notes) - 1:
        body.append("")
    if notes:
        body.append(rule_n(col))
        body.extend(notes)
    # the rail: category rows for orientation, section rows for the cursor
    rows = max(len(body), h - 5)
    rw = list(cfg.RAIL)
    sel_ix = 0
    for ri, r in enumerate(rw):
        if r.get("section") == name:
            sel_ix = ri
            break
    rtop = max(0, min(sel_ix - rows // 2, max(0, len(rw) - rows)))
    out: list[str] = []
    for i in range(rows):
        r = rw[rtop + i] if rtop + i < len(rw) else None
        b = body[i] if i < len(body) else ""
        if r is None:
            label = ""
        elif not r.get("section"):
            label = r["cat"]
        else:
            label = ("▸ " if r["section"] == name else "  ") + r["section"]
        braw = g_pad(b, col)
        join = "├─" if _RULE_ROW.match(braw) else "│ "
        out.append(Fixed(g_pad(g_pad(label, rail_w) + join + braw, w)))
    if out:
        out.append(Fixed(g_pad("─" * rail_w + "┴" + "─" * max(0, w - rail_w - 1), w)))
    file_kind = bool(k) and editor_kind(cfg, name, k[0], k[1]) == "file"
    klist: list[tuple[str, str]]
    if s.edit:
        ek = s.edit["kind"]
        if ek == "pick":
            klist = [("↑/↓", "choose"), ("Enter", "commit"), ("Esc", "cancel")]
        elif ek == "check":
            klist = (
                [("space", "tick"), ("↑/↓", "move")]
                + ([("J/K", "order")] if k and k[1] == "o" else [])
                + [("Enter", "commit"), ("Esc", "cancel")]
            )
        elif ek == "list":
            klist = [
                ("a", "add"),
                ("x", "remove"),
                ("↑/↓", "row"),
                ("Enter", "commit"),
                ("Esc", "cancel"),
            ]
        elif ek == "num":
            klist = [("↑/↓", "step"), ("Enter", "commit"), ("Esc", "cancel")]
        else:
            eu = empty_unsets(k[1]) if k else True
            klist = [
                ("Enter", "commit"),
                ("Esc", "cancel"),
                ("empty" if eu else '""', "unsets" if eu else "stores empty"),
            ]
    elif s.set_typing:
        klist = [("type", "narrow"), ("↑↓", "field"), ("Enter", "keep"), ("Esc", "clear")]
    else:
        klist = (
            [("↑↓", "field"), ("Tab", "section"), ("l", "layer"), ("Esc", "back")]
            if file_kind
            else [
                ("↑↓", "field"),
                ("Tab", "section"),
                ("Enter", "edit"),
                ("l", "layer"),
                ("x", "unset"),
                ("Esc", "back"),
            ]
        )
        if wide:
            klist += [("i", "stack"), ("\\", "filter")]
    ctx = (
        f"{len(cfg.CATS)} categories · {len(cfg.names)} sections · in {cat_of(cfg, name)} ▸ {name}"
        + (" · editing" if s.edit else "")
    )
    return g_frame(session, fixture, "Eä ▸ eawf-core ▸ Settings", ctx, out, klist, w, h)


# ---------- the seam: the lens, the sections, the editor ----------


def _seam_edit(ctx: keys.Ctx, k: str) -> bool:
    s, cfg = ctx.s, ctx.fixture.settings
    kk = cur_key(s, cfg)
    ed = s.edit
    if k == "Escape":
        s.edit = None
        ctx.log("Esc", "edit cancelled · nothing written")
        return True
    if kk is None or ed is None:
        return True
    name = sec(s, cfg)
    if ed["kind"] == "pick":
        if k in ("ArrowDown", "ArrowUp"):
            ed["idx"] = (ed["idx"] + (1 if k == "ArrowDown" else len(ed["vals"]) - 1)) % len(
                ed["vals"]
            )
            ctx.log(k, f"→ {ed['vals'][ed['idx']]}")
        elif k == "Enter":
            pv = ed["vals"][ed["idx"]]
            v: Any = (pv == "true") if kk[1] == "b" else pv
            write_value(cfg, s, name, kk[0], v)
            inr = inert(cfg, f"{name}.{kk[0]}", v)
            ctx.log(
                "Enter",
                f"wrote {name}.{kk[0]} = {show(cfg, v)} at {lens(s)}"
                + (f" · inert: floor {show(cfg, inr[0])} applies" if inr else ""),
            )
            s.edit = None
        return True
    if ed["kind"] == "check":
        if k in ("ArrowDown", "ArrowUp"):
            ed["idx"] = (ed["idx"] + (1 if k == "ArrowDown" else len(ed["opts"]) - 1)) % len(
                ed["opts"]
            )
        elif k == " ":
            o = ed["opts"][ed["idx"]]
            was_on = o in ed["on"]
            if was_on:
                ed["on"].remove(o)
            else:
                ed["on"].append(o)
            ctx.log("space", ("untick " if was_on else "tick ") + f"{o} · {len(ed['on'])} selected")
        elif k in ("J", "K") and ed["ordered"]:
            to = ed["idx"] + (1 if k == "J" else -1)
            if 0 <= to < len(ed["opts"]):
                ed["opts"][ed["idx"]], ed["opts"][to] = ed["opts"][to], ed["opts"][ed["idx"]]
                ed["idx"] = to
                ctx.log(k, "order → " + ", ".join(v for v in ed["opts"] if v in ed["on"]))
            else:
                ctx.log(k, "already at the " + ("bottom" if k == "J" else "top"))
        elif k == "Enter":
            picked = [v for v in ed["opts"] if v in ed["on"]]
            write_value(cfg, s, name, kk[0], picked)
            ctx.log("Enter", f"wrote {name}.{kk[0]} = {show(cfg, picked)} at {lens(s)}")
            s.edit = None
        return True
    if ed["kind"] == "list":
        if k in ("ArrowDown", "ArrowUp"):
            if ed["text"] is not None:
                ed["items"][ed["idx"]] = ed["text"]
                ed["text"] = None
            n2 = max(1, len(ed["items"]))
            ed["idx"] = (ed["idx"] + (1 if k == "ArrowDown" else n2 - 1)) % n2
        elif k == "a" and ed["text"] is None:
            ed["items"].insert(ed["idx"] + 1, "")
            ed["idx"] += 1
            ed["text"] = ""
            ctx.log("a", "row added · type it")
        elif k == "x" and ed["text"] is None:
            if ed["items"]:
                rm = ed["items"].pop(ed["idx"])
                ed["idx"] = max(0, ed["idx"] - 1)
                ctx.log("x", "removed " + (rm or "‹empty›"))
        elif k == "Enter":
            if ed["text"] is not None:
                ed["items"][ed["idx"]] = ed["text"]
            items = [x for x in ed["items"] if x != ""]
            write_value(cfg, s, name, kk[0], items)
            ctx.log(
                "Enter",
                f"wrote {name}.{kk[0]} = {show(cfg, items)} at {lens(s)}"
                + ("" if items else " · an empty list, not unset — x removes the key"),
            )
            s.edit = None
        elif k == "Backspace":
            if ed["text"] is None:
                ed["text"] = ed["items"][ed["idx"]] if ed["idx"] < len(ed["items"]) else ""
            ed["text"] = ed["text"][:-1]
        elif len(k) == 1:
            if ed["text"] is None:
                ed["text"] = ""
            ed["text"] += k
        return True
    if ed["kind"] == "num":
        if k in ("ArrowUp", "ArrowDown"):
            step = 0.05 if kk[1] == "f" else 1
            cur = _parse_num(ed["text"] or "0", True) or 0
            nv = cur + (step if k == "ArrowUp" else -step)
            if ed["min"] is not None and nv < ed["min"]:
                nv = ed["min"]
            if ed["max"] is not None and nv > ed["max"]:
                nv = ed["max"]
            ed["text"] = (
                js_str(_js_round(nv * 100) / 100) if kk[1] == "f" else js_str(_js_round(nv))
            )
        elif k == "Enter":
            if ed["text"].strip() == "":
                unset_value(cfg, s, name, kk[0])
                ctx.log("Enter", f"unset {name}.{kk[0]} at {lens(s)}")
            else:
                num = _parse_num(ed["text"], kk[1] == "f")
                if num is None:
                    ctx.log("Enter", "not a number — nothing written")
                    s.edit = None
                    return True
                if ed["min"] is not None and num < ed["min"]:
                    num = ed["min"]
                if ed["max"] is not None and num > ed["max"]:
                    num = ed["max"]
                write_value(cfg, s, name, kk[0], num)
                ctx.log(
                    "Enter",
                    f"wrote {name}.{kk[0]} = {js_str(num)} at {lens(s)}"
                    + (
                        f" · range {js_str(ed['min'])}–{js_str(ed['max'])}"
                        if ed["min"] is not None
                        else ""
                    ),
                )
            s.edit = None
        elif k == "Backspace":
            ed["text"] = ed["text"][:-1]
        elif _NUM_KEY.match(k):
            ed["text"] += k
        return True
    # text
    if k == "Enter":
        text = ed["text"]
        if text.strip() == "" and empty_unsets(kk[1]):
            unset_value(cfg, s, name, kk[0])
            ctx.log("Enter", f"unset {name}.{kk[0]} at {lens(s)}")
        else:
            write_value(cfg, s, name, kk[0], text)
            ctx.log("Enter", f"wrote {name}.{kk[0]} = {show(cfg, text)} at {lens(s)}")
        s.edit = None
        return True
    if k == "Backspace":
        ed["text"] = ed["text"][:-1]
        return True
    if len(k) == 1:
        ed["text"] += k
        return True
    # the prototype's text editor lets any other key fall through to the core
    return False


def _open_editor(ctx: keys.Ctx) -> None:
    s, cfg = ctx.s, ctx.fixture.settings
    ck = cur_key(s, cfg)
    if not ck:
        return
    name = sec(s, cfg)
    kd = editor_kind(cfg, name, ck[0], ck[1])
    stt = state(cfg, name, ck[0], _at(ck, 2), lens(s))
    here = _set_at(cfg, f"{name}.{ck[0]}", lens(s))
    base = here if here is not _UNDEF else stt.value
    if kd == "file":
        ctx.log("Enter", f"{ck[0]} merges by id · edit it in {layer_where(cfg, lens(s))}")
        return
    if kd == "pick":
        vals = ["true", "false"] if ck[1] == "b" else str(_at(ck, 3) or "").split(" · ")
        at = vals.index(js_str(base)) if js_str(base) in vals else 0
        s.edit = {"kind": "pick", "vals": vals, "idx": at, "text": None}
        ctx.log("Enter", f"choosing {name}.{ck[0]} · {len(vals)} values · writes to {lens(s)}")
    elif kd == "check":
        cat = choices(cfg, name, ck[0]) or []
        on_now = list(base) if isinstance(base, list) else []
        disp = (
            ([v for v in on_now if v in cat] + [v for v in cat if v not in on_now])
            if ck[1] == "o"
            else list(cat)
        )
        s.edit = {
            "kind": "check",
            "opts": disp,
            "on": on_now,
            "idx": 0,
            "text": None,
            "ordered": ck[1] == "o",
        }
        ctx.log("Enter", f"ticking {name}.{ck[0]} · writes to {lens(s)}")
    elif kd == "list":
        s.edit = {
            "kind": "list",
            "items": list(base) if isinstance(base, list) else [],
            "idx": 0,
            "text": None,
        }
        ctx.log("Enter", f"editing the list {name}.{ck[0]} · writes to {lens(s)}")
    elif kd == "num":
        rng = str(_at(ck, 3) or "").split("–")
        mn = _parse_num(rng[0], True)
        mx = _parse_num(rng[1], True) if len(rng) > 1 else None
        s.edit = {
            "kind": "num",
            "text": "" if base is None or base is _UNDEF else js_str(base),
            "min": mn,
            "max": mx,
        }
        ctx.log(
            "Enter",
            f"editing {name}.{ck[0]}"
            + (f" · range {_at(ck, 3)}" if _at(ck, 3) else "")
            + f" · writes to {lens(s)}",
        )
    else:
        s.edit = {"kind": "text", "text": as_text(base)}
        ctx.log(
            "Enter",
            f"editing {name}.{ck[0]} at {lens(s)}"
            + (" · shadowed, and the write still lands here" if stt.g == "≠" else ""),
        )


def seam(ctx: keys.Ctx, key: str, shift: bool) -> bool:
    s, cfg = ctx.s, ctx.fixture.settings
    if s.overlay:
        return False
    k = key
    if s.edit is not None:
        return _seam_edit(ctx, k)
    # the filter is owned here: the core's filter mode writes a variable this route never reads
    if k == "\\" and not s.set_typing:
        s.set_typing = True
        s.typing = True
        ctx.log("\\\\", f"filter · type to narrow {sec(s, cfg)} · Esc clears")
        return True
    if s.set_typing:
        if k == "Escape":
            s.set_typing = False
            s.typing = False
            s.set_filter = ""
            ctx.log("Esc", "filter cleared")
        elif k == "Enter":
            s.set_typing = False
            s.typing = False
            ctx.log(
                "Enter",
                ("filter kept · " if s.set_filter else "no filter · ")
                + f"{len(section_keys(s, cfg))} of {len(cfg.SECTIONS.get(sec(s, cfg), []))} keys",
            )
        elif k == "Backspace":
            s.set_filter = (s.set_filter or "")[:-1]
            s.set_key = 0
        elif k in ("ArrowDown", "ArrowUp"):
            kf = section_keys(s, cfg)
            if kf:
                s.set_key = max(
                    0, min(len(kf) - 1, (s.set_key or 0) + (1 if k == "ArrowDown" else -1))
                )
        elif len(k) == 1:
            s.set_filter = (s.set_filter or "") + k
            s.set_key = 0
        return True
    if k in ("l", "L"):
        w5 = list(cfg.WRITABLE)
        ix = w5.index(lens(s)) if lens(s) in w5 else -1
        back = k == "L" or shift
        s.lens = w5[(ix + (len(w5) - 1 if back else 1)) % len(w5)]
        ctx.log("L" if back else "l", f"layer → {s.lens} · {layer_where(cfg, s.lens)}")
        return True
    if k == "Tab":
        n = len(cfg.sectionOrder)
        s.set_sec = ((s.set_sec or 0) + (n - 1 if shift else 1)) % n
        s.set_key = 0
        s.set_filter = ""
        ctx.log("Shift+Tab" if shift else "Tab", f"{cat_of(cfg, sec(s, cfg))} ▸ {sec(s, cfg)}")
        return True
    if k in ("ArrowDown", "ArrowUp"):
        ks = section_keys(s, cfg)
        if not ks:
            ctx.log(k, f"{sec(s, cfg)} holds no keys")
            return True
        s.set_key = max(0, min(len(ks) - 1, (s.set_key or 0) + (1 if k == "ArrowDown" else -1)))
        ck = cur_key(s, cfg)
        ctx.log(k, f"{sec(s, cfg)}.{ck[0] if ck else ''}")
        return True
    if k == "Enter":
        _open_editor(ctx)
        return True
    if k == " ":
        bk = cur_key(s, cfg)
        if bk and bk[1] == "b":
            cur = state(cfg, sec(s, cfg), bk[0], _at(bk, 2), lens(s)).value
            write_value(cfg, s, sec(s, cfg), bk[0], not cur)
            ctx.log("space", f"{sec(s, cfg)}.{bk[0]} = {js_str(not cur)} at {lens(s)}")
            return True
    if k == "x":
        xk = cur_key(s, cfg)
        if xk:
            fb = fallback(cfg, sec(s, cfg), xk[0], _at(xk, 2), lens(s))
            if not fb:
                ctx.log("x", f"not set at {lens(s)} — nothing to unset")
            else:
                unset_value(cfg, s, sec(s, cfg), xk[0])
                ctx.log(
                    "x", f"unset {sec(s, cfg)}.{xk[0]} · falls back to {fb[0]} {show(cfg, fb[1])}"
                )
            return True
    if k == "i":
        keys.go(ctx, "settings.stack", "stack")
        return True
    return False
