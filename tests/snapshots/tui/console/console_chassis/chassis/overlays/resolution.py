"""The resolution card: why a purged target no longer resolves and what still reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.frame import bar, build, header_row, keybar, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    rows: list[str] = [
        header_row(session, fixture, " Eä ▸ resolution · RUN-3c6ef367", w),
        " this target no longer resolves",
        bar(w),
        " FACT      RUN-3c6ef367 events purged · revision 41,002",
        thin(w),
        " ENDING    purged · retention 7d",
        "           the fact remains; only its events are gone",
        thin(w),
        " WHAT WORKS  The run, its result and its lineage stay readable.",
        "             its timeline and raw segment do not, and never will",
        thin(w),
        " NOT       This is not missing, moved, retired or denied — four other",
        "           endings, each with its own card and its own remedy.",
    ]
    return build(session, rows, keybar([("Y", "copy URN"), ("Esc", "back")], w), w, h)
