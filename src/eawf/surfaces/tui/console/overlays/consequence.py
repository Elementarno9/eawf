"""The consequence card: what a verb will and will not do, read before it is confirmed.

A drawer target describes any route's heavy verb; without one the card follows the
Attention row under the cursor and the pending verb letter.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import VERB, attn_list
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.registry import kind_of

CRUMB = " Eä ▸ consequence"
_KEYS: tuple[tuple[str, str], ...] = (("Enter", "confirm"), ("Esc", "cancel"))
_STALE = " IF STALE  This card reloads at the current revision and never overwrites."
# What each attention verb other than answer does, and does not do, beyond its action's text.
_EFFECTS = {
    "z": "hidden for you only — other principals still see it",
    "x": "recorded as declined, with your reason — it does not answer",
    "v": "sealed for every principal — it leaves the active buckets and becomes immutable",
}


def _panes(view: View, effects: str, non_effects: str) -> list[str]:
    w = view.w
    return [
        thin(w),
        *dv.wrap_pane("EFFECTS", effects, w),
        thin(w),
        *dv.wrap_pane("NOT", non_effects, w),
        thin(w),
        _STALE,
    ]


def _not_text(verb: str, action: Action) -> str:
    if verb == "a":
        return action.not_effects
    if verb == "x":
        return action.not_deny or "nothing is granted by declining"
    if verb == "z":
        return action.not_snooze or "it stays open for every other principal"
    return action.not_resolve or "it is closed as handled, not answered"


def render(view: View) -> list[str]:
    """Return the consequence card."""
    s, fx, w = view.session, view.fixture, view.w
    revision = f" AT        revision {group(fx.proto.revision)} · exact"
    target = s.c_target
    if target:
        rows = [
            header(view, CRUMB),
            f" {target['verb']} · {target['id']} · {kind_of(target['id'])}",
            bar(w),
            f" ASKING    {target['verb']} {target['id']}",
            revision,
            *_panes(
                view,
                target.get("effects") or "This verb’s effects are not modelled in this prototype.",  # noqa: RUF001
                target.get("not") or "Its non-effects are not modelled either.",
            ),
        ]
        return build(view, rows, keybar(_KEYS, w))
    shown = attn_list(s, fx) if s.route == "attention" else []
    action = shown[s.sel] if s.sel < len(shown) else fx.proto.attention[0]
    key = s.verb or "a"
    verb = VERB[key]
    rows = [
        header(view, CRUMB),
        f" {verb.name} · {action.id} · {kind_of(action.id)}",
        bar(w),
        f" ASKING    {verb.name} {action.id} → {verb.state}",
        revision,
        *_panes(view, _EFFECTS.get(key, action.effects), _not_text(key, action)),
    ]
    return build(view, rows, keybar(_KEYS, w))
