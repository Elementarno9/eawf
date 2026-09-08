"""The inspect drawer: the focused field with its provenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.derive import copy_target, num

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    P = fixture.proto
    if session.route == "entry":
        e = P.entry[session.entry_sel] if session.entry_sel < len(P.entry) else P.entry[0]
        val = (
            e.rows[session.path_sel][0]
            if e.rows and session.path_sel < len(e.rows)
            else e.state.lower()
        )
        return [
            f" INSPECT   value       {val}",
            "           quality     measured · as stored in the snapshot",
            "           answered by none · no projection exists yet",
            "           revision    41,208 · the last known revision",
            "           freshness   snapshot · 6m 12s old, not live",
        ]
    run = session.route == "run.detail"
    return [
        " INSPECT   value       "
        + ("cost ~4.62 of 20.00" if run else copy_target(session, fixture)),
        "           quality     " + ("~ derived · provider rate card" if run else "measured"),
        f"           answered by daemon@1 · revision {num(P.revision)} · exact",
        "           freshness   as of 14:02 · within this view’s 2s target",
    ]
