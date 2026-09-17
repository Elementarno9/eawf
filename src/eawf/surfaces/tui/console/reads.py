"""What a count may claim and what a verb may do in each connection state.

``session.conn`` is the input; the projection binder becomes its producer. A connection
state that cannot vouch for a count says so in the frame (a label under the title and an
``ATTACHED`` line), and a state that refuses writes names its reason from the fixture.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.session import Session

# The connection states a write may be sent in.
MUTABLE: frozenset[str] = frozenset({"LIVE", "LIVE / PARTIAL", "DEGRADED"})
_REVISION = "revision 41,208"


@dataclass(frozen=True, slots=True)
class Reads:
    """What the frame may claim about its counts.

    Attributes:
        complete: Whether a count can be called complete.
        label: Why it cannot, shown under the title; empty when it can.
        age: The ``ATTACHED`` line's revision and age; empty when complete.
    """

    complete: bool
    label: str
    age: str


_COMPLETE = Reads(True, "", "")
_READS: dict[str, Reads] = {
    "SNAPSHOT REQUIRED": Reads(
        False, "no usable revision · operator repair needed", "∅ unavailable"
    ),
    "LIVE": _COMPLETE,
    "LIVE / PARTIAL": Reads(False, "partial · affected aggregates are labelled", _REVISION),
    "GAP DETECTED": Reads(False, "no count can be called complete for the gap", _REVISION),
    "OFFLINE SNAPSHOT": Reads(
        False, "a snapshot · nothing is arriving", f"{_REVISION} · 6m 12s old"
    ),
    "REPLAYING": Reads(False, "replaying · cached facts only", _REVISION),
    "SNAPSHOT LOADING": Reads(
        False, "loading · the previous revision, labelled with its age", _REVISION
    ),
    "DEGRADED": Reads(False, "degraded · a feed is unhealthy", _REVISION),
    "DISCONNECTED": Reads(False, "disconnected · nothing is arriving", _REVISION),
}


def reads(session: Session) -> Reads:
    """Return what a count may claim in the session's connection state."""
    return _READS.get(session.conn, _COMPLETE)


def can_mutate(session: Session) -> bool:
    """Return whether the session's connection state admits a write."""
    return session.conn in MUTABLE


def mut_reason(session: Session, fixture: Fixture) -> str:
    """Return the live reason a write is refused in the session's connection state."""
    return fixture.proto.states.refuse.get(session.conn) or "not permitted in this connection state"


def attn_cell(session: Session, n: int) -> str:
    """Return an attention count, unknown rather than zero when the reads cannot vouch for it."""
    if reads(session).complete:
        return str(n)
    return str(n) if n else "–"  # noqa: RUF001
