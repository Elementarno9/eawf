"""The consequence card: what a verb will and will not do, read before it is confirmed.
A drawer target (``session.c_target``) describes any route's heavy verb; without one the
card follows the attention row and the pending verb letter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.attention import VERB, attn_list
from ...chassis.derive import num, wrap_pane
from ...chassis.frame import bar, build, header_row, keybar, thin
from ...chassis.registry import kind_of

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS = [("Enter", "confirm"), ("Esc", "cancel")]
_STALE = " IF STALE  This card reloads at the current revision and never overwrites."


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    P = fixture.proto
    if s.c_target:
        t = s.c_target
        rows: list[str] = [
            header_row(s, fixture, " Eä ▸ consequence", w),
            f" {t['verb']} · {t['id']} · {kind_of(t['id'])}",
            bar(w),
            f" ASKING    {t['verb']} {t['id']}",
            f" AT        revision {num(P.revision)} · exact",
            thin(w),
            *wrap_pane(
                "EFFECTS",
                t.get("effects") or "This verb\u2019s effects are not modelled in this prototype.",
                w,
            ),
            thin(w),
            *wrap_pane("NOT", t.get("not") or "Its non-effects are not modelled either.", w),
            thin(w),
            _STALE,
        ]
        return build(s, rows, keybar(_KEYS, w), w, h)
    a = None
    if s.route == "attention":
        rows_now = attn_list(s, fixture)
        a = rows_now[s.sel] if s.sel < len(rows_now) else None
    if a is None:
        a = P.attention[0]
    verb = s.verb or "a"
    v = VERB[verb]
    if verb == "z":
        extra = "hidden for you only — other principals still see it"
    elif verb == "x":
        extra = "recorded as declined, with your reason — it does not answer"
    elif verb == "v":
        extra = "sealed for every principal — it leaves the active buckets and becomes immutable"
    else:
        extra = a.effects
    if verb == "a":
        not_text = a.not_
    elif verb == "x":
        not_text = a.notDeny or "nothing is granted by declining"
    elif verb == "z":
        not_text = a.notSnooze or "it stays open for every other principal"
    else:
        not_text = a.notResolve or "it is closed as handled, not answered"
    rows = [
        header_row(s, fixture, " Eä ▸ consequence", w),
        f" {v['name']} · {a.id} · {kind_of(a.id)}",
        bar(w),
        f" ASKING    {v['name']} {a.id} → {v['state']}",
        f" AT        revision {num(P.revision)} · exact",
        thin(w),
        *wrap_pane("EFFECTS", extra, w),
        thin(w),
        *wrap_pane("NOT", not_text, w),
        thin(w),
        _STALE,
    ]
    return build(s, rows, keybar(_KEYS, w), w, h)
