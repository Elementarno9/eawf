"""settings: one route over the real config surface, read through the active lens layer.

The model half resolves a key through the nine layers, marks a stored value a floor makes
inert, and names the editor a type gets. The route half draws the section rail, the key
window and the notes for the key under the cursor, and owns the lens, the filter and the
editor keys. A layer that holds nothing is told apart from one that holds an explicit null
by a sentinel, because the catalog carries both.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from eawf.surfaces.tui.console.chrome import SettingsCatalog
from eawf.surfaces.tui.console.frame import Fixed, View, g_frame, g_pad, rule_n
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.renderers.provenance import list_frame
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import cell_len

UNDEFINED: Any = object()
BUILT_IN = "built-in"
DEFAULT_LENS = "repo"
_RULE_ROW = re.compile(r"^─+\s*$")
_NUM_KEY = re.compile(r"^[0-9.\-]$")
_INT_HEAD = re.compile(r"^\s*[-+]?\d+")
_FLOAT_HEAD = re.compile(r"^\s*[-+]?(\d+\.?\d*|\.\d+)")
_EMPTY_ITEM = "‹empty›"  # noqa: RUF001
_RANGE_SEP = "–"  # noqa: RUF001
# The gutter glyph of a key no layer sets.
_UNSET_GLYPH = _RANGE_SEP
Key = Sequence[Any]


# ---------- the model over the catalog ----------


def js_str(value: Any) -> str:
    """Return a scalar the way the catalog's own tooling prints it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if value is None:
        return "null"
    return str(value)


def show(cfg: SettingsCatalog, stored: Any) -> str:
    """Return a stored value as the route shows it."""
    if stored is None or stored is UNDEFINED:
        return cfg.unset
    if isinstance(stored, bool):
        return "true" if stored else "false"
    if stored == "":
        return '""'
    if isinstance(stored, list):
        return "[" + ", ".join(js_str(x) for x in stored) + "]" if stored else "[]"
    if isinstance(stored, dict):
        return "{" + str(len(stored)) + "}" if stored else "{}"
    return js_str(stored)


@dataclass(frozen=True, slots=True)
class StackRow:
    """One layer of a key's stack: what it holds, its kind, where it lives, whether set."""

    layer: str
    value: Any
    kind: str
    where: str
    is_set: bool


@dataclass(frozen=True, slots=True)
class Resolved:
    """A key resolved through every layer, with the layer that wins."""

    stack: tuple[StackRow, ...]
    winner_layer: str
    winner_value: Any
    full: str


@dataclass(frozen=True, slots=True)
class FieldState:
    """A key as the lens sees it: its gutter glyph, value, winner and the reason."""

    glyph: str
    value: Any
    winner_layer: str
    winner_value: Any
    why: str


def _stored_at(cfg: SettingsCatalog, full: str, layer: str) -> Any:
    return cfg.stored.get(full, {}).get(layer, UNDEFINED)


def resolve(cfg: SettingsCatalog, section: str, key: str, default: Any) -> Resolved:
    """Resolve ``section.key`` through the layers, the last layer holding a value winning."""
    full = f"{section}.{key}"
    stack: list[StackRow] = []
    winner_layer, winner_value = BUILT_IN, default
    for layer in cfg.layers:
        lid = layer["id"]
        if lid == BUILT_IN:
            value = default
        elif _stored_at(cfg, full, lid) is not UNDEFINED:
            value = _stored_at(cfg, full, lid)
        else:
            value = cfg.runtime.get(full, {}).get(lid, UNDEFINED)
        is_set = value is not UNDEFINED
        stack.append(StackRow(lid, value, layer["kind"], layer["where"], is_set))
        if is_set:
            winner_layer, winner_value = lid, value
    return Resolved(tuple(stack), winner_layer, winner_value, full)


def inert(cfg: SettingsCatalog, full: str, value: Any) -> tuple[Any, str] | None:
    """Return the floor and who set it when a floor makes ``value`` inert, else ``None``."""
    floor = cfg.floor.get(full)
    if not floor or value is UNDEFINED:
        return None
    order = list(floor["order"])
    if value not in order or floor["floor"] not in order:
        return None
    if order.index(value) >= order.index(floor["floor"]):
        return None
    return (floor["floor"], floor["by"])


def state(cfg: SettingsCatalog, section: str, key: str, default: Any, lens: str) -> FieldState:
    """Return how ``section.key`` reads through ``lens``."""
    r = resolve(cfg, section, key, default)
    here = _stored_at(cfg, r.full, lens)
    if here is not UNDEFINED:
        floor = inert(cfg, r.full, here)
        if floor:
            why = f"set here · the profile floor wins, holding {show(cfg, floor[0])}"
            return FieldState("≠", here, r.winner_layer, r.winner_value, why)
        if r.winner_layer == lens:
            return FieldState("=", here, r.winner_layer, r.winner_value, "set here, winning")
        why = f"set here · the {r.winner_layer} layer wins, holding {show(cfg, r.winner_value)}"
        return FieldState("≠", here, r.winner_layer, r.winner_value, why)
    if r.winner_layer != BUILT_IN:
        why = f"not set here · inherits {show(cfg, r.winner_value)} from the {r.winner_layer} layer"
        return FieldState("·", r.winner_value, r.winner_layer, r.winner_value, why)
    return FieldState(
        _UNSET_GLYPH, default, r.winner_layer, r.winner_value, "not set anywhere · built-in default"
    )


def fallback(
    cfg: SettingsCatalog, section: str, key: str, default: Any, lens: str
) -> tuple[str, Any] | None:
    """Return the layer and value ``section.key`` falls back to once ``lens`` stops holding it."""
    full = f"{section}.{key}"
    saved = _stored_at(cfg, full, lens)
    if saved is UNDEFINED:
        return None
    del cfg.stored[full][lens]
    r = resolve(cfg, section, key, default)
    cfg.stored[full][lens] = saved
    return (r.winner_layer, r.winner_value)


def doc(cfg: SettingsCatalog, section: str, key: str) -> list[str] | None:
    """Return the documentation lines of ``section.key``."""
    lines = cfg.doc.get(f"{section}.{key}")
    return list(lines) if lines else None


def choices(cfg: SettingsCatalog, section: str, key: str) -> list[str] | None:
    """Return the fixed choices of ``section.key``."""
    options = cfg.choices.get(f"{section}.{key}")
    return list(options) if options else None


def editor_kind(cfg: SettingsCatalog, section: str, key: str, typ: str) -> str:
    """Return the editor a key of type ``typ`` opens: pick, num, check, list, file or text."""
    if typ in ("b", "e"):
        return "pick"
    if typ in ("i", "f"):
        return "num"
    if typ in ("l", "o"):
        return "check" if choices(cfg, section, key) else "list"
    if typ in ("k", "m"):
        return "file"
    return "text"


def cat_of(cfg: SettingsCatalog, section: str) -> str:
    """Return the category ``section`` is filed under."""
    return next((cat for cat, members in cfg.cats if section in members), "")


# ---------- the route's own helpers ----------


def sec(s: Session, cfg: SettingsCatalog) -> str:
    """Return the section the rail cursor is on."""
    return cfg.section_order[min(s.set_sec, len(cfg.section_order) - 1)]


def section_keys(s: Session, cfg: SettingsCatalog) -> list[Key]:
    """Return the section's keys the filter leaves."""
    keys = list(cfg.sections.get(sec(s, cfg), []))
    query = s.set_filter.lower()
    return [k for k in keys if query in str(k[0]).lower()] if query else keys


def cur_key(s: Session, cfg: SettingsCatalog) -> Key | None:
    """Return the key under the cursor, the last key past the end."""
    keys = section_keys(s, cfg)
    return keys[min(s.set_key, len(keys) - 1)] if keys else None


def lens(s: Session) -> str:
    """Return the layer edits write to."""
    return s.lens or DEFAULT_LENS


def layer_where(cfg: SettingsCatalog, lid: str) -> str:
    """Return where layer ``lid`` lives."""
    return next((layer["where"] for layer in cfg.layers if layer["id"] == lid), "")


def as_text(value: Any) -> str:
    """Return a value as a text editor starts from."""
    if value is None or value is UNDEFINED or isinstance(value, dict):
        return ""
    if isinstance(value, list):
        return ", ".join(js_str(x) for x in value)
    return js_str(value)


def empty_unsets(typ: str) -> bool:
    """Return whether committing an empty text unsets a key of type ``typ``."""
    return typ not in ("s", "n")


def write_value(cfg: SettingsCatalog, s: Session, section: str, key: str, value: Any) -> None:
    """Store ``value`` for ``section.key`` at the lens layer."""
    cfg.stored.setdefault(f"{section}.{key}", {})[lens(s)] = value


def unset_value(cfg: SettingsCatalog, s: Session, section: str, key: str) -> None:
    """Remove the lens layer's value for ``section.key``."""
    full = f"{section}.{key}"
    if full in cfg.stored:
        cfg.stored[full].pop(lens(s), None)


def at(k: Key, i: int) -> Any:
    """Return column ``i`` of a key row, ``None`` past its end."""
    return k[i] if i < len(k) else None


def parse_num(text: str, *, is_float: bool) -> float | int | None:
    """Return the numeric head of ``text``, ``None`` when it has none."""
    found = (_FLOAT_HEAD if is_float else _INT_HEAD).match(text)
    if not found:
        return None
    return float(found.group(0)) if is_float else int(found.group(0))


def js_round(x: float) -> int:
    """Round half up, as the catalog's own tooling does."""
    return math.floor(x + 0.5)


# ---------- the frame ----------


@dataclass(frozen=True, slots=True)
class _Layout:
    rail_w: int
    col: int
    key_w: int
    value_w: int

    @classmethod
    def at_width(cls, w: int) -> _Layout:
        rail_w = 14 if w < 120 else 17
        col = w - rail_w - 2
        key_w = min(38, max(24, col - 24))
        return cls(rail_w, col, key_w, col - key_w - 3)


class _Notes:
    """The notes docked under the key window, wrapped to the body column."""

    def __init__(self, col: int) -> None:
        self.col = col
        self.lines: list[str] = []

    def add(self, text: str) -> None:
        line = " "
        for word in text.split(" "):
            bare = line in (" ", "   ")
            if cell_len(line) + cell_len(word) + (0 if bare else 1) > self.col and line.strip():
                self.lines.append(line)
                line = "   "
                bare = True
            line += ("" if bare else " ") + word
            while cell_len(line) > self.col:
                self.lines.append(line[: self.col])
                line = "   " + line[self.col :]
        if line.strip():
            self.lines.append(line)


def _pick_notes(notes: _Notes, k: Key, docs: list[str] | None, value: Any, ed: Any) -> None:
    values = ["true", "false"] if k[1] == "b" else str(at(k, 3) or "").split(" · ")
    means: dict[str, str] = {}
    if docs and len(docs) > 1 and docs[1]:
        for clause in docs[1].split(" · "):
            v = clause.split(" ")[0]
            if v in values:
                means[v] = clause[len(v) + 1 :]
    width = max(cell_len(v) for v in values) + 1
    for i, v in enumerate(values):
        pointer = ("▸ " if ed["idx"] == i else "  ") if ed else "  "
        lead = "  " + pointer + ("● " if js_str(value) == v else "○ ")
        mean = means.get(v, "")
        if not mean:
            notes.add(lead + v)
        elif cell_len(lead) + width + cell_len(mean) + 1 <= notes.col:
            notes.add(lead + g_pad(v, width) + mean)
        else:
            notes.add(lead + v)
            notes.add("       " + mean)


def _check_notes(
    notes: _Notes, cfg: SettingsCatalog, name: str, k: Key, value: Any, ed: Any
) -> None:
    catalog = choices(cfg, name, k[0]) or []
    ticked = list(ed["on"]) if ed else (list(value) if isinstance(value, list) else [])
    if ed:
        drawn = list(ed["opts"])
    elif k[1] == "o":
        drawn = [v for v in ticked if v in catalog] + [v for v in catalog if v not in ticked]
    else:
        drawn = list(catalog)
    rank = 0
    for i, v in enumerate(drawn):
        has = v in ticked
        rank += 1 if has else 0
        number = (g_pad(str(rank), 3) if has else "   ") if k[1] == "o" else ""
        pointer = (" ▸ " if ed["idx"] == i else "   ") if ed else "   "
        notes.add(pointer + number + "[" + ("×" if has else " ") + "] " + v)  # noqa: RUF001
    if k[1] == "o":
        notes.add("the numbers are failover order")


def _editor_notes(view: View, notes: _Notes, name: str, k: Key) -> None:
    """Add the notes for the key under the cursor: its type, its doc, its editor."""
    s, cfg = view.session, view.fixture.settings
    docs = doc(cfg, name, k[0])
    kind = editor_kind(cfg, name, k[0], k[1])
    value = state(cfg, name, k[0], at(k, 2), lens(s)).value
    notes.add(f"{k[0]} · {cfg.types[k[1]]}" + (f" · {at(k, 4)}" if at(k, 4) else ""))
    if docs:
        notes.add("")
        notes.add(docs[0])
    ed = s.edit
    if kind == "pick":
        _pick_notes(notes, k, docs, value, ed)
    elif kind == "check":
        _check_notes(notes, cfg, name, k, value, ed)
    elif kind == "list" and ed:
        for i, item in enumerate(ed["items"] or [_EMPTY_ITEM]):
            pointed = ed["idx"] == i
            typed = pointed and ed["text"] is not None
            notes.add((" ▸ " if pointed else "   ") + (ed["text"] + "▏" if typed else item))
    elif kind in ("num", "text") and ed:
        span = f"      range {at(k, 3)}" if kind == "num" and at(k, 3) else ""
        notes.add(f"   typing   {ed['text']}▏{span}")
    if kind == "file":
        notes.add(
            "read-only here · entries merge across layers by id, so a row removed in one layer "
            "only stops contributing fields — edit it in " + layer_where(cfg, lens(s))
        )


def _key_window(view: View, layout: _Layout, name: str, notes: list[str]) -> list[str]:
    """Return the key rows the notes leave room for, with the window count when cut."""
    s, cfg, h = view.session, view.fixture.settings, view.h
    keys = section_keys(s, cfg)
    total = len(cfg.sections.get(name, []))
    avail = h - 5
    count = "" if len(keys) == total else f"{len(keys)} of {total}"
    lead_len = cell_len(g_pad(" " + name.upper(), 22) + count + _filter_hint(s))
    strip_row = 1 if lead_len + 3 + cell_len(_strip(cfg)) > layout.col else 0
    shown = max(2, min(len(keys), avail - 2 - strip_row - 1 - len(notes) - 1))
    more = len(keys) > shown
    if not more:
        shown = min(len(keys), shown + 1)
    top = max(0, min(s.set_key - shown // 2, max(0, len(keys) - shown)))
    rows: list[str] = []
    for i, row in enumerate(keys[top : top + shown]):
        field = state(cfg, name, row[0], at(row, 2), lens(s))
        mark = "▸" if top + i == s.set_key else " "
        read_only = editor_kind(cfg, name, row[0], row[1]) == "file"
        value = show(cfg, field.value) + ("   read-only" if read_only else "")
        cell = _key_cell(row[0], layout.key_w)
        rows.append(
            g_pad(f"{mark} {field.glyph} {cell}" + g_pad(value, layout.value_w), layout.col)
        )
    if not keys:
        empty = "  this section holds no keys · kept for schema shape"
        rows.append("  nothing matches \\" + s.set_filter if s.set_filter else empty)
    if more:
        rows.append(f"  {top + 1}{_RANGE_SEP}{top + shown} of {len(keys)}")
    return rows


def _key_cell(name: str, key_w: int) -> str:
    room = key_w - 4
    return g_pad(name[: room - 2] + "…" if cell_len(name) > room - 1 else name, room)


def _filter_hint(s: Session) -> str:
    if not (s.set_filter or s.set_typing):
        return ""
    return "   \\" + s.set_filter + ("▏" if s.set_typing else "")


def _strip(cfg: SettingsCatalog) -> str:
    return " › ".join(cfg.writable)  # noqa: RUF001


def _body(view: View, layout: _Layout, name: str, k: Key | None) -> list[str]:
    s, cfg, w, h = view.session, view.fixture.settings, view.w, view.h
    col = layout.col
    keys = section_keys(s, cfg)
    total = len(cfg.sections.get(name, []))
    count = "" if len(keys) == total else f"{len(keys)} of {total}"
    lead = g_pad(" " + name.upper(), 14 if w < 120 else 22) + count + _filter_hint(s)
    strip = _strip(cfg)
    body: list[str] = []
    if cell_len(lead) + 3 + cell_len(strip) <= col:
        gap = col - cell_len(lead) - cell_len(strip)
        body.append(g_pad(lead + " " * gap + strip, col))
    else:
        body.append(g_pad(lead[:col] if cell_len(lead) > col else lead, col))
        body.append(g_pad(" " + strip, col))
    body.append(g_pad(g_pad("     KEY", layout.key_w) + "VALUE", col))
    notes = _Notes(col)
    if k:
        _editor_notes(view, notes, name, k)
    avail = h - 5
    lead_len = cell_len(g_pad(" " + name.upper(), 22) + count + _filter_hint(s))
    strip_row = 1 if lead_len + 3 + cell_len(strip) > col else 0
    cap = max(2, avail - 2 - strip_row - 1 - 3 - 1)
    kept = notes.lines[:cap]
    body += _key_window(view, layout, name, kept)
    body.extend("" for _ in range(avail - len(kept) - 1 - len(body)))
    if kept:
        body.append(rule_n(col))
        body.extend(kept)
    return body


def _with_rail(view: View, layout: _Layout, name: str, body: list[str]) -> list[str]:
    """Return the body beside the rail: category rows for orientation, sections for the cursor."""
    cfg, w, h = view.fixture.settings, view.w, view.h
    rows = max(len(body), h - 5)
    rail = list(cfg.rail)
    selected = next((i for i, r in enumerate(rail) if r.get("section") == name), 0)
    top = max(0, min(selected - rows // 2, max(0, len(rail) - rows)))
    out: list[str] = []
    for i in range(rows):
        entry = rail[top + i] if top + i < len(rail) else None
        if entry is None:
            label = ""
        elif not entry.get("section"):
            label = entry["cat"]
        else:
            label = ("▸ " if entry["section"] == name else "  ") + entry["section"]
        cell = g_pad(body[i] if i < len(body) else "", layout.col)
        join = "├─" if _RULE_ROW.match(cell) else "│ "
        out.append(Fixed(g_pad(g_pad(label, layout.rail_w) + join + cell, w)))
    if out:
        foot = "─" * layout.rail_w + "┴" + "─" * max(0, w - layout.rail_w - 1)
        out.append(Fixed(g_pad(foot, w)))
    return out


_EDITOR_KEYS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "pick": (("↑↓", "choose"), ("Enter", "commit"), ("Esc", "cancel")),
        "list": (
            ("a", "add"),
            ("x", "remove"),
            ("↑↓", "row"),
            ("Enter", "commit"),
            ("Esc", "cancel"),
        ),
        "num": (("↑↓", "step"), ("Enter", "commit"), ("Esc", "cancel")),
    }
)


def _keys(view: View, name: str, k: Key | None) -> list[tuple[str, str]]:
    """Return the keybar for the editor, the filter or the browsing state."""
    s, cfg = view.session, view.fixture.settings
    if s.edit:
        kind = s.edit["kind"]
        if kind in _EDITOR_KEYS:
            return list(_EDITOR_KEYS[kind])
        if kind == "check":
            order = [("J K", "order")] if k and k[1] == "o" else []
            return [
                ("space", "tick"),
                ("↑↓", "move"),
                *order,
                ("Enter", "commit"),
                ("Esc", "cancel"),
            ]
        unsets = empty_unsets(k[1]) if k else True
        empty = ("empty", "unsets") if unsets else ('""', "stores empty")
        return [("Enter", "commit"), ("Esc", "cancel"), empty]
    if s.set_typing:
        return [("type", "narrow"), ("↑↓", "field"), ("Enter", "keep"), ("Esc", "clear")]
    read_only = k is not None and editor_kind(cfg, name, k[0], k[1]) == "file"
    edit = [] if read_only else [("Enter", "edit")]
    unset = [] if read_only else [("x", "unset")]
    keys = [("↑↓", "field"), ("Tab", "section"), *edit, ("l", "layer"), *unset, ("Esc", "back")]
    if view.w >= 120:
        keys += [("i", "stack"), ("\\", "filter")]
    return keys


def render(view: View) -> list[str]:
    """Return the Settings frame, native when the effective-settings view is held."""
    if view.settings is not None:
        return list_frame(view, view.settings)
    s, cfg = view.session, view.fixture.settings
    name = sec(s, cfg)
    keys = section_keys(s, cfg)
    if s.set_key >= len(keys):
        s.set_key = max(0, len(keys) - 1)
    k = cur_key(s, cfg)
    layout = _Layout.at_width(view.w)
    rows = _with_rail(view, layout, name, _body(view, layout, name, k))
    ctx = (
        f"{len(cfg.cats)} categories · {len(cfg.names)} sections · in {cat_of(cfg, name)} ▸ {name}"
        + (" · editing" if s.edit else "")
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Settings",
        ctx=ctx,
        body=rows,
        keys=_keys(view, name, k),
    )


# ---------- the keys: the lens, the sections, the editor ----------


def _cycle(ed: dict[str, Any], field: str, key: str) -> None:
    n = len(ed[field])
    ed["idx"] = (ed["idx"] + (1 if key == "ArrowDown" else n - 1)) % n


def _edit_pick(ctx: Ctx, ed: dict[str, Any], name: str, k: Key, key: str) -> bool:
    s, cfg = ctx.s, ctx.fixture.settings
    if key in ("ArrowDown", "ArrowUp"):
        _cycle(ed, "vals", key)
        ctx.log(key, f"→ {ed['vals'][ed['idx']]}")
    elif key == "Enter":
        picked = ed["vals"][ed["idx"]]
        value: Any = (picked == "true") if k[1] == "b" else picked
        write_value(cfg, s, name, k[0], value)
        floor = inert(cfg, f"{name}.{k[0]}", value)
        ctx.log(
            "Enter",
            f"wrote {name}.{k[0]} = {show(cfg, value)} at {lens(s)}"
            + (f" · inert: floor {show(cfg, floor[0])} applies" if floor else ""),
        )
        s.edit = None
    return True


def _edit_check(ctx: Ctx, ed: dict[str, Any], name: str, k: Key, key: str) -> bool:
    s, cfg = ctx.s, ctx.fixture.settings
    opts, ticked = ed["opts"], ed["on"]
    if key in ("ArrowDown", "ArrowUp"):
        _cycle(ed, "opts", key)
    elif key == " ":
        option = opts[ed["idx"]]
        was_on = option in ticked
        if was_on:
            ticked.remove(option)
        else:
            ticked.append(option)
        ctx.log("space", ("untick " if was_on else "tick ") + f"{option} · {len(ticked)} selected")
    elif key in ("J", "K") and ed["ordered"]:
        to = ed["idx"] + (1 if key == "J" else -1)
        if 0 <= to < len(opts):
            opts[ed["idx"]], opts[to] = opts[to], opts[ed["idx"]]
            ed["idx"] = to
            ctx.log(key, "order → " + ", ".join(v for v in opts if v in ticked))
        else:
            ctx.log(key, "already at the " + ("bottom" if key == "J" else "top"))
    elif key == "Enter":
        picked = [v for v in opts if v in ticked]
        write_value(cfg, s, name, k[0], picked)
        ctx.log("Enter", f"wrote {name}.{k[0]} = {show(cfg, picked)} at {lens(s)}")
        s.edit = None
    return True


def _edit_list(ctx: Ctx, ed: dict[str, Any], name: str, k: Key, key: str) -> bool:
    s, cfg = ctx.s, ctx.fixture.settings
    items = ed["items"]
    typing = ed["text"] is not None
    if key in ("ArrowDown", "ArrowUp"):
        if typing:
            items[ed["idx"]] = ed["text"]
            ed["text"] = None
        n = max(1, len(items))
        ed["idx"] = (ed["idx"] + (1 if key == "ArrowDown" else n - 1)) % n
    elif key == "a" and not typing:
        items.insert(ed["idx"] + 1, "")
        ed["idx"] += 1
        ed["text"] = ""
        ctx.log("a", "row added · type it")
    elif key == "x" and not typing:
        if items:
            removed = items.pop(ed["idx"])
            ed["idx"] = max(0, ed["idx"] - 1)
            ctx.log("x", "removed " + (removed or _EMPTY_ITEM))
    elif key == "Enter":
        if typing:
            items[ed["idx"]] = ed["text"]
        kept = [x for x in items if x != ""]
        write_value(cfg, s, name, k[0], kept)
        ctx.log(
            "Enter",
            f"wrote {name}.{k[0]} = {show(cfg, kept)} at {lens(s)}"
            + ("" if kept else " · an empty list, not unset — x removes the key"),
        )
        s.edit = None
    elif key == "Backspace":
        start = ed["text"] if typing else (items[ed["idx"]] if ed["idx"] < len(items) else "")
        ed["text"] = start[:-1]
    elif len(key) == 1:
        ed["text"] = (ed["text"] if typing else "") + key
    return True


def _clamp(value: float, ed: dict[str, Any]) -> float:
    if ed["min"] is not None and value < ed["min"]:
        value = ed["min"]
    if ed["max"] is not None and value > ed["max"]:
        value = ed["max"]
    return value


def _commit_num(ctx: Ctx, ed: dict[str, Any], name: str, k: Key) -> None:
    s, cfg = ctx.s, ctx.fixture.settings
    if ed["text"].strip() == "":
        unset_value(cfg, s, name, k[0])
        ctx.log("Enter", f"unset {name}.{k[0]} at {lens(s)}")
        return
    parsed = parse_num(ed["text"], is_float=k[1] == "f")
    if parsed is None:
        ctx.log("Enter", "not a number — nothing written")
        return
    value = _clamp(parsed, ed)
    write_value(cfg, s, name, k[0], value)
    span = (
        f" · range {js_str(ed['min'])}{_RANGE_SEP}{js_str(ed['max'])}"
        if ed["min"] is not None
        else ""
    )
    ctx.log("Enter", f"wrote {name}.{k[0]} = {js_str(value)} at {lens(s)}{span}")


def _edit_num(ctx: Ctx, ed: dict[str, Any], name: str, k: Key, key: str) -> bool:
    if key in ("ArrowUp", "ArrowDown"):
        step = 0.05 if k[1] == "f" else 1
        current = parse_num(ed["text"] or "0", is_float=True) or 0
        moved = _clamp(current + (step if key == "ArrowUp" else -step), ed)
        rounded = js_round(moved * 100) / 100 if k[1] == "f" else js_round(moved)
        ed["text"] = js_str(rounded)
    elif key == "Enter":
        _commit_num(ctx, ed, name, k)
        ctx.s.edit = None
    elif key == "Backspace":
        ed["text"] = ed["text"][:-1]
    elif _NUM_KEY.match(key):
        ed["text"] += key
    return True


def _edit_text(ctx: Ctx, ed: dict[str, Any], name: str, k: Key, key: str) -> bool:
    s, cfg = ctx.s, ctx.fixture.settings
    if key == "Enter":
        text = ed["text"]
        if text.strip() == "" and empty_unsets(k[1]):
            unset_value(cfg, s, name, k[0])
            ctx.log("Enter", f"unset {name}.{k[0]} at {lens(s)}")
        else:
            write_value(cfg, s, name, k[0], text)
            ctx.log("Enter", f"wrote {name}.{k[0]} = {show(cfg, text)} at {lens(s)}")
        s.edit = None
        return True
    if key == "Backspace":
        ed["text"] = ed["text"][:-1]
        return True
    if len(key) == 1:
        ed["text"] += key
        return True
    # any other key falls through to the dispatcher, as the text editor always allowed
    return False


_EDITORS: Mapping[str, Callable[[Ctx, dict[str, Any], str, Key, str], bool]] = MappingProxyType(
    {"pick": _edit_pick, "check": _edit_check, "list": _edit_list, "num": _edit_num}
)


def _seam_edit(ctx: Ctx, key: str) -> bool:
    """Route a key to the open editor; Escape cancels any editor without writing."""
    s, cfg = ctx.s, ctx.fixture.settings
    if key == "Escape":
        s.edit = None
        ctx.log("Esc", "edit cancelled · nothing written")
        return True
    k = cur_key(s, cfg)
    ed = s.edit
    if k is None or ed is None:
        return True
    editor = _EDITORS.get(ed["kind"], _edit_text)
    return editor(ctx, ed, sec(s, cfg), k, key)


def _open_editor(ctx: Ctx) -> None:
    """Open the editor the key under the cursor takes, seeded with the lens's value."""
    s, cfg = ctx.s, ctx.fixture.settings
    k = cur_key(s, cfg)
    if not k:
        return
    name = sec(s, cfg)
    kind = editor_kind(cfg, name, k[0], k[1])
    field = state(cfg, name, k[0], at(k, 2), lens(s))
    here = _stored_at(cfg, f"{name}.{k[0]}", lens(s))
    base = here if here is not UNDEFINED else field.value
    where = f"writes to {lens(s)}"
    if kind == "file":
        ctx.log("Enter", f"{k[0]} merges by id · edit it in {layer_where(cfg, lens(s))}")
    elif kind == "pick":
        values = ["true", "false"] if k[1] == "b" else str(at(k, 3) or "").split(" · ")
        idx = values.index(js_str(base)) if js_str(base) in values else 0
        s.edit = {"kind": "pick", "vals": values, "idx": idx, "text": None}
        ctx.log("Enter", f"choosing {name}.{k[0]} · {len(values)} values · {where}")
    elif kind == "check":
        catalog = choices(cfg, name, k[0]) or []
        ticked = list(base) if isinstance(base, list) else []
        ordered = k[1] == "o"
        drawn = (
            [v for v in ticked if v in catalog] + [v for v in catalog if v not in ticked]
            if ordered
            else list(catalog)
        )
        s.edit = {"kind": "check", "opts": drawn, "on": ticked, "idx": 0, "text": None}
        s.edit["ordered"] = ordered
        ctx.log("Enter", f"ticking {name}.{k[0]} · {where}")
    elif kind == "list":
        items = list(base) if isinstance(base, list) else []
        s.edit = {"kind": "list", "items": items, "idx": 0, "text": None}
        ctx.log("Enter", f"editing the list {name}.{k[0]} · {where}")
    elif kind == "num":
        _open_num(ctx, name, k, base)
    else:
        s.edit = {"kind": "text", "text": as_text(base)}
        shadowed = " · shadowed, and the write still lands here" if field.glyph == "≠" else ""
        ctx.log("Enter", f"editing {name}.{k[0]} at {lens(s)}{shadowed}")


def _open_num(ctx: Ctx, name: str, k: Key, base: Any) -> None:
    s = ctx.s
    bounds = str(at(k, 3) or "").split(_RANGE_SEP)
    low = parse_num(bounds[0], is_float=True)
    high = parse_num(bounds[1], is_float=True) if len(bounds) > 1 else None
    text = "" if base is None or base is UNDEFINED else js_str(base)
    s.edit = {"kind": "num", "text": text, "min": low, "max": high}
    span = f" · range {at(k, 3)}" if at(k, 3) else ""
    ctx.log("Enter", f"editing {name}.{k[0]}{span} · writes to {lens(s)}")


def _seam_filter(ctx: Ctx, key: str) -> bool:
    """Handle a key while the route's own filter field is open."""
    s, cfg = ctx.s, ctx.fixture.settings
    if key == "Escape":
        s.set_typing = False
        s.typing = False
        s.set_filter = ""
        ctx.log("Esc", "filter cleared")
    elif key == "Enter":
        s.set_typing = False
        s.typing = False
        shown = len(section_keys(s, cfg))
        total = len(cfg.sections.get(sec(s, cfg), []))
        kept = "filter kept · " if s.set_filter else "no filter · "
        ctx.log("Enter", f"{kept}{shown} of {total} keys")
    elif key == "Backspace":
        s.set_filter = s.set_filter[:-1]
        s.set_key = 0
    elif key in ("ArrowDown", "ArrowUp"):
        keys = section_keys(s, cfg)
        if keys:
            step = 1 if key == "ArrowDown" else -1
            s.set_key = max(0, min(len(keys) - 1, s.set_key + step))
    elif len(key) == 1:
        s.set_filter += key
        s.set_key = 0
    return True


def _seam_browse(ctx: Ctx, key: str, shift: bool) -> bool:
    """Handle the lens, section, cursor and toggle keys while browsing."""
    s, cfg = ctx.s, ctx.fixture.settings
    if key in ("l", "L"):
        layers = list(cfg.writable)
        idx = layers.index(lens(s)) if lens(s) in layers else -1
        back = key == "L" or shift
        s.lens = layers[(idx + (len(layers) - 1 if back else 1)) % len(layers)]
        ctx.log("L" if back else "l", f"layer → {s.lens} · {layer_where(cfg, s.lens)}")
        return True
    if key == "Tab":
        n = len(cfg.section_order)
        s.set_sec = (s.set_sec + (n - 1 if shift else 1)) % n
        s.set_key = 0
        s.set_filter = ""
        name = sec(s, cfg)
        ctx.log("Shift+Tab" if shift else "Tab", f"{cat_of(cfg, name)} ▸ {name}")
        return True
    if key in ("ArrowDown", "ArrowUp"):
        keys = section_keys(s, cfg)
        if not keys:
            ctx.log(key, f"{sec(s, cfg)} holds no keys")
            return True
        s.set_key = max(0, min(len(keys) - 1, s.set_key + (1 if key == "ArrowDown" else -1)))
        k = cur_key(s, cfg)
        ctx.log(key, f"{sec(s, cfg)}.{k[0] if k else ''}")
        return True
    if key == "Enter":
        _open_editor(ctx)
        return True
    return False


def _seam_value(ctx: Ctx, key: str) -> bool:
    """Toggle a boolean on space, unset the lens's value on ``x``, open the stack on ``i``."""
    s, cfg = ctx.s, ctx.fixture.settings
    k = cur_key(s, cfg)
    name = sec(s, cfg)
    if key == " " and k and k[1] == "b":
        flipped = not state(cfg, name, k[0], at(k, 2), lens(s)).value
        write_value(cfg, s, name, k[0], flipped)
        ctx.log("space", f"{name}.{k[0]} = {js_str(flipped)} at {lens(s)}")
        return True
    if key == "x" and k:
        fell = fallback(cfg, name, k[0], at(k, 2), lens(s))
        if not fell:
            ctx.log("x", f"not set at {lens(s)} — nothing to unset")
        else:
            unset_value(cfg, s, name, k[0])
            ctx.log("x", f"unset {name}.{k[0]} · falls back to {fell[0]} {show(cfg, fell[1])}")
        return True
    if key == "i":
        go(ctx, "settings.stack", "stack")
        return True
    return False


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Handle the route's keys: the editor first, then the filter, then browsing."""
    s = ctx.s
    if s.overlay:
        return False
    if s.edit is not None:
        return _seam_edit(ctx, key)
    # the route owns its filter: the dispatcher's filter writes a field this route never reads
    if key == "\\" and not s.set_typing:
        s.set_typing = True
        s.typing = True
        ctx.log("\\\\", f"filter · type to narrow {sec(s, ctx.fixture.settings)} · Esc clears")
        return True
    if s.set_typing:
        return _seam_filter(ctx, key)
    return _seam_browse(ctx, key, shift) or _seam_value(ctx, key)
