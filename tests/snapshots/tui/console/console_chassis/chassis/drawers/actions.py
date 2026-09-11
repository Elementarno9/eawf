"""The action menu drawer: letter-driven, light verbs first, no cursor. A verb that
cannot act is drawn disabled and keeps its reason."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.attention import menu_order, verb_available
from ...chassis.frame import TBL
from ...chassis.width import cell_len

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    acts = menu_order(session, fixture)
    T = TBL([10, 5, 21, 0], 2)
    out = [T.head(["ACTIONS", "KEY", "VERB", "REASON"])]
    for a in acts:
        ok, why = verb_available(session, fixture, a)
        if cell_len(a[1]) > 21:
            raise ValueError(f"verb too long for the column: {a[1]}")
        if cell_len(why) > w - 39:
            raise ValueError(f"reason too long for the column: {why}")
        out.append(T.row(["", a[0], a[1], "" if ok else why], False))
    if not acts:
        out.append("   No verb is defined for this route.")
    return out
