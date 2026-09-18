"""The question overlay: the selected question, honest about a resolved state.

A confirmed ledger is never overwritten by answering again.
"""

from __future__ import annotations

from collections.abc import Sequence

from eawf.surfaces.tui.console.attention import OPEN, question_row
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.overlays.states import with_state_rows
from eawf.surfaces.tui.console.reads import can_mutate

ANSWERS: tuple[str, ...] = (
    "the wave shim only",
    "the alias table only",
    "both — they are independent",
)


def build_with_states(view: View, name: str, rows: Sequence[str], keys: str) -> list[str]:
    """Build a decision overlay with its state rows just above the keybar.

    The body is filled to its full height first, so the state rows take the last body
    rows whatever the overlay's own length.
    """
    body = [*rows, *([""] * max(0, view.h - 1 - len(rows)))]
    wrapped = with_state_rows(name, [*body, keys], view.session, view.w)
    return build(view, wrapped[:-1], keys)


def render(view: View) -> list[str]:
    """Return the question overlay."""
    s, fx, w = view.session, view.fixture, view.w
    q = question_row(s, fx)
    is_open = q.state == OPEN
    status = "waiting for your answer" if is_open else f"already {q.state.lower()} · immutable"
    deadline = (
        "Open — the run waits; it is not stopped."
        if is_open
        else f"{q.due} · the deadline no longer applies."
    )
    closing = (
        (
            " NOT       Answering does not dispatch anything and does not merge",
            "           the batch; the run continues from where it paused.",
        )
        if is_open
        else (
            " NOT       this question is resolved — its ledger is sealed and the",
            "           answers above are the record of what was decided.",
        )
    )
    rows = [
        header(view, f" Eä ▸ question · {q.id}"),
        f" asked by EAWF-0042 · {status}",
        bar(w),
        " QUESTION  Which shim may be retired — the wave shim, or the alias table?",
        thin(w),
        f" ASKED     10:02:03 · revision {group(fx.proto.revision)}",
        f" DEADLINE  {deadline}",
        thin(w),
        f" ANSWERS   1  {ANSWERS[0]}",
        f"           2  {ANSWERS[1]}",
        f"           3  {ANSWERS[2]}",
        thin(w),
        *closing,
    ]
    live = [("1 2 3", "pick an answer"), ("x", "decline"), ("Esc", "back — it stays open")]
    pairs = live if is_open and can_mutate(s) else [("Esc", "back")]
    return build_with_states(view, "question", rows, keybar(pairs, w))
