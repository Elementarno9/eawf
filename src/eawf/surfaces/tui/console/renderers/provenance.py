"""The native settings frames: what is in force, which layer set it, and editing it at a layer.

A settings value drawn without its layer is a value an operator cannot act on: the layer
that wins is the only place editing it has any effect, and a repo value that a local file
quietly overrides looks exactly like one that is in force. Both frames here answer that
from the effective-settings read model rather than from a catalog of the console's own.

The route draws the six categories and their catalog sections as a rail, the selected
section's keys with their effective value and the layer behind it, the writable layer
chain with the lens marked, and one readout for the key under the cursor. The stack card
draws one key's nine layers, lowest first, from the same read, so the card cannot
disagree with the row it was opened from.

An edit is made under the lens and previewed before it is sent: the key, the layer it
writes to, the values the catalog allows and what the effective value becomes, including
when a higher layer shadows the write so that it changes nothing in force. The console
writes no file; the edit is a request to the daemon's layered-config verbs, and the value
shown afterwards is the daemon's re-read, never the value the console asked for.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from eawf.kernel.config.layered import LAYER_ORDER, Layer
from eawf.kernel.config.registry.leaf_catalog import pair_siblings
from eawf.kernel.projection.settings import (
    LAYER_PLACES,
    LENS_LAYERS,
    EffectiveSettingsView,
    SettingsLeaf,
)
from eawf.surfaces.tui.console.cards import setting_token
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Grid,
    LeadReceded,
    Lensed,
    Receded,
    View,
    bar,
    build,
    needs_count,
    scope_label,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import keybar, pick, route_pairs
from eawf.surfaces.tui.console.mutation import open_setting
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.operations import SettingRequest
from eawf.surfaces.tui.console.renderers import setting_editors as editors
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, TRUTH
from eawf.surfaces.tui.console.width import cell_len, pad

#: The route keybar while browsing, in the order the packet states it.
ROUTE_KEYS: tuple[tuple[str, str], ...] = tuple(
    pick("settings", "field", "section", "edit", "layer", "unset", "back")
)

#: The stack card's keybar: it moves between layers and closes, and edits nothing.
STACK_KEYS: tuple[tuple[str, str], ...] = route_pairs("settings.stack")

#: The pairs the route adds from the packet's wide frame on: the stack card and the filter.
WIDE_KEYS: tuple[tuple[str, str], ...] = tuple(pick("settings", "stack", "filter"))

#: The keybar each editor kind shows while it is open.
_EDIT_KEYS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "pick": (("↑↓", "choose"), ("Enter", "write"), ("Esc", "cancel")),
        "num": (("↑↓", "step"), ("Enter", "write"), ("Esc", "cancel")),
        "text": (("type", "value"), ("Enter", "write"), ("Esc", "cancel")),
        "offer": (("↑↓", "layer"), ("Enter", "edit there"), ("Esc", "cancel")),
    }
)

#: The keybar while the filter field takes the typing.
_FILTER_KEYS: tuple[tuple[str, str], ...] = (
    ("type", "narrow"),
    ("↑↓", "field"),
    ("Enter", "keep"),
    ("Esc", "clear"),
)

#: The rail's width below and in the wide layout, before a long name widens it.
RAIL_NARROW = 14
RAIL_WIDE = 17

#: The gutter glyph per lens relation: set at the lens and in force, set at the lens and
#: shadowed, inherited from another layer, and stated by no layer but the defaults.
GLYPH_WINS = "="
GLYPH_SHADOWED = "≠"
GLYPH_INHERITS = "·"
GLYPH_DEFAULT = "–"  # noqa: RUF001

#: The FROM cell of a key no code reads, listed only while a file still states it: the
#: four glyphs keep their meaning for it, and the word says what sets it apart.
UNREAD = "unread"

#: How a layer that states nothing for a key reads in the stack card.
NOT_STATED = "–"  # noqa: RUF001

#: The catalog shapes the console edits; a mapping or free-form value is edited in its file.
_PICKED = frozenset({"bool", "literal"})
_TYPED = frozenset({"int", "float", "str", "list_str"})
_NUMERIC = frozenset({"int", "float"})

#: What one arrow press moves a float by.
_FLOAT_STEP = 0.05

#: The stack card's title and the note under it: the card explains, it never edits.
STACK_TITLE = "STACK · nine layers"
STACK_FOOT = "Read only · a value is changed through the lens, never from here."

# the caret keeps a cell of its own before the layer name, as every list's does
_STACK = Grid([12, 17, 12, 0])
_CHAIN_SEP = " › "  # noqa: RUF001


def _crumb(scope: str) -> str:
    """Return the settings breadcrumb; the stack card is an overlay and keeps it."""
    return " " + CRUMB_SEP.join([BRAND, scope, "Settings"])


def lens(session: Session) -> Layer:
    """Return the layer an edit writes to, the repo layer until the lens is moved."""
    return Layer(session.lens) if session.lens in LENS_LAYERS else Layer.REPO


def placement(
    session: Session, settings: EffectiveSettingsView
) -> tuple[str, int, SettingsLeaf | None]:
    """Return the selected section, the key offset and the leaf under the cursor, clamped.

    The offsets are published back into the session so a re-read that removed keys
    leaves the cursor on a key that exists rather than past the end.
    """
    sections = settings.sections()
    if not sections:
        return "", 0, None
    session.set_sec = min(max(session.set_sec, 0), len(sections) - 1)
    section = sections[session.set_sec]
    keys = section_keys(settings, session, section)
    session.set_key = min(max(session.set_key, 0), max(len(keys) - 1, 0))
    return section, session.set_key, keys[session.set_key] if keys else None


def _rank(layer: Layer | None) -> int:
    return -1 if layer is None else LAYER_ORDER.index(layer.value)


def glyph(leaf: SettingsLeaf, at: Layer) -> str:
    """Return the key's gutter glyph as the lens layer ``at`` sees it."""
    if leaf.stated_at(at) is not None:
        return GLYPH_WINS if leaf.source_layer is at else GLYPH_SHADOWED
    if leaf.source_layer not in (None, Layer.BUILT_IN):
        return GLYPH_INHERITS
    return GLYPH_DEFAULT


def value_text(leaf: SettingsLeaf, *, short: bool = False) -> str:
    """Return the key's effective value cell; a denied key keeps the denied token.

    Args:
        leaf: The key whose value in force is drawn.
        short: Draw the slot and its state word without the reason, as a key row does;
            the readout under the table carries the reason for the key under the cursor.
    """
    cell = value_cell(leaf.effective)
    shown = f"{cell.slot} {cell.word}".rstrip() if short else cell.full
    return f"{TRUTH['denied'].unicode} denied · {shown}" if leaf.deny_chain else shown


def why(leaf: SettingsLeaf, at: Layer) -> str:
    """Return one sentence on how the lens layer ``at`` relates to the value in force."""
    here = leaf.stated_at(at)
    if here is not None and leaf.source_layer is at:
        return f"set at {at}, and {at} wins"
    if here is not None:
        held = value_text(leaf)
        return f"set at {at} as {here} · the {leaf.source_layer} layer wins, holding {held}"
    if leaf.source_layer is None:
        return "no layer states this key, not even the defaults"
    return f"not set at {at} · inherits {value_text(leaf)} from the {leaf.source_layer} layer"


def _unwritable(leaf: SettingsLeaf) -> str:
    """Return why no layer may write ``leaf``, in the words its catalog standing calls for."""
    match leaf.consumer_kind:
        case None:
            return f"{leaf.key} is off the catalog · no eawf code reads it · x removes it"
        case "deprecated":
            return f"{leaf.key} is deprecated · no longer read · x removes it from this layer"
        case "reserved":
            return f"{leaf.key} is reserved · not read yet · kept for a planned feature"
        case _:
            return f"{leaf.key} is locked · it is set by code and no layer may write it"


def refusal(leaf: SettingsLeaf, at: Layer) -> str:
    """Return why the key cannot be edited at ``at``; empty when it can."""
    if not leaf.editable_at:
        return _unwritable(leaf)
    if leaf.value_type not in _PICKED | _TYPED and leaf.editor is None:
        where = LAYER_PLACES[at][1]
        return f"{leaf.key} is a {leaf.value_type or 'free-form'} value · edit it in {where}"
    if at not in leaf.editable_at:
        allowed = ", ".join(layer.value for layer in leaf.editable_at)
        return f"{leaf.key} is not editable at {at} · it is editable at {allowed}"
    return ""


def offered(leaf: SettingsLeaf, lens_layers: Sequence[Layer]) -> tuple[Layer, ...]:
    """Return the file layers that may hold ``leaf``, the highest precedence first."""
    return tuple(layer for layer in reversed(lens_layers) if layer in leaf.editable_at)


def after_write(leaf: SettingsLeaf, at: Layer, shown: str) -> str:
    """Return what the effective value becomes once ``shown`` is written at ``at``."""
    if _rank(leaf.source_layer) > _rank(at):
        return (
            f"changes nothing in force · the {leaf.source_layer} layer above {at} "
            f"holds {value_text(leaf)}"
        )
    return f"{shown} from {at} becomes the value in force"


def after_unset(leaf: SettingsLeaf, at: Layer) -> str:
    """Return what the key falls back to once ``at`` stops stating it."""
    if leaf.source_layer is not at:
        return f"changes nothing in force · {leaf.source_layer or 'no'} layer holds the value"
    below = [entry for entry in leaf.stack if not entry.wins]
    if not below:
        return "no layer states it afterwards"
    return f"falls back to {below[-1].layer} {below[-1].value}"


# ---------- the route frame ----------


def _name(leaf: SettingsLeaf, section: str) -> str:
    """Return the key as its section's rows name it, without the section prefix."""
    return leaf.key.split(".", 1)[1] if leaf.key.startswith(f"{section}.") else leaf.key


def section_keys(
    settings: EffectiveSettingsView, session: Session, section: str
) -> tuple[SettingsLeaf, ...]:
    """Return the section's keys the route filter leaves, every key when no filter is set."""
    keys = settings.keys_of(section)
    query = session.set_filter.lower()
    return tuple(leaf for leaf in keys if query in _name(leaf, section).lower()) if query else keys


def _rail_entries(settings: EffectiveSettingsView) -> list[tuple[str, str | None]]:
    """Return the rail's rows: each category heading, then each of its sections."""
    rows: list[tuple[str, str | None]] = []
    for category in settings.rail:
        rows.append((category.name, None))
        rows.extend((category.name, section) for section in category.sections)
    return rows


def rail_width(settings: EffectiveSettingsView, *, wide: bool) -> int:
    """Return the rail's width: the packet's 14 or 17 cells, widened so no name is clipped."""
    names = [s for c in settings.rail for s in c.sections] + [c.name for c in settings.rail]
    floor = RAIL_WIDE if wide else RAIL_NARROW
    return max(floor, max((cell_len(name) for name in names), default=0) + 2)


def _chain(layers: Sequence[Layer]) -> str:
    return _CHAIN_SEP.join(layer.value for layer in layers)


def _setters(leaf: SettingsLeaf | None) -> dict[str, str]:
    """Return the surface each lens layer that sets ``leaf`` is drawn in: winner, loser."""
    if leaf is None:
        return {}
    return {
        entry.layer.value: "ok" if entry.layer is leaf.source_layer else "warn"
        for entry in leaf.stack
        if entry.layer in LENS_LAYERS
    }


def _clip(text: str, n: int) -> str:
    return pad(text, n) if cell_len(text) > n else text


def pick_values(leaf: SettingsLeaf) -> tuple[str, ...]:
    """Return the values a pick editor offers for ``leaf``; empty for a key that is typed."""
    if leaf.value_type == "bool":
        return ("true", "false")
    return tuple(leaf.allowed) if leaf.value_type == "literal" else ()


def _held(leaf: SettingsLeaf, at: Layer) -> str | None:
    """Return the value the lens layer ``at`` sees: its own, else the one it inherits."""
    return leaf.stated_at(at) or leaf.effective.value


def _chooser(leaf: SettingsLeaf, at: Layer, edit: dict[str, Any] | None) -> list[str]:
    """Return the pick list: one row per value, ``●`` on the stored one, ``▸`` on the pointed.

    The stored value keeps its dot while another is pointed at, so choosing never hides
    what is in force at the lens. Every row keeps the caret's slot, so the values stay in
    one column wherever the caret is.
    """
    held = _held(leaf, at)
    rows: list[str] = []
    for i, value in enumerate(pick_values(leaf)):
        pointer = "▸ " if edit is not None and edit["idx"] == i else "  "
        rows.append(f" {pointer}{'●' if value == held else '○'} {value}")
    return rows


def _readout(
    leaf: SettingsLeaf | None, session: Session, section: str, col: int, cap: int
) -> list[str]:
    """Return the docked notes: the key, its meaning, its values, then the edit or its reason.

    The meaning wraps over the rows the key window leaves; it gives way first, so the
    values and the preview of a write are never the rows that are cut.
    """
    if leaf is None:
        hint = f" nothing matches \\{session.set_filter}" if session.set_filter else ""
        return [hint or " this section holds no keys"]
    wrapped = textwrap.wrap(
        leaf.meaning or "the catalog states no meaning for this key", max(8, col - 4)
    )
    meaning = [f"{' ' if i == 0 else '   '}{line}" for i, line in enumerate(wrapped)]
    held = session.edit
    edit = held if held is not None and held.get("key") == leaf.key else None
    tail = _tail(leaf, session, edit, col, max(1, cap - 3))
    lines = [_head(leaf, section), *meaning[: max(0, cap - 1 - len(tail))], *tail]
    return [_clip(line, col) for line in lines[: max(1, cap)]]


def span(value_range: tuple[float | None, float | None]) -> str:
    """Return a catalog range as the readout states it: both ends, or the one that is set."""
    low, high = value_range
    if low is not None and high is not None:
        return f"{low:g} to {high:g}"
    return f"at least {low:g}" if low is not None else f"at most {high:g}"


def _head(leaf: SettingsLeaf, section: str) -> str:
    """Return the readout's first line: the key, its shape, and what it may hold."""
    shape = leaf.value_type or "outside the catalog"
    if leaf.editor is not None:
        detail = f" · {editors.head(leaf)}"
    elif leaf.value_range is not None:
        detail = f" · {span(leaf.value_range)}"
    else:
        detail = f" · one of {' | '.join(leaf.allowed)}" if leaf.allowed else ""
    return f" {_name(leaf, section)} · {shape}{detail}"


def edit_layer(session: Session, edit: Mapping[str, Any]) -> Layer:
    """Return the layer an open edit writes to: the one it was opened at, else the lens."""
    return Layer(edit["at"]) if "at" in edit else lens(session)


def _offer_lines(leaf: SettingsLeaf, edit: Mapping[str, Any], at: Layer) -> list[str]:
    """Return the layer offer: the layers that can hold the key, the pointed one first."""
    layers = [Layer(layer) for layer in edit["layers"]]
    names = ", ".join(layer.value for layer in layers)
    rows = [f" {at} cannot hold {leaf.key.rsplit('.', 1)[-1]} · only {names} can"]
    for i, layer in enumerate(layers):
        caret = "▸" if i == edit["idx"] else " "
        rows.append(f" {caret} write it at {layer} · {LAYER_PLACES[layer][1]}")
    rows.append(f"   Esc keeps the lens at {at} and writes nothing")
    return rows


def _composite_tail(
    leaf: SettingsLeaf, edit: dict[str, Any], to: Layer, col: int, room: int
) -> list[str]:
    """Return an open list or mapping editor's rows, then where it writes and what follows."""
    write = editors.written(edit)
    if write.reason:
        after = f"nothing yet · {write.reason}"
    elif write.unset:
        stated = leaf.stated_at(to) is not None
        after = after_unset(leaf, to) if stated else f"nothing to unset · {to} states no ladder"
    else:
        after = after_write(leaf, to, editors.shown(edit, write))
    return [
        *editors.lines(edit, col, room),
        f" WRITES   {leaf.key} at {to} · {LAYER_PLACES[to][1]}",
        f" AFTER    {after}",
    ]


def _tail(
    leaf: SettingsLeaf, session: Session, edit: dict[str, Any] | None, col: int, room: int
) -> list[str]:
    """Return the readout under the meaning: the value chooser or editor, or why it stands."""
    at = lens(session)
    if edit is None:
        return [*_chooser(leaf, at, None), f" {why(leaf, at)}"]
    if edit["kind"] == "offer":
        return _offer_lines(leaf, edit, at)
    to = edit_layer(session, edit)
    if edit["kind"] in editors.COMPOSITE:
        return _composite_tail(leaf, edit, to, col, room)
    tail = _chooser(leaf, to, edit)
    if edit["kind"] == "pick":
        shown = edit["vals"][edit["idx"]]
    else:
        step = "      ↑↓ steps" if edit["kind"] == "num" else ""
        tail.append(f" typing   {edit['text']}▏{step}")
        shown = edit["text"] or '""'
    tail.append(f" WRITES   {leaf.key} at {to} · {LAYER_PLACES[to][1]}")
    tail.append(f" AFTER    {after_write(leaf, to, shown)}")
    return tail


def _key_width(col: int) -> int:
    """Return the key column's width: half the body, within readable bounds."""
    return min(34, max(18, col // 2 - 4))


def _key_rows(
    keys: Sequence[SettingsLeaf], session: Session, section: str, col: int, room: int
) -> list[str]:
    """Return the section's key rows windowed onto the cursor, with the window count."""
    at = lens(session)
    if not keys:
        if session.set_filter:
            return [f"  nothing matches \\{session.set_filter}"]
        return ["  this section holds no keys"]
    key_w = _key_width(col)
    from_w = 10
    value_w = col - key_w - from_w - 5
    take = max(1, min(len(keys), room - (1 if len(keys) > room else 0)))
    top = max(0, min(session.set_key - take // 2, len(keys) - take))
    rows: list[str] = []
    for index, leaf in enumerate(keys[top : top + take], start=top):
        caret = "▸" if index == session.set_key else " "
        source = leaf.source_layer.value if leaf.source_layer is not None else NOT_STATED
        if leaf.unread:
            source = UNREAD
        line = (
            f"{caret} {glyph(leaf, at)} "
            + pad(_name(leaf, section), key_w - 1)
            + " "
            + pad(value_text(leaf, short=True), value_w)
            + " "
            + pad(source, from_w)
        )
        rows.append(pad(line, col))
    if take < len(keys):
        rows.append(f"  {top + 1}-{top + take} of {len(keys)}")
    return rows


def _filter_hint(session: Session) -> str:
    if not (session.set_filter or session.set_typing):
        return ""
    return f"   \\{session.set_filter}" + ("▏" if session.set_typing else "")


def _body(
    view: View, settings: EffectiveSettingsView, section: str, leaf: SettingsLeaf | None, col: int
) -> list[str]:
    """Return the body column: the section head and chain, the keys, then the readout."""
    session, avail = view.session, view.h - 5
    at = lens(session)
    keys = section_keys(settings, session, section)
    total = len(settings.keys_of(section))
    count = "" if len(keys) == total else f"   {len(keys)} of {total}"
    head = f" {section.upper()}{count}{_filter_hint(session)}"
    chain = _chain(settings.lens_layers)
    setters = _setters(leaf)
    body: list[str] = (
        [
            Lensed(
                pad(head + " " * (col - cell_len(head) - cell_len(chain)) + chain, col),
                lens=at.value,
                layers=setters,
            )
        ]
        if cell_len(head) + 2 + cell_len(chain) <= col
        else [pad(head, col), Lensed(pad(f" {chain}", col), lens=at.value, layers=setters)]
    )
    key_w = _key_width(col)
    body.append(pad(pad("    KEY", key_w + 4) + pad("VALUE", col - key_w - 14) + "FROM", col))
    # the key list keeps three rows however tall the notes grow
    readout = _readout(leaf, session, section, col, max(2, avail - len(body) - 1 - 3))
    room = max(1, avail - len(body) - 1 - len(readout))
    body += _key_rows(keys, session, section, col, room)
    body += ["" for _ in range(avail - len(body) - 1 - len(readout))]
    body.append("─" * col)
    body += readout
    return body


def _with_rail(
    settings: EffectiveSettingsView,
    section: str,
    body: Sequence[str],
    w: int,
    rows: int,
    *,
    width: int,
    editing: bool,
) -> list[str]:
    """Return the body beside the rail, the rail windowed onto the selected section.

    The rail is closed by its own rule before the keybar, so the vertical line never
    reads as running into the keys. While an editor is open the rail and the key list do
    not own the arrows, so they recede and only the readout below the rule stays lit.
    """
    rail = _rail_entries(settings)
    col = w - width - 2
    selected = next((i for i, (_c, s) in enumerate(rail) if s == section), 0)
    top = max(0, min(selected - rows // 2, max(0, len(rail) - rows)))
    out: list[str] = []
    readout = next((i for i, cell in enumerate(body) if cell.startswith("─")), len(body))
    for i in range(rows):
        entry = rail[top + i] if top + i < len(rail) else None
        if entry is None:
            label = ""
        elif entry[1] is None:
            label = entry[0].upper()
        else:
            label = ("▸ " if entry[1] == section else "  ") + entry[1]
        cell = body[i] if i < len(body) else ""
        join = "├─" if cell.startswith("─") else "│ "
        line = pad(pad(label, width) + join + pad(cell, col), w)
        if editing:
            out.append(Receded(line) if i < readout else LeadReceded(line))
        elif isinstance(cell, Lensed):
            out.append(Lensed(line, lens=cell.lens, layers=cell.layers))
        else:
            out.append(Fixed(line))
    out.append(Fixed("─" * width + "┴" + "─" * (w - width - 1)))
    return out


def _keys(view: View) -> tuple[tuple[str, str], ...]:
    """Return the keybar for the open editor, the filter field or browsing."""
    session = view.session
    edit = session.edit
    if edit is not None:
        return editors.keys(edit) if edit["kind"] in editors.COMPOSITE else _EDIT_KEYS[edit["kind"]]
    if session.set_typing:
        return _FILTER_KEYS
    return ROUTE_KEYS + (WIDE_KEYS if view.wide else ())


def settings_frame(view: View, settings: EffectiveSettingsView) -> list[str]:
    """Return the Settings route: the category rail, the section's keys and the readout.

    Args:
        view: The render being built; its session carries the section, key and lens.
        settings: The effective-settings read model.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    scope = scope_label(view, settings.header.scope_id)
    section, _offset, leaf = placement(session, settings)
    outside = len(settings.uncatalogued())
    ctx = (
        f" {len(settings.rail)} categories · {plural(len(settings.sections()), 'section')}"
        f" · in {settings.category_of(section).upper()} ▸ {section}"
        + (" · editing" if session.edit is not None else "")
        + (
            f" · {group(outside)} {'leaf' if outside == 1 else 'leaves'} off the catalog"
            if outside
            else ""
        )
    )
    rail = rail_width(settings, wide=view.wide)
    col = w - rail - 2
    body = _body(view, settings, section, leaf, col)
    rows: list[str] = [
        header_row(session, crumb=_crumb(scope), scope=scope, needs=needs_count(view), w=w),
        _clip(ctx, w),
        bar(w),
        *_with_rail(
            settings, section, body, w, view.h - 5, width=rail, editing=session.edit is not None
        ),
    ]
    keys = _keys(view)
    if session.edit is None and (leaf is None or leaf.stated_at(lens(session)) is None):
        # unset removes the lens layer's own value, so with none there it has nothing to do
        keys = tuple(pair for pair in keys if pair[0] != "x")
    if session.edit is None and len(section_keys(settings, session, section)) < 2:
        # a section showing one key has no other field for the arrows to step to
        keys = tuple(pair for pair in keys if pair[1] != "field")
    return build(view, rows, keybar(keys, w))


# ---------- the stack card ----------


def _on_lens(leaf: SettingsLeaf, at: Layer) -> str:
    """Return one sentence on what the lens layer contributes to the key."""
    here = leaf.stated_at(at)
    if here is None:
        source = leaf.source_layer.value if leaf.source_layer is not None else "no layer"
        return f"{at} sets nothing here; the value comes from {source}."
    if leaf.source_layer is at:
        return f"{at} sets {here} and wins."
    return f"{at} sets {here}, and the {leaf.source_layer} layer above it wins."


def _tier_two(leaf: SettingsLeaf) -> list[str]:
    """Return the stack's second tier: only the parts of the field tuple that are stated."""
    rows: list[str] = []
    if leaf.deny_chain:
        rows.append(f"DENIED BY  {' › '.join(leaf.deny_chain)}")  # noqa: RUF001
    if leaf.constraint_chain:
        rows.append(f"CONSTRAINED BY  {' › '.join(leaf.constraint_chain)}")  # noqa: RUF001
    if leaf.capability_requirement is not None:
        state = leaf.certification_state or f"{TRUTH['unknown'].unicode} certification unknown"
        rows.append(f"NEEDS      {leaf.capability_requirement} · {state}")
    if leaf.secret_ref is not None:
        rows.append(f"SECRET     {leaf.secret_ref} · the value never renders")
    return rows


def _stack_lines(
    leaf: SettingsLeaf, session: Session, inner: int
) -> tuple[list[str], list[str], list[str]]:
    """Return the card's three blocks: the key lines, the layer ladder and the reading."""
    at = lens(session)
    shape = leaf.value_type or "outside the catalog"
    allowed = f" · one of {' | '.join(leaf.allowed)}" if leaf.allowed else ""
    key_lines = [f"KEY        {leaf.key}", f"TYPE       {shape}{allowed}"]
    ladder = [_STACK.head(["LAYER", "VALUE", "KIND", "WHERE"])]
    for index, name in enumerate(LAYER_ORDER):
        layer = Layer(name)
        kind, where = LAYER_PLACES[layer]
        stated = leaf.stated_at(layer)
        value = (
            stated
            if stated is None or not leaf.deny_chain
            else f"{TRUTH['denied'].unicode} {stated}"
        )
        cells = [layer.value, value or NOT_STATED, kind.value, where]
        ladder.append(_STACK.row(cells, index == session.sel, inner))
    source = leaf.source_layer.value if leaf.source_layer is not None else "no layer"
    reading = [
        f"WINNING    {source} {value_text(leaf) if leaf.source_layer else ''}".rstrip(),
        f"LENS       {at} · the file layer an edit on the Settings route writes to",
        f"LENS SETS  {_on_lens(leaf, at)}",
        *_tier_two(leaf),
    ]
    return key_lines, ladder, reading


def stack_frame(view: View, settings: EffectiveSettingsView) -> list[str]:
    """Return the stack card of the key under the route's cursor: all nine layers, boxed.

    The card is drawn over the route as a read-only overlay: its lines give way before
    a layer does, the key's two lines folding into one and then the note under the box
    going, so every layer and every stated second-tier field fits at 80 by 24.

    Args:
        view: The render being built; its session carries the key the card opened on
            and, in ``sel``, the layer row under the cursor.
        settings: The effective-settings read model, the same one the route drew from.

    Returns:
        The full frame, keybar last; the route frame when the section holds no key.
    """
    session, w, h = view.session, view.w, view.h
    scope = scope_label(view, settings.header.scope_id)
    section, _offset, leaf = placement(session, settings)
    if leaf is None:
        return settings_frame(view, settings)
    session.sel = min(max(session.sel, 0), len(LAYER_ORDER) - 1)
    at = lens(session)
    source = leaf.source_layer.value if leaf.source_layer is not None else "no layer"
    inner = w - 4
    key_lines, ladder, reading = _stack_lines(leaf, session, inner)
    pre = [f"  {section.upper()} · {_name(leaf, section)}"]
    foot = [f" {STACK_FOOT}"]
    folded = [f"{key_lines[0]} · {key_lines[1].split(maxsplit=1)[1]}"]
    # header, context, rule, the box's two borders and the keybar take six rows; what
    # gives way first is the key's second line, then the spacer, the note and the pre-line
    room = h - 6
    for keys_now, gap, pre_now, foot_now in (
        (key_lines, [""], pre, foot),
        (folded, [""], pre, foot),
        (folded, [], pre, foot),
        (folded, [], pre, []),
        (folded, [], [], []),
    ):
        lines = [*keys_now, *ladder, *gap, *reading]
        if len(pre_now) + len(lines) + len(foot_now) <= room:
            break
    top = f"┌─ {STACK_TITLE} "
    rows: list[str] = [
        header_row(session, crumb=_crumb(scope), scope=scope, needs=needs_count(view), w=w),
        _clip(
            f" LAYER ▸ {at} · in force from {source} · effective revision "
            f"{group(settings.header.projection_revision)}",
            w,
        ),
        bar(w),
        *(Fixed(pad(row, w)) for row in pre_now),
        Fixed(top + "─" * max(0, w - cell_len(top) - 1) + "┐"),
        *(Fixed(f"│ {pad(line, inner)} │") for line in lines),
        Fixed("└" + "─" * (w - 2) + "┘"),
        *(Fixed(pad(row, w)) for row in foot_now),
    ]
    return build(view, rows, keybar(STACK_KEYS, w))


# ---------- the route's keys: the lens, the sections, the filter, the edit ----------


def _start_edit(ctx: Ctx, settings: EffectiveSettingsView, leaf: SettingsLeaf, at: Layer) -> None:
    """Open the editor ``leaf`` takes, writing to ``at`` and seeded from what ``at`` holds."""
    s = ctx.s
    seed = _held(leaf, at) or ""
    values = list(pick_values(leaf))
    if leaf.editor is not None:
        partners = {key: _held(settings.leaf(key), at) for key in pair_siblings(leaf.key)}
        s.edit = editors.open_editor(leaf, at, _held(leaf, at), partners)
    elif values:
        s.edit = {
            "kind": "pick",
            "key": leaf.key,
            "at": at.value,
            "vals": values,
            "idx": values.index(seed) if seed in values else 0,
        }
    else:
        text = seed.strip("[]") if leaf.value_type == "list_str" else seed
        kind = "num" if leaf.value_type in _NUMERIC else "text"
        text = "" if text == '""' else text
        s.edit = {"kind": kind, "key": leaf.key, "at": at.value, "text": text}
    ctx.log("Enter", f"editing {leaf.key} · writes to {at} · Esc writes nothing")


def _open_edit(ctx: Ctx, settings: EffectiveSettingsView, leaf: SettingsLeaf) -> None:
    """Open the editor for ``leaf`` under the lens, offer the layers that hold it, or refuse.

    A key the lens layer cannot hold is offered at the file layers that can, highest
    precedence first; choosing one writes there, and the lens itself does not move.
    """
    s = ctx.s
    at = lens(s)
    layers = offered(leaf, settings.lens_layers)
    if at not in leaf.editable_at and layers and not refusal(leaf, layers[0]):
        names = [layer.value for layer in layers]
        s.edit = {"kind": "offer", "key": leaf.key, "layers": names, "idx": 0}
        ctx.log("Enter", f"{at} cannot hold {leaf.key} · choose the layer to write it at")
        return
    refused = refusal(leaf, at)
    if refused:
        ctx.log("Enter", refused)
        return
    _start_edit(ctx, settings, leaf, at)


def coerce(leaf: SettingsLeaf, text: str) -> tuple[Any, str]:
    """Return the typed value ``text`` stands for under the key's catalog type.

    Args:
        leaf: The key being edited, whose ``value_type`` decides the parse.
        text: What the operator chose or typed.

    Returns:
        The value and an empty reason, or ``None`` and why nothing can be written.
    """
    match leaf.value_type:
        case "bool":
            return text == "true", ""
        case "int":
            try:
                return int(text.strip()), ""
            except ValueError:
                return None, f"{text!r} is not a whole number · nothing written"
        case "float":
            try:
                return float(text.strip()), ""
            except ValueError:
                return None, f"{text!r} is not a number · nothing written"
        case "list_str":
            return [item.strip() for item in text.split(",") if item.strip()], ""
        case _:
            return text, ""


def step(leaf: SettingsLeaf, text: str, up: bool) -> str:
    """Return ``text`` moved one step up or down, held inside the key's catalog range.

    An int moves by one and a float by a twentieth; a text that is not a number yet steps
    from zero. A step that would leave the catalog's ``value_range`` stops at its end, so
    the arrows never offer a value the daemon refuses.
    """
    whole = leaf.value_type == "int"
    try:
        current = float(text.strip()) if text.strip() else 0.0
    except ValueError:
        current = 0.0
    moved = current + (1 if up else -1) * (1 if whole else _FLOAT_STEP)
    low, high = leaf.value_range or (None, None)
    if low is not None:
        moved = max(moved, low)
    if high is not None:
        moved = min(moved, high)
    return str(round(moved)) if whole else f"{round(moved, 2):g}"


def _request(
    settings: EffectiveSettingsView,
    leaf: SettingsLeaf,
    at: Layer,
    *,
    value: Any = None,
    unset: bool = False,
    together: tuple[tuple[str, Any], ...] = (),
) -> SettingRequest:
    """Return the request writing ``value`` for ``leaf`` at ``at``, or removing it there."""
    return SettingRequest(
        target=leaf.key,
        layer=at.value,
        value=value,
        unset=unset,
        branch=settings.branch if at is Layer.BRANCH else None,
        together=together,
    )


def _commit_whole(
    ctx: Ctx, settings: EffectiveSettingsView, leaf: SettingsLeaf, edit: dict[str, Any]
) -> None:
    """Send a list or mapping editor's whole value with its difference, or say why not."""
    at = edit_layer(ctx.s, edit)
    write = editors.written(edit)
    key = "w" if edit["kind"] in ("rows", "pin") else "Enter"
    if write.reason:
        ctx.log(key, write.reason)
        return
    others = [settings.leaf(other) for other in pair_siblings(leaf.key)]
    if write.unset and all(one.stated_at(at) is None for one in (leaf, *others)):
        ctx.log(key, f"{leaf.key} is not set at {at} · nothing to unset")
        return
    ctx.s.edit = None
    token = setting_token(leaf)
    changes = editors.changes(edit, write)
    if write.unset:
        together = tuple((one.key, None) for one in others)
        request = _request(settings, leaf, at, unset=True, together=together)
        open_setting(ctx, request, effect=after_unset(leaf, at), token=token, changes=changes)
        return
    effect = after_write(leaf, at, editors.shown(edit, write))
    if others:
        together = tuple((one.key, write.value[one.key]) for one in others)
        request = _request(settings, leaf, at, value=write.value[leaf.key], together=together)
    else:
        request = _request(settings, leaf, at, value=write.value)
    open_setting(ctx, request, effect=effect, token=token, changes=changes)


def _commit(ctx: Ctx, settings: EffectiveSettingsView, edit: dict[str, Any]) -> None:
    """Write the chosen value at the edit's layer through the daemon, or say why not."""
    s = ctx.s
    leaf = settings.leaf(edit["key"])
    if edit["kind"] in editors.COMPOSITE:
        _commit_whole(ctx, settings, leaf, edit)
        return
    text = edit["vals"][edit["idx"]] if edit["kind"] == "pick" else edit["text"]
    value, why_not = coerce(leaf, text)
    if why_not:
        ctx.log("Enter", why_not)
        return
    s.edit = None
    at = edit_layer(s, edit)
    request = _request(settings, leaf, at, value=value)
    open_setting(ctx, request, effect=after_write(leaf, at, text), token=setting_token(leaf))


def _offer_key(ctx: Ctx, settings: EffectiveSettingsView, edit: dict[str, Any], key: str) -> None:
    """Move over the offered layers; Enter opens the key's editor writing to the one chosen."""
    s = ctx.s
    if key == "Escape":
        s.edit = None
        ctx.log("Esc", f"the lens stays at {lens(s)} · nothing written")
    elif key in ("ArrowDown", "ArrowUp"):
        edit["idx"] = (edit["idx"] + (1 if key == "ArrowDown" else -1)) % len(edit["layers"])
    elif key == "Enter":
        s.edit = None
        _start_edit(ctx, settings, settings.leaf(edit["key"]), Layer(edit["layers"][edit["idx"]]))


def _composite_key(
    ctx: Ctx, settings: EffectiveSettingsView, edit: dict[str, Any], key: str, shift: bool
) -> None:
    """Hand a key to the open list or mapping editor, then act on what it asked for."""
    verdict, note = editors.press(edit, key, shift)
    if verdict == "cancel":
        ctx.s.edit = None
        ctx.log("Esc", "edit cancelled · nothing written")
    elif verdict == "write":
        _commit(ctx, settings, edit)
    elif note:
        ctx.log(key, note)


def _edit_key(ctx: Ctx, settings: EffectiveSettingsView, key: str, shift: bool) -> bool:
    """Route a key to the open editor; Escape leaves the edit before it leaves the route."""
    s = ctx.s
    edit = s.edit
    assert edit is not None, "only called with an edit open"
    kind = edit["kind"]
    if kind == "offer":
        _offer_key(ctx, settings, edit, key)
    elif kind in editors.COMPOSITE:
        _composite_key(ctx, settings, edit, key, shift)
    elif key == "Escape":
        s.edit = None
        ctx.log("Esc", "edit cancelled · nothing written")
    elif key == "Enter":
        _commit(ctx, settings, edit)
    elif kind == "pick" and key in ("ArrowDown", "ArrowUp"):
        edit["idx"] = (edit["idx"] + (1 if key == "ArrowDown" else -1)) % len(edit["vals"])
    elif kind == "num" and key in ("ArrowDown", "ArrowUp"):
        edit["text"] = step(settings.leaf(edit["key"]), edit["text"], key == "ArrowUp")
    elif kind != "pick" and key == "Backspace":
        edit["text"] = edit["text"][:-1]
    elif kind != "pick" and len(key) == 1:
        edit["text"] += key
    return True


def _unset(ctx: Ctx, settings: EffectiveSettingsView, leaf: SettingsLeaf) -> None:
    """Remove the lens layer's value, or say there is nothing there to remove.

    A key no code reads is removed from whichever file layer states it, since that
    removal is the one edit left for it; any other key only where it may be written.
    """
    at = lens(ctx.s)
    if leaf.stated_at(at) is None:
        ctx.log("x", f"{leaf.key} is not set at {at} · nothing to unset")
        return
    refused = "" if leaf.unread else refusal(leaf, at)
    if refused:
        ctx.log("x", refused)
        return
    request = _request(settings, leaf, at, unset=True)
    open_setting(ctx, request, effect=after_unset(leaf, at), token=setting_token(leaf))


def _filter_key(ctx: Ctx, settings: EffectiveSettingsView, key: str) -> bool:
    """Handle a key while the route's filter field is open: type, keep, clear or move."""
    s = ctx.s
    section, _offset, _leaf = placement(s, settings)
    if key == "Escape":
        s.set_typing = s.typing = False
        s.set_filter = ""
        ctx.log("Esc", "filter cleared")
    elif key == "Enter":
        s.set_typing = s.typing = False
        shown = len(section_keys(settings, s, section))
        kept = "filter kept · " if s.set_filter else "no filter · "
        ctx.log("Enter", f"{kept}{shown} of {len(settings.keys_of(section))} keys")
    elif key == "Backspace":
        s.set_filter = s.set_filter[:-1]
        s.set_key = 0
    elif key in ("ArrowDown", "ArrowUp"):
        count = len(section_keys(settings, s, section))
        s.set_key = max(0, min(count - 1, s.set_key + (1 if key == "ArrowDown" else -1)))
    elif len(key) == 1:
        s.set_filter += key
        s.set_key = 0
    return True


def _browse_key(ctx: Ctx, settings: EffectiveSettingsView, key: str, shift: bool) -> bool:
    """Handle the lens, section and cursor keys while browsing; return whether claimed."""
    s = ctx.s
    section, _offset, _leaf = placement(s, settings)
    if key in ("l", "L"):
        layers = settings.lens_layers
        at = lens(s)
        step_by = -1 if key == "L" or shift else 1
        here = layers.index(at) if at in layers else 0
        s.lens = layers[(here + step_by) % len(layers)].value
        ctx.log(key, f"lens → {s.lens} · {LAYER_PLACES[Layer(s.lens)][1]}")
        return True
    if key == "Tab":
        count = len(settings.sections())
        s.set_sec = (s.set_sec + (-1 if shift else 1)) % max(count, 1)
        s.set_key = 0
        s.set_filter = ""
        section, _offset, _leaf = placement(s, settings)
        ctx.log("Shift+Tab" if shift else "Tab", f"{settings.category_of(section)} ▸ {section}")
        return True
    if key in ("ArrowDown", "ArrowUp"):
        keys = section_keys(settings, s, section)
        s.set_key = max(0, min(len(keys) - 1, s.set_key + (1 if key == "ArrowDown" else -1)))
        return True
    return False


def native_seam(ctx: Ctx, settings: EffectiveSettingsView, key: str, shift: bool) -> bool:
    """Handle the Settings route's keys over the effective-settings view.

    Args:
        ctx: The keystroke's context; its session carries the section, key, lens, filter
            and edit.
        settings: The view the frame was drawn from.
        key: The dispatcher name of the key pressed.
        shift: Whether Shift was held, which steps Tab back a section.

    Returns:
        Whether the key was claimed.
    """
    s = ctx.s
    if s.overlay:
        return False
    if s.edit is not None:
        return _edit_key(ctx, settings, key, shift)
    if s.set_typing:
        return _filter_key(ctx, settings, key)
    if key == "\\":
        s.set_typing = s.typing = True
        section, _offset, _leaf = placement(s, settings)
        ctx.log("\\", f"filter · type to narrow {section} · Esc clears")
        return True
    if _browse_key(ctx, settings, key, shift):
        return True
    _section, _offset, leaf = placement(s, settings)
    if leaf is None:
        return False
    if key == "Enter":
        _open_edit(ctx, settings, leaf)
    elif key == "x":
        _unset(ctx, settings, leaf)
    elif key == "i":
        go(ctx, "settings.stack", "stack")
    else:
        return False
    return True
