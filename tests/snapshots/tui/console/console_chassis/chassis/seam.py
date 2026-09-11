"""The projection seam: what a count may claim and what a verb may do per connection state.

``session.conn`` is the harness-settable input; the product binder later becomes its
producer. Under the simulator flag DISCONNECTED renders as the prototype rendered it
(complete), so the pack's frames compare as they are; the product row is the repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..chassis.fixture import Fixture
    from ..chassis.session import Session

MUTABLE: frozenset[str] = frozenset({"LIVE", "LIVE / PARTIAL", "DEGRADED"})


@dataclass(frozen=True, slots=True)
class Reads:
    complete: bool
    label: str
    age: str


_READS: dict[str, Reads] = {
    "SNAPSHOT REQUIRED": Reads(
        False, "no usable revision · operator repair needed", "∅ unavailable"
    ),
    "LIVE": Reads(True, "", ""),
    "LIVE / PARTIAL": Reads(False, "partial · affected aggregates are labelled", "revision 41,208"),
    "GAP DETECTED": Reads(False, "no count can be called complete for the gap", "revision 41,208"),
    "OFFLINE SNAPSHOT": Reads(
        False, "a snapshot · nothing is arriving", "revision 41,208 · 6m 12s old"
    ),
    "REPLAYING": Reads(False, "replaying · cached facts only", "revision 41,208"),
    "SNAPSHOT LOADING": Reads(
        False, "loading · the previous revision, labelled with its age", "revision 41,208"
    ),
    "DEGRADED": Reads(False, "degraded · a feed is unhealthy", "revision 41,208"),
}
_DISCONNECTED = Reads(False, "disconnected · nothing is arriving", "revision 41,208")
_COMPLETE = Reads(True, "", "")


def reads(session: Session) -> Reads:
    """What a count may claim in this connection state."""
    if session.conn == "DISCONNECTED" and not session.simulator:
        return _DISCONNECTED
    return _READS.get(session.conn, _COMPLETE)


def can_mutate(session: Session) -> bool:
    return session.conn in MUTABLE


def mut_reason(session: Session, fixture: Fixture) -> str:
    return fixture.proto.states.refuse.get(session.conn) or "not permitted in this connection state"


def attn_cell(session: Session, n: int) -> str:
    """An attention count is unknown, not zero, when the projection cannot vouch for it."""
    if reads(session).complete:
        return str(n)
    return str(n) if n else "–"


def parity_text(rows: list[str]) -> str:
    """The plain-text path: the same rows the frame renderer produced, joined."""
    return "\n".join(rows)
