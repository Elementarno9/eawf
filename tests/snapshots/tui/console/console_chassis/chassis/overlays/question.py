"""The question overlay: derived from the selected row and honest about resolved state,
because a confirmed ledger must never be overwritten by re-answering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.attention import question_row
from ...chassis.derive import num
from ...chassis.frame import bar, build, header_row, keybar, thin
from ...chassis.overlays import with_state_rows
from ...chassis.seam import can_mutate

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def _wrap_build(
    name: str, session: Session, rows: list[str], keys: str, w: int, h: int
) -> list[str]:
    """The prototype wraps the BUILT frame, so the state rows sit just above the keybar:
    fill to the body height first, then splice with the keybar standing in as the last row."""
    body = list(rows)
    while len(body) < h - 1:
        body.append("")
    wrapped = with_state_rows(name, body + [keys], session, w)
    return build(session, wrapped[:-1], keys, w, h)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    P = fixture.proto
    q = question_row(s, fixture)
    open_ = q.state == "OPEN"
    live = open_ and can_mutate(s)
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ question · {q.id}", w),
        " asked by EAWF-0042 · "
        + ("waiting for your answer" if open_ else f"already {q.state.lower()} · immutable"),
        bar(w),
        " QUESTION  Which shim may be retired — the wave shim, or the alias table?",
        thin(w),
        f" ASKED     10:02:03 · revision {num(P.revision)}",
        " DEADLINE  "
        + (
            "Open — the run waits; it is not stopped."
            if open_
            else f"{q.due} · the deadline no longer applies."
        ),
        thin(w),
        " ANSWERS   1  the wave shim only",
        "           2  the alias table only",
        "           3  both — they are independent",
        thin(w),
        " NOT       Answering does not dispatch anything and does not merge"
        if open_
        else " NOT       this question is resolved — its ledger is sealed and the",
        "           the batch; the run continues from where it paused."
        if open_
        else "           answers above are the record of what was decided.",
    ]
    pairs = (
        [("1 2 3", "pick an answer"), ("x", "decline"), ("Esc", "back — it stays open")]
        if live
        else [("Esc", "back")]
    )
    return _wrap_build("question", s, rows, keybar(pairs, w), w, h)
