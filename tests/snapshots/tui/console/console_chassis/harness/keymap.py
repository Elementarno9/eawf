"""Key names: the journeys use DOM names, Textual's Pilot uses its own. Both directions
live here so the App's ``on_key`` and the replayer cannot disagree."""

from __future__ import annotations

# JS (DOM) key name -> Pilot key name
JS_TO_PILOT: dict[str, str] = {
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "Home": "home",
    "End": "end",
    "Enter": "enter",
    "Escape": "escape",
    "Tab": "tab",
    "Backspace": "backspace",
    " ": "space",
    "/": "slash",
    "\\": "backslash",
    ".": "full_stop",
    "-": "minus",
    "[": "left_square_bracket",
    "]": "right_square_bracket",
    "?": "question_mark",
    "*": "asterisk",
    "Shift-Tab": "shift+tab",
}
PILOT_TO_JS: dict[str, str] = {v: k for k, v in JS_TO_PILOT.items()}
PILOT_TO_JS["ctrl+f"] = "ctrl+f"

# keys the harness intercepts instead of delivering: `w` cycles the frame size
SIMULATOR_KEYS: frozenset[str] = frozenset({"w"})
# keys that act only under simulator=True and only on the entry layer
ENTRY_SIMULATOR_KEYS: frozenset[str] = frozenset({"[", "]"})


def to_pilot_key(js_key: str) -> str:
    if js_key in JS_TO_PILOT:
        return JS_TO_PILOT[js_key]
    if js_key.startswith("Shift-"):
        return "shift+" + to_pilot_key(js_key[len("Shift-") :])
    return js_key


def to_js_key(pilot_key: str, character: str | None) -> tuple[str, bool] | None:
    """(DOM key, shift) for a Textual key event; None for a bare modifier."""
    if pilot_key == "shift+tab":
        return ("Tab", True)
    if pilot_key in PILOT_TO_JS:
        return (PILOT_TO_JS[pilot_key], False)
    if len(pilot_key) == 1:
        return (pilot_key, False)
    if character and len(character) == 1 and character.isprintable():
        return (character, False)
    if pilot_key.startswith("shift+") and len(pilot_key) == len("shift+") + 1:
        return (pilot_key[-1].upper(), True)
    return None
