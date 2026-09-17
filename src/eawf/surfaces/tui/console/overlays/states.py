"""The state models the four decision overlays step through, and the rows that show them.

Each decision overlay ends with three rows naming the state it is in, what ends that
state and the transitions it can never take; ``s`` steps the overlay through its model.
The rows replace the last body rows of the built frame, just above the keybar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from eawf.surfaces.tui.console.frame import Fixed
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

OV_MODEL: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "question": (
            ("open", "You answer, or another eligible principal does."),
            ("answered", "Nothing — an answer is immutable, and a change is a new question."),
            ("answered elsewhere", "Nothing — your outcome is superseded, not refused."),
            ("expired", "Nothing — the deadline passed with no answer."),
            ("withdrawn", "Nothing — the agent resolved it without you."),
            ("unanswerable", "The asking Run is recovered first."),
        ),
        "pause": (
            ("paused by you", "You resume it."),
            ("waiting on a check", "A predicate the system evaluates comes true."),
            ("waiting on a person", "An eligible principal answers."),
            ("escalated", "An operator with release authority answers."),
            ("held", "A person lifts the hold — no predicate will."),
        ),
        "evidence": (
            ("uncertified", "Rung 4 passes — only rung 4 certifies."),
            ("certified", "Nothing — certification is an outcome, never an inference."),
            ("rung failed", "Rung 3 returned a negative, so rung 4 cannot run."),
            ("digest unavailable", "The input digest becomes fetchable again."),
        ),
        "readiness": (
            ("draft", "Every member reaches ready."),
            ("candidate", "An operator approves against the current head."),
            ("approved", "Publication succeeds, or the bound head moves."),
            ("invalidated → draft", "The members are re-checked — candidate is an illegal target."),
            ("published", "Nothing — publication is immutable."),
        ),
    }
)
OV_IMPOSSIBLE: Mapping[str, str] = MappingProxyType(
    {
        "question": "answered → open · expired → answered · withdrawn → answered",
        "pause": "held → resumed by you · waiting on a check → escalated",
        "evidence": "a passed rung above an unpassed rung · failed by inference",
        "readiness": "approved → candidate · draft → approved · published → draft",
    }
)


def state_rows(name: str, session: Session, w: int) -> list[str]:
    """Return overlay ``name``'s three state rows at its current step.

    Raises:
        KeyError: ``name`` has no state model.
    """
    model = OV_MODEL[name]
    i = session.ov_state.get(name, 0) % len(model)
    word, ends = model[i]
    return [
        Fixed(pad(f" STATE      {word}   ({i + 1} of {len(model)} · s cycles)", w)),
        Fixed(pad(f" ENDS WHEN  {ends}", w)),
        Fixed(pad(f" IMPOSSIBLE {OV_IMPOSSIBLE[name]}", w)),
    ]


def with_state_rows(name: str, rows: Sequence[str], session: Session, w: int) -> list[str]:
    """Return ``rows`` with the state rows spliced in just before the last row."""
    if not rows:
        return list(rows)
    last = len(rows) - 1
    spliced = [*rows[: max(0, last - 3)], *state_rows(name, session, w), rows[last]]
    return [row if isinstance(row, Fixed) else Fixed(pad(row, w)) for row in spliced]


def step_state(session: Session, name: str) -> str | None:
    """Advance overlay ``name`` one step through its model and return the new state's word."""
    model = OV_MODEL.get(name)
    if not model:
        return None
    session.ov_state[name] = (session.ov_state.get(name, 0) + 1) % len(model)
    return model[session.ov_state[name]][0]
