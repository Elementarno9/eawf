"""The pause overlay: a control whose outcome is unknown, stated as unknown."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.frame import bar, build, header_row, keybar, thin
from ...chassis.overlays import with_state_rows
from ...chassis.seam import can_mutate

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    rows: list[str] = [
        header_row(s, fixture, " Eä ▸ pause · RUN-a708a7d6", w),
        " the control outcome is unknown · we do not know",
        bar(w),
        " PAUSE     requested   14:03:11 · accepted 14:03:11",
        "           confirmed   —  the run never answered",
        thin(w),
        " RESUME PREDICATE  a heartbeat at or after 14:03:11",
        " RETRY BUDGET      2 of 3 attempts used · 1 left",
        thin(w),
        " UNKNOWN   This is not a failure and not a success. Until the run answers,",
        "           neither resume nor cancel can be confirmed — only requested.",
        thin(w),
        " NOT       Reconciling does not restart the run and does not fail the task.",
    ]
    pairs = (
        [("n", "reconcile"), ("c", "cancel"), ("Esc", "back")]
        if can_mutate(s)
        else [("Esc", "back")]
    )
    keys = keybar(pairs, w)
    # the prototype wraps the BUILT frame, so the state rows sit just above the keybar
    while len(rows) < h - 1:
        rows.append("")
    wrapped = with_state_rows("pause", rows + [keys], s, w)
    return build(s, wrapped[:-1], keys, w, h)
