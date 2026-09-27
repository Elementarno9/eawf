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
from collections.abc import Sequence
from typing import Any

from eawf.kernel.config.layered import LAYER_ORDER, Layer
from eawf.kernel.projection.settings import (
    LAYER_PLACES,
    LENS_LAYERS,
    EffectiveSettingsView,
    SettingsLeaf,
)
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    needs_count,
    thin,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.operations import SettingRequest
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, TRUTH
from eawf.surfaces.tui.console.width import cell_len, pad

#: The route keybar while browsing, in the order the packet states it.
ROUTE_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "field"),
    ("Tab", "section"),
    ("Enter", "edit"),
    ("l", "layer"),
    ("x", "unset"),
    ("Esc", "back"),
)

#: The stack card's keybar: it moves between layers and closes, and edits nothing.
STACK_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "layer"), ("Esc", "close"))

_PICK_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "choose"), ("Enter", "write"), ("Esc", "cancel"))
_TEXT_KEYS: tuple[tuple[str, str], ...] = (("type", "value"), ("Enter", "write"), ("Esc", "cancel"))

#: The gutter glyph per lens relation: set at the lens and in force, set at the lens and
#: shadowed, inherited from another layer, and stated by no layer but the defaults.
GLYPH_WINS = "="
GLYPH_SHADOWED = "≠"
GLYPH_INHERITS = "·"
GLYPH_DEFAULT = "–"  # noqa: RUF001

#: How a layer that states nothing for a key reads in the stack card.
NOT_STATED = "–"  # noqa: RUF001

#: The catalog shapes the console edits; a mapping or free-form value is edited in its file.
_PICKED = frozenset({"bool", "literal"})
_TYPED = frozenset({"int", "float", "str", "list_str"})

_STACK = Table([11, 20, 10, 0], 2)
_CHAIN_SEP = " › "  # noqa: RUF001
_READOUT_ROWS = 5


def _crumb(scope: str, *, stack: bool = False) -> str:
    """Return the settings breadcrumb; the stack card names the route as its parent."""
    return " " + CRUMB_SEP.join([BRAND, scope, "Settings", *(["Stack"] if stack else [])])


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
    keys = settings.keys_of(section)
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


def refusal(leaf: SettingsLeaf, at: Layer) -> str:
    """Return why the key cannot be edited at ``at``; empty when it can."""
    if not leaf.editable_at:
        return f"{leaf.key} is locked · no layer may write it"
    if leaf.value_type not in _PICKED | _TYPED:
        where = LAYER_PLACES[at][1]
        return f"{leaf.key} is a {leaf.value_type or 'free-form'} value · edit it in {where}"
    if at not in leaf.editable_at:
        allowed = ", ".join(layer.value for layer in leaf.editable_at)
        return f"{leaf.key} is not editable at {at} · it is editable at {allowed}"
    return ""


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


def _rail_entries(settings: EffectiveSettingsView) -> list[tuple[str, str | None]]:
    """Return the rail's rows: each category heading, then each of its sections."""
    rows: list[tuple[str, str | None]] = []
    for category in settings.rail:
        rows.append((category.name, None))
        rows.extend((category.name, section) for section in category.sections)
    return rows


def rail_width(settings: EffectiveSettingsView) -> int:
    """Return the rail's width: the longest section name and its two-cell marker, never clipped."""
    names = [s for c in settings.rail for s in c.sections] + [c.name for c in settings.rail]
    return max((cell_len(name) for name in names), default=0) + 2


def _chain(at: Layer) -> str:
    return _CHAIN_SEP.join(f"[{layer}]" if layer is at else layer.value for layer in LENS_LAYERS)


def _clip(text: str, n: int) -> str:
    return pad(text, n) if cell_len(text) > n else text


def _readout(leaf: SettingsLeaf | None, session: Session, col: int) -> list[str]:
    """Return the docked readout: the key's type, meaning and allowed values, or the edit."""
    if leaf is None:
        return [" this section holds no keys"]
    at = lens(session)
    shape = leaf.value_type or "outside the catalog"
    allowed = f" · one of {' | '.join(leaf.allowed)}" if leaf.allowed else ""
    lines = [f" {leaf.key} · {shape}{allowed}"]
    lines += textwrap.wrap(leaf.meaning or "the catalog states no meaning for this key", col - 2)[
        :1
    ]
    lines = [lines[0], *(f" {line}" for line in lines[1:])]
    edit = session.edit
    if edit is not None and edit.get("key") == leaf.key:
        if edit["kind"] == "pick":
            picks = "  ".join(
                ("● " if i == edit["idx"] else "○ ") + value for i, value in enumerate(edit["vals"])
            )
            lines.append(f" VALUE    {picks}")
            shown = edit["vals"][edit["idx"]]
        else:
            lines.append(f" VALUE    {edit['text']}▏")
            shown = edit["text"] or '""'
        lines.append(f" WRITES   {leaf.key} at {at} · {LAYER_PLACES[at][1]}")
        lines.append(f" AFTER    {after_write(leaf, at, shown)}")
    else:
        lines.append(f" {why(leaf, at)}")
    return [_clip(line, col) for line in lines]


def _key_width(col: int) -> int:
    """Return the key column's width: half the body, within readable bounds."""
    return min(34, max(18, col // 2 - 4))


def _key_rows(
    settings: EffectiveSettingsView, session: Session, section: str, col: int, room: int
) -> list[str]:
    """Return the section's key rows windowed onto the cursor, with the window count."""
    keys = settings.keys_of(section)
    at = lens(session)
    if not keys:
        return ["  this section holds no keys"]
    key_w = _key_width(col)
    from_w = 10
    value_w = col - key_w - from_w - 5
    take = max(1, min(len(keys), room - (1 if len(keys) > room else 0)))
    top = max(0, min(session.set_key - take // 2, len(keys) - take))
    rows: list[str] = []
    for index, leaf in enumerate(keys[top : top + take], start=top):
        caret = "▸" if index == session.set_key else " "
        name = leaf.key.split(".", 1)[1] if leaf.key.startswith(f"{section}.") else leaf.key
        source = leaf.source_layer.value if leaf.source_layer is not None else NOT_STATED
        line = (
            f"{caret} {glyph(leaf, at)} "
            + pad(name, key_w - 1)
            + " "
            + pad(value_text(leaf, short=True), value_w)
            + " "
            + pad(source, from_w)
        )
        rows.append(pad(line, col))
    if take < len(keys):
        rows.append(f"  {top + 1}-{top + take} of {len(keys)}")
    return rows


def _body(
    view: View, settings: EffectiveSettingsView, section: str, leaf: SettingsLeaf | None, col: int
) -> list[str]:
    """Return the body column: the section head and chain, the keys, then the readout."""
    session, avail = view.session, view.h - 4
    at = lens(session)
    head = f" {section.upper()}"
    chain = _chain(at)
    body = (
        [pad(head + " " * (col - cell_len(head) - cell_len(chain)) + chain, col)]
        if cell_len(head) + 2 + cell_len(chain) <= col
        else [pad(head, col), pad(f" {chain}", col)]
    )
    key_w = _key_width(col)
    body.append(pad(pad("    KEY", key_w + 4) + pad("VALUE", col - key_w - 14) + "FROM", col))
    readout = _readout(leaf, session, col)[:_READOUT_ROWS]
    room = max(1, avail - len(body) - 1 - len(readout))
    body += _key_rows(settings, session, section, col, room)
    body += ["" for _ in range(avail - len(body) - 1 - len(readout))]
    body.append("─" * col)
    body += readout
    return body


def _with_rail(
    settings: EffectiveSettingsView, section: str, body: Sequence[str], w: int, rows: int
) -> list[str]:
    """Return the body beside the rail, the rail windowed onto the selected section."""
    rail = _rail_entries(settings)
    width = rail_width(settings)
    col = w - width - 2
    selected = next((i for i, (_c, s) in enumerate(rail) if s == section), 0)
    top = max(0, min(selected - rows // 2, max(0, len(rail) - rows)))
    out: list[str] = []
    for i in range(rows):
        entry = rail[top + i] if top + i < len(rail) else None
        if entry is None:
            label = ""
        elif entry[1] is None:
            label = entry[0]
        else:
            label = ("▸ " if entry[1] == section else "  ") + entry[1]
        cell = body[i] if i < len(body) else ""
        join = "├─" if cell.startswith("─") else "│ "
        out.append(Fixed(pad(pad(label, width) + join + pad(cell, col), w)))
    return out


def settings_frame(view: View, settings: EffectiveSettingsView) -> list[str]:
    """Return the Settings route: the category rail, the section's keys and the readout.

    Args:
        view: The render being built; its session carries the section, key and lens.
        settings: The effective-settings read model.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    scope = settings.header.scope_id
    section, _offset, leaf = placement(session, settings)
    outside = len(settings.uncatalogued())
    ctx = (
        f" {len(settings.rail)} categories · {plural(len(settings.sections()), 'section')}"
        f" · {settings.category_of(section)} ▸ {section}"
        + (" · editing" if session.edit is not None else "")
        + (
            f" · {group(outside)} {'leaf' if outside == 1 else 'leaves'} off the catalog"
            if outside
            else ""
        )
    )
    col = w - rail_width(settings) - 2
    body = _body(view, settings, section, leaf, col)
    rows: list[str] = [
        header_row(session, crumb=_crumb(scope), scope=scope, needs=needs_count(view), w=w),
        _clip(ctx, w),
        bar(w),
        *_with_rail(settings, section, body, w, view.h - 4),
    ]
    edit = session.edit
    keys = ROUTE_KEYS if edit is None else _PICK_KEYS if edit["kind"] == "pick" else _TEXT_KEYS
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
        rows.append(f" DENIED BY {' › '.join(leaf.deny_chain)}")  # noqa: RUF001
    if leaf.constraint_chain:
        rows.append(f" CONSTRAINED BY {' › '.join(leaf.constraint_chain)}")  # noqa: RUF001
    if leaf.capability_requirement is not None:
        state = leaf.certification_state or f"{TRUTH['unknown'].unicode} certification unknown"
        rows.append(f" NEEDS     {leaf.capability_requirement} · {state}")
    if leaf.secret_ref is not None:
        rows.append(f" SECRET    {leaf.secret_ref} · the value never renders")
    return rows


def stack_frame(view: View, settings: EffectiveSettingsView) -> list[str]:
    """Return the stack card of the key under the route's cursor: all nine layers.

    Args:
        view: The render being built; its session carries the key the card opened on
            and, in ``sel``, the layer row under the cursor.
        settings: The effective-settings read model, the same one the route drew from.

    Returns:
        The full frame, keybar last; the route frame when the section holds no key.
    """
    session, w = view.session, view.w
    scope = settings.header.scope_id
    _section, _offset, leaf = placement(session, settings)
    if leaf is None:
        return settings_frame(view, settings)
    session.sel = min(max(session.sel, 0), len(LAYER_ORDER) - 1)
    at = lens(session)
    source = leaf.source_layer.value if leaf.source_layer is not None else "no layer"
    rows: list[str] = [
        header_row(
            session, crumb=_crumb(scope, stack=True), scope=scope, needs=needs_count(view), w=w
        ),
        _clip(
            f" {leaf.key} · in force from {source} · effective revision "
            f"{group(settings.header.projection_revision)}",
            w,
        ),
        bar(w),
        _STACK.head(["LAYER", "VALUE", "KIND", "WHERE"]),
    ]
    for index, name in enumerate(LAYER_ORDER):
        layer = Layer(name)
        kind, where = LAYER_PLACES[layer]
        stated = leaf.stated_at(layer)
        value = (
            stated
            if stated is None or not leaf.deny_chain
            else f"{TRUTH['denied'].unicode} {stated}"
        )
        line = _STACK.row(
            [layer.value, value or NOT_STATED, kind.value, where], index == session.sel
        )
        rows.append(line if index == session.sel else Fixed(pad(line, w)))
    rows.append(thin(w))
    rows.append(
        _clip(f" WINNING   {source} {value_text(leaf) if leaf.source_layer else ''}".rstrip(), w)
    )
    rows.append(_clip(f" LENS      {at} · l on the Settings route cycles the five file layers", w))
    rows.append(_clip(f" ON LENS   {_on_lens(leaf, at)}", w))
    rows += [_clip(row, w) for row in _tier_two(leaf)]
    return build(view, rows, keybar(STACK_KEYS, w))


# ---------- the route's keys: the lens, the sections, the edit ----------


def _open_edit(ctx: Ctx, leaf: SettingsLeaf) -> None:
    """Open the chooser for ``leaf`` under the lens, or refuse with the reason named."""
    s = ctx.s
    at = lens(s)
    refused = refusal(leaf, at)
    if refused:
        ctx.log("Enter", refused)
        return
    seed = leaf.stated_at(at) or leaf.effective.value or ""
    if leaf.value_type in _PICKED:
        values = ["true", "false"] if leaf.value_type == "bool" else list(leaf.allowed)
        s.edit = {
            "kind": "pick",
            "key": leaf.key,
            "vals": values,
            "idx": values.index(seed) if seed in values else 0,
        }
    else:
        text = seed.strip("[]") if leaf.value_type == "list_str" else seed
        s.edit = {"kind": "text", "key": leaf.key, "text": "" if text == '""' else text}
    ctx.log("Enter", f"editing {leaf.key} · writes to {at} · Esc writes nothing")


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


def _send(ctx: Ctx, request: SettingRequest, done: str) -> None:
    """Hand one edit to the daemon link, saying plainly when there is none."""
    if ctx.send is None or not ctx.send(request):
        ctx.log("Enter" if not request.unset else "x", "no daemon link · nothing was written")
        return
    ctx.log(
        "Enter" if not request.unset else "x",
        f"{done} · the daemon answers and the view is re-read",
    )


def _commit(ctx: Ctx, settings: EffectiveSettingsView, edit: dict[str, Any]) -> None:
    """Write the chosen value at the lens through the daemon, or say why it was not sent."""
    s = ctx.s
    leaf = settings.leaf(edit["key"])
    text = edit["vals"][edit["idx"]] if edit["kind"] == "pick" else edit["text"]
    value, why_not = coerce(leaf, text)
    if why_not:
        ctx.log("Enter", why_not)
        return
    s.edit = None
    at = lens(s)
    request = SettingRequest(
        target=leaf.key,
        layer=at.value,
        value=value,
        branch=settings.branch if at is Layer.BRANCH else None,
    )
    _send(ctx, request, f"writing {leaf.key} = {text} at {at}")


def _edit_key(ctx: Ctx, settings: EffectiveSettingsView, key: str) -> bool:
    """Route a key to the open chooser; Escape leaves the edit before it leaves the route."""
    s = ctx.s
    edit = s.edit
    assert edit is not None, "only called with an edit open"
    if key == "Escape":
        s.edit = None
        ctx.log("Esc", "edit cancelled · nothing written")
    elif key == "Enter":
        _commit(ctx, settings, edit)
    elif edit["kind"] == "pick" and key in ("ArrowDown", "ArrowUp"):
        edit["idx"] = (edit["idx"] + (1 if key == "ArrowDown" else -1)) % len(edit["vals"])
    elif edit["kind"] == "text" and key == "Backspace":
        edit["text"] = edit["text"][:-1]
    elif edit["kind"] == "text" and len(key) == 1:
        edit["text"] += key
    return True


def _unset(ctx: Ctx, settings: EffectiveSettingsView, leaf: SettingsLeaf) -> None:
    """Remove the lens layer's value, or say there is nothing there to remove."""
    at = lens(ctx.s)
    if leaf.stated_at(at) is None:
        ctx.log("x", f"{leaf.key} is not set at {at} · nothing to unset")
        return
    refused = refusal(leaf, at)
    if refused:
        ctx.log("x", refused)
        return
    request = SettingRequest(
        target=leaf.key,
        layer=at.value,
        unset=True,
        branch=settings.branch if at is Layer.BRANCH else None,
    )
    _send(ctx, request, f"unsetting {leaf.key} at {at} · {after_unset(leaf, at)}")


def native_seam(ctx: Ctx, settings: EffectiveSettingsView, key: str, shift: bool) -> bool:
    """Handle the Settings route's keys over the effective-settings view.

    Args:
        ctx: The keystroke's context; its session carries the section, key, lens and edit.
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
        return _edit_key(ctx, settings, key)
    section, _offset, leaf = placement(s, settings)
    if key in ("l", "L"):
        at = lens(s)
        step = -1 if key == "L" or shift else 1
        s.lens = LENS_LAYERS[(LENS_LAYERS.index(at) + step) % len(LENS_LAYERS)].value
        ctx.log(key, f"lens → {s.lens} · {LAYER_PLACES[Layer(s.lens)][1]}")
        return True
    if key == "Tab":
        count = len(settings.sections())
        s.set_sec = (s.set_sec + (-1 if shift else 1)) % max(count, 1)
        s.set_key = 0
        section, _offset, leaf = placement(s, settings)
        ctx.log("Shift+Tab" if shift else "Tab", f"{settings.category_of(section)} ▸ {section}")
        return True
    if key in ("ArrowDown", "ArrowUp"):
        keys = settings.keys_of(section)
        s.set_key = max(0, min(len(keys) - 1, s.set_key + (1 if key == "ArrowDown" else -1)))
        return True
    if leaf is None:
        return False
    if key == "Enter":
        _open_edit(ctx, leaf)
        return True
    if key == "x":
        _unset(ctx, settings, leaf)
        return True
    if key == "i":
        go(ctx, "settings.stack", "stack")
        return True
    return False
