"""The Settings route's editors for a key whose value is a list or a mapping.

A key holding one scalar is picked or typed on the route itself; the keys here hold a
set, an ordering, a fixed ladder, a record keyed by role, or a ledger of digests, and a
comma-separated text field would let an operator write a value the reader then refuses:
a profile that does not exist, a two-model ladder, a misspelled role, a hand-typed hash.
Each editor offers only what its reader accepts, taken from the code that reads it
through the settings view, and rebuilds the whole value, which the daemon then writes as
one value under its lock.

Every editor keeps its state in the session's ``edit`` mapping, so the route redraws it
from the session alone, and states the difference its write makes, which the
consequence card shows before anything is sent.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from eawf.kernel.config.layered import Layer
from eawf.kernel.projection.settings import SettingsLeaf
from eawf.surfaces.tui.console.width import cell_len, pad

Edit = dict[str, Any]
Pairs = tuple[tuple[str, str], ...]

#: The editor kinds this module owns: every catalog ``editor`` value.
COMPOSITE = frozenset({"check", "order", "tiers", "rows", "pin"})

#: The slots of a model ladder, cheapest first.
TIERS = ("cheap", "mid", "top")

#: The keybar of each editor, and of the rows editor while a role's tools are typed.
KEYS: Mapping[str, Pairs] = MappingProxyType(
    {
        "check": (("↑↓←→", "move"), ("Space", "toggle"), ("Enter", "write"), ("Esc", "cancel")),
        "order": (
            ("↑↓", "move"),
            ("Space", "toggle"),
            ("Shift+↑↓", "reorder"),
            ("Enter", "write"),
            ("Esc", "cancel"),
        ),
        "tiers": (("↑↓", "slot"), ("type", "id"), ("Enter", "write"), ("Esc", "cancel")),
        "rows": (
            ("↑↓", "role"),
            ("Enter", "tools"),
            ("x", "drop"),
            ("w", "write"),
            ("Esc", "cancel"),
        ),
        "rows.typing": (("type", "tools"), ("Enter", "keep"), ("Esc", "back")),
        "pin": (("↑↓", "profile"), ("p", "pin"), ("x", "drop"), ("w", "write"), ("Esc", "cancel")),
    }
)

_ADDED = "+"
_DROPPED = "−"  # noqa: RUF001
_NONE = "–"  # noqa: RUF001
_TICKED = "[×]"  # noqa: RUF001
_CLEAR = "[ ]"
_DIGEST = 8


@dataclass(frozen=True, slots=True)
class Written:
    """What an editor's write sends, or why it sends nothing.

    Attributes:
        value: The whole value to write; ``None`` for an unset or a refusal.
        unset: Whether the write removes the layer's value instead.
        reason: Why nothing can be written; empty when something can.
    """

    value: Any = None
    unset: bool = False
    reason: str = ""


def items(text: str | None) -> list[str]:
    """Return the members of a list as the settings view prints it; none for a null."""
    if not text or text in ("null", "[]"):
        return []
    return [part.strip() for part in text.strip("[]").split(",") if part.strip()]


def _short(digest: str) -> str:
    """Return a digest as a pin row names it: its first hex digits, without the algorithm."""
    return digest.split(":", 1)[-1][:_DIGEST]


# ---------- opening ----------


def _open_list(leaf: SettingsLeaf, edit: Edit, held: str | None) -> None:
    ticked = items(held)
    known = list(leaf.allowed)
    unknown = [value for value in ticked if value not in known]
    if leaf.editor == "order":
        edit["opts"] = ticked + [value for value in known if value not in ticked]
    else:
        edit["opts"] = known + unknown
    edit["on"] = ticked
    edit["was"] = list(ticked)


def _open_tiers(edit: Edit, held: str | None) -> None:
    ladder = items(held)
    edit["slots"] = ladder if len(ladder) == len(TIERS) else ["" for _ in TIERS]
    edit["was"] = list(ladder)


def _open_rows(leaf: SettingsLeaf, edit: Edit, at: Layer) -> None:
    stated = leaf.data_at(at)
    record = {
        str(role): [str(tool) for tool in tools]
        for role, tools in (stated.items() if isinstance(stated, dict) else ())
        if isinstance(tools, list)
    }
    edit["roles"] = list(leaf.allowed) + [role for role in record if role not in leaf.allowed]
    edit["tools"] = {role: list(tools) for role, tools in record.items()}
    edit["was"] = record
    edit["typing"] = None


def _open_pin(leaf: SettingsLeaf, edit: Edit, at: Layer) -> None:
    stated = leaf.data_at(at)
    pins = {str(k): str(v) for k, v in (stated.items() if isinstance(stated, dict) else ())}
    current = dict(leaf.candidates)
    edit["names"] = list(current) + [name for name in pins if name not in current]
    edit["current"] = current
    edit["pins"] = dict(pins)
    edit["was"] = pins


def open_editor(leaf: SettingsLeaf, at: Layer, held: str | None) -> Edit:
    """Return the edit state for ``leaf`` written at ``at``, seeded from what it holds.

    Args:
        leaf: A key whose catalog ``editor`` is one of :data:`COMPOSITE`.
        at: The layer the edit writes to.
        held: The value the layer sees as the settings view prints it: its own, else
            the one it inherits. A mapping is seeded from the layer's own statement
            instead, since the layers below still contribute theirs to the merge.
    """
    edit: Edit = {"kind": leaf.editor, "key": leaf.key, "at": at.value, "idx": 0}
    match leaf.editor:
        case "check" | "order":
            _open_list(leaf, edit, held)
        case "tiers":
            _open_tiers(edit, held)
        case "rows":
            _open_rows(leaf, edit, at)
        case _:
            _open_pin(leaf, edit, at)
    return edit


# ---------- keys ----------


def _move(edit: Edit, by: int, count: int) -> None:
    edit["idx"] = min(max(edit["idx"] + by, 0), max(count - 1, 0))


def _toggle(edit: Edit) -> None:
    value = edit["opts"][edit["idx"]]
    if value in edit["on"]:
        edit["on"].remove(value)
    else:
        edit["on"].append(value)


def _press_check(edit: Edit, key: str, shift: bool) -> tuple[str, str]:
    cols = edit.get("cols", 1)
    step = {"ArrowRight": 1, "ArrowLeft": -1, "ArrowDown": cols, "ArrowUp": -cols}.get(key)
    if step is not None:
        _move(edit, step, len(edit["opts"]))
    elif key == " " and edit["opts"]:
        _toggle(edit)
    return "", ""


def _press_order(edit: Edit, key: str, shift: bool) -> tuple[str, str]:
    opts, here = edit["opts"], edit["idx"]
    if key in ("ArrowUp", "ArrowDown"):
        _move(edit, 1 if key == "ArrowDown" else -1, len(opts))
        there = edit["idx"]
        if shift and there != here:
            opts[here], opts[there] = opts[there], opts[here]
    elif key == " " and opts:
        _toggle(edit)
    return "", ""


def _press_tiers(edit: Edit, key: str, shift: bool) -> tuple[str, str]:
    slots = edit["slots"]
    if key in ("ArrowUp", "ArrowDown"):
        _move(edit, 1 if key == "ArrowDown" else -1, len(slots))
    elif key == "Backspace":
        slots[edit["idx"]] = slots[edit["idx"]][:-1]
    elif len(key) == 1 and not key.isspace():
        slots[edit["idx"]] += key
    return "", ""


def _type_tools(edit: Edit, key: str) -> tuple[str, str]:
    """Take a key while a role's tools are typed: Enter keeps them, Escape drops the typing."""
    role = edit["roles"][edit["idx"]]
    if key == "Escape":
        edit["typing"] = None
        return "", f"{role} kept as it was"
    if key == "Enter":
        tools = items(edit["typing"])
        if tools:
            edit["tools"][role] = tools
        else:
            edit["tools"].pop(role, None)
        edit["typing"] = None
        return "", f"{role} · {len(tools)} tools · w writes"
    edit["typing"] = edit["typing"][:-1] if key == "Backspace" else edit["typing"] + key
    return "", ""


def _press_rows(edit: Edit, key: str, shift: bool) -> tuple[str, str]:
    role = edit["roles"][edit["idx"]]
    if key in ("ArrowUp", "ArrowDown"):
        _move(edit, 1 if key == "ArrowDown" else -1, len(edit["roles"]))
    elif key == "Enter":
        edit["typing"] = ", ".join(edit["tools"].get(role, []))
        return "", f"typing {role}'s tools · comma-separated · Enter keeps"
    elif key == "x":
        edit["tools"].pop(role, None)
        return "", f"{role} dropped · w writes"
    elif key == "w":
        return "write", ""
    return "", ""


def _press_pin(edit: Edit, key: str, shift: bool) -> tuple[str, str]:
    names = edit["names"]
    name = names[edit["idx"]] if names else ""
    if key in ("ArrowUp", "ArrowDown"):
        _move(edit, 1 if key == "ArrowDown" else -1, len(names))
    elif key == "p" and name in edit["current"]:
        edit["pins"][name] = edit["current"][name]
        return "", f"{name} pinned at {_short(edit['current'][name])} · w writes"
    elif key == "p" and name:
        return "", f"{name} has no digest to pin · it is not discoverable here"
    elif key == "x" and edit["pins"].pop(name, None) is not None:
        return "", f"{name} unpinned · w writes"
    elif key == "w":
        return "write", ""
    return "", ""


_PRESS: Mapping[str, Callable[[Edit, str, bool], tuple[str, str]]] = MappingProxyType(
    {
        "check": _press_check,
        "order": _press_order,
        "tiers": _press_tiers,
        "rows": _press_rows,
        "pin": _press_pin,
    }
)


def press(edit: Edit, key: str, shift: bool) -> tuple[str, str]:
    """Apply one key to the open editor.

    Args:
        edit: The editor's state, changed in place.
        key: The dispatcher name of the key pressed.
        shift: Whether Shift was held, which reorders in the order editor.

    Returns:
        The verdict, ``write``, ``cancel`` or empty for a key the editor kept, and a note
        for the log, empty when the key says nothing worth logging.
    """
    if edit["kind"] == "rows" and edit["typing"] is not None:
        return _type_tools(edit, key)
    if key == "Escape":
        return "cancel", ""
    if key == "Enter" and edit["kind"] in ("check", "order", "tiers"):
        return "write", ""
    return _PRESS[edit["kind"]](edit, key, shift)


def keys(edit: Edit) -> Pairs:
    """Return the keybar of the open editor."""
    typing = edit["kind"] == "rows" and edit["typing"] is not None
    return KEYS["rows.typing" if typing else edit["kind"]]


# ---------- the write ----------


def written(edit: Edit) -> Written:
    """Return the whole value the open editor writes, an unset, or why it writes nothing."""
    match edit["kind"]:
        case "check":
            return Written(list(edit["on"]))
        case "order":
            return Written([value for value in edit["opts"] if value in edit["on"]])
        case "tiers":
            return _ladder(edit["slots"])
        case "rows":
            tools = edit["tools"]
            return Written({role: list(tools[role]) for role in edit["roles"] if tools.get(role)})
        case _:
            pins = edit["pins"]
            return Written({name: pins[name] for name in edit["names"] if name in pins})


def _ladder(slots: Sequence[str]) -> Written:
    """Return a ladder's write: all three ids, or none of them, which unsets the ladder."""
    filled = [slot.strip() for slot in slots if slot.strip()]
    if not filled:
        return Written(unset=True)
    if len(filled) < len(TIERS):
        return Written(
            reason=f"a ladder is three model ids or none · {len(filled)} of 3 filled · "
            "nothing written"
        )
    return Written(filled)


def _list_changes(was: Sequence[str], now: Sequence[str]) -> list[str]:
    changes = [f"{_ADDED} {value}" for value in now if value not in was]
    changes += [f"{_DROPPED} {value}" for value in was if value not in now]
    if not changes and list(was) != list(now):
        changes.append(f"order {' › '.join(now)}")  # noqa: RUF001
    return changes


def _record_changes(was: Mapping[str, list[str]], now: Mapping[str, list[str]]) -> list[str]:
    width = max((cell_len(role) for role in {*was, *now}), default=0)
    changes: list[str] = []
    same = 0
    for role in dict.fromkeys([*was, *now]):
        before, after = was.get(role, []), now.get(role, [])
        rows = [f"{pad(role, width)}  {_DROPPED} {tool}" for tool in before if tool not in after]
        rows += [f"{pad(role, width)}  {_ADDED} {tool}" for tool in after if tool not in before]
        changes += rows
        same += 0 if rows else 1
    if changes and same:
        changes.append(f"{same} {'role' if same == 1 else 'roles'} unchanged")
    return changes


def _pin_changes(was: Mapping[str, str], now: Mapping[str, str]) -> list[str]:
    changes: list[str] = []
    for name in dict.fromkeys([*was, *now]):
        before, after = was.get(name), now.get(name)
        if before == after:
            continue
        if before is None:
            changes.append(f"{name}  {_ADDED} {_short(str(after))}")
        elif after is None:
            changes.append(f"{name}  {_DROPPED} {_short(before)}")
        else:
            changes.append(f"{name}  {_short(before)} → {_short(after)}")
    return changes


def changes(edit: Edit, write: Written) -> tuple[str, ...]:
    """Return the per-member difference ``write`` makes to what the layer stated."""
    was = edit["was"]
    match edit["kind"]:
        case "check" | "order":
            return tuple(_list_changes(was, write.value))
        case "tiers":
            before = [*was, "", "", ""][: len(TIERS)]
            after = ([] if write.unset else [*write.value])[: len(TIERS)] + [""] * len(TIERS)
            return tuple(
                f"{tier}  {old or _NONE} → {new or _NONE}"
                for tier, old, new in zip(TIERS, before, after, strict=False)
                if old != new
            )
        case "rows":
            return tuple(_record_changes(was, write.value))
        case _:
            return tuple(_pin_changes(was, write.value))


def shown(edit: Edit, write: Written) -> str:
    """Return the write as one short phrase for the readout's AFTER line."""
    if write.unset:
        return "the built-in ladder"
    members = write.value
    if isinstance(members, list):
        sep = " › " if edit["kind"] in ("order", "tiers") else ", "  # noqa: RUF001
        return sep.join(members) or "[]"
    noun = "role" if edit["kind"] == "rows" else "pin"
    return f"{len(members)} {noun}{'' if len(members) == 1 else 's'}"


# ---------- drawing ----------


def head(leaf: SettingsLeaf) -> str:
    """Return what the readout's head says of the value set an editor ranges over."""
    match leaf.editor:
        case "check":
            return f"ticked from {len(leaf.allowed)} known values"
        case "order":
            return f"ordered from {' | '.join(leaf.allowed)}"
        case "tiers":
            return "three model ids, cheapest first"
        case "rows":
            return f"tools keyed by * for every role, or by one of {len(leaf.allowed) - 1} roles"
        case _:
            return "digests the console computes; you never type one"


def _window(lines: list[str], at: int, room: int) -> list[str]:
    """Return at most ``room`` of ``lines``, keeping line ``at`` in view."""
    if len(lines) <= room:
        return lines
    top = max(0, min(at - room // 2, len(lines) - room))
    return lines[top : top + room]


def _check_lines(edit: Edit, col: int, room: int) -> list[str]:
    opts = edit["opts"]
    cell = max((cell_len(value) for value in opts), default=1) + 7
    cols = max(1, (col - 2) // cell)
    edit["cols"] = cols
    grid: list[str] = []
    for row in range(math.ceil(len(opts) / cols)):
        cells = []
        for i in range(row * cols, min(len(opts), (row + 1) * cols)):
            caret = "▸" if i == edit["idx"] else " "
            box = _TICKED if opts[i] in edit["on"] else _CLEAR
            cells.append(pad(f"{caret} {box} {opts[i]}", cell))
        grid.append(" " + "".join(cells).rstrip())
    hint = " Space toggles · the order written is the order ticked"
    return [hint, *_window(grid, edit["idx"] // cols, max(1, room - 1))]


def _order_lines(edit: Edit, room: int) -> list[str]:
    rank = 0
    rows: list[str] = []
    for i, value in enumerate(edit["opts"]):
        ticked = value in edit["on"]
        rank += 1 if ticked else 0
        caret = "▸" if i == edit["idx"] else " "
        number = str(rank) if ticked else _NONE
        rows.append(f" {caret} {number} {_TICKED if ticked else _CLEAR} {value}")
    hint = " the first ticked runtime that is reachable is dispatched to"
    return [hint, *_window(rows, edit["idx"], max(1, room - 1))]


def _tier_lines(edit: Edit) -> list[str]:
    rows = []
    for i, tier in enumerate(TIERS):
        pointed = i == edit["idx"]
        text = edit["slots"][i] + ("▏" if pointed else "")
        rows.append(f" {'▸' if pointed else ' '} {pad(tier, 7)} {text or _NONE}")
    return [*rows, " empty all three and Enter to fall back to the built-in ladder"]


def _tools_cell(tools: Sequence[str]) -> str:
    if not tools:
        return _NONE
    return tools[0] + (f" +{len(tools) - 1}" if len(tools) > 1 else "")


def _row_lines(edit: Edit, room: int) -> list[str]:
    width = max(cell_len(role) for role in edit["roles"]) + 2
    rows: list[str] = []
    for i, role in enumerate(edit["roles"]):
        pointed = i == edit["idx"]
        typed = pointed and edit["typing"] is not None
        cell = f"{edit['typing']}▏" if typed else _tools_cell(edit["tools"].get(role, []))
        rows.append(f" {'▸' if pointed else ' '} {pad(role, width)} {cell}")
    table = [f"   {pad('ROLE', width)} TOOLS", *_window(rows, edit["idx"], max(1, room - 2))]
    return [*table, " Enter edits the role's tools, comma-separated · x drops the role"]


def _standing(edit: Edit, name: str) -> str:
    pinned, current = edit["pins"].get(name), edit["current"].get(name)
    if pinned is None:
        return "unpinned"
    return "pinned" if pinned == current else "stale"


def _pin_lines(edit: Edit, room: int) -> list[str]:
    if not edit["names"]:
        return [" no profile here has a digest to pin"]
    width = max(cell_len(name) for name in [*edit["names"], "PROFILE"]) + 2
    rows: list[str] = []
    for i, name in enumerate(edit["names"]):
        pinned = edit["pins"].get(name)
        current = edit["current"].get(name)
        cells = [
            pad(name, width),
            pad(_short(pinned) if pinned else _NONE, _DIGEST + 3),
            pad(_short(current) if current else _NONE, _DIGEST + 3),
            _standing(edit, name),
        ]
        rows.append(f" {'▸' if i == edit['idx'] else ' '} {''.join(cells)}")
    heading = f"   {pad('PROFILE', width)}{pad('PINNED', _DIGEST + 3)}{pad('CURRENT', _DIGEST + 3)}"
    table = [f"{heading}STANDING", *_window(rows, edit["idx"], max(1, room - 2))]
    return [*table, " p pins the current digest · x drops the pin"]


def lines(edit: Edit, col: int, room: int) -> list[str]:
    """Return the open editor's rows for the readout, at most ``room`` of them.

    Args:
        edit: The editor's state; a check grid publishes the column count it drew, so
            the up and down arrows move by a row of the grid the operator sees.
        col: The readout's width.
        room: The rows the readout leaves the editor.
    """
    match edit["kind"]:
        case "check":
            drawn = _check_lines(edit, col, room)
        case "order":
            drawn = _order_lines(edit, room)
        case "tiers":
            drawn = _tier_lines(edit)
        case "rows":
            drawn = _row_lines(edit, room)
        case _:
            drawn = _pin_lines(edit, room)
    return drawn[: max(1, room)]


__all__ = [
    "COMPOSITE",
    "KEYS",
    "TIERS",
    "Written",
    "changes",
    "head",
    "items",
    "keys",
    "lines",
    "open_editor",
    "press",
    "shown",
    "written",
]
